// Tests for the Vercel proxy functions. Run with:
//   node --test web/test/proxy.test.js
// (passing the directory works on Linux/macOS but not on Windows, where Node
// tries to execute the directory itself.)
//
// The point of these is the boundary: the browser must not be able to reach the
// backend without the dashboard token, and the backend token must never appear
// in a response.

const test = require("node:test");
const assert = require("node:assert");
const http = require("node:http");

const { proxy, buildQuery } = require("../lib/proxy");

function fakeRes() {
  const res = { statusCode: 0, headers: {}, body: "", jsonBody: null };
  res.setHeader = (k, v) => { res.headers[k.toLowerCase()] = v; return res; };
  res.status = (c) => { res.statusCode = c; return res; };
  res.json = (o) => { res.jsonBody = o; res.body = JSON.stringify(o); return res; };
  res.send = (b) => { res.body = b; return res; };
  return res;
}

function upstream(handler) {
  return new Promise((resolve) => {
    const server = http.createServer(handler);
    server.listen(0, "127.0.0.1", () => resolve(server));
  });
}

test("query is rebuilt from a whitelist", () => {
  assert.equal(buildQuery({}), "");
  assert.equal(buildQuery({ limit: "10" }), "?limit=10");
  assert.equal(buildQuery({ limit: "100000" }), "?limit=200");
  assert.equal(buildQuery({ limit: "abc" }), "");
  assert.equal(buildQuery({ status: "manual_review,conflict" }),
    "?status=manual_review%2Cconflict");
  // anything that is not a status list is dropped
  assert.equal(buildQuery({ status: "../../etc/passwd" }), "");
});

test("refuses without a dashboard token configured", async () => {
  delete process.env.DASHBOARD_TOKEN;
  const res = fakeRes();
  await proxy({ headers: {} }, res, "/api/status");
  assert.equal(res.statusCode, 503);
});

test("refuses a wrong or missing dashboard token", async () => {
  process.env.DASHBOARD_TOKEN = "right";
  for (const presented of [undefined, "", "wrong"]) {
    const res = fakeRes();
    const headers = presented === undefined ? {} : { "x-dashboard-token": presented };
    await proxy({ headers }, res, "/api/status");
    assert.equal(res.statusCode, 401);
  }
});

test("refuses when the backend is not configured", async () => {
  process.env.DASHBOARD_TOKEN = "right";
  delete process.env.MIRROR_API_URL;
  delete process.env.MIRROR_API_TOKEN;
  const res = fakeRes();
  await proxy({ headers: { "x-dashboard-token": "right" } }, res, "/api/status");
  assert.equal(res.statusCode, 503);
});

test("proxies with the backend token and never returns it", async () => {
  let seenAuth = null;
  let seenPath = null;
  const server = await upstream((req, res) => {
    seenAuth = req.headers.authorization;
    seenPath = req.url;
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ paper_only: true }));
  });
  const port = server.address().port;

  process.env.DASHBOARD_TOKEN = "right";
  process.env.MIRROR_API_URL = `http://127.0.0.1:${port}`;
  process.env.MIRROR_API_TOKEN = "backend-secret";

  const res = fakeRes();
  await proxy({ headers: { "x-dashboard-token": "right" }, query: { limit: "5" } },
    res, "/api/events");

  assert.equal(res.statusCode, 200);
  assert.equal(seenAuth, "Bearer backend-secret");
  assert.equal(seenPath, "/api/events?limit=5");
  assert.ok(!res.body.includes("backend-secret"));
  assert.deepEqual(JSON.parse(res.body), { paper_only: true });

  server.close();
});

test("reports an unreachable backend without leaking its address", async () => {
  process.env.DASHBOARD_TOKEN = "right";
  process.env.MIRROR_API_URL = "http://127.0.0.1:1";
  process.env.MIRROR_API_TOKEN = "backend-secret";

  const res = fakeRes();
  await proxy({ headers: { "x-dashboard-token": "right" } }, res, "/api/status");
  assert.equal(res.statusCode, 502);
  assert.equal(res.jsonBody.error, "backend unreachable");
  assert.ok(!res.body.includes("backend-secret"));
  assert.ok(!res.body.includes("127.0.0.1"));
});

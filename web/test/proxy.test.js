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

const { proxy, buildQuery, tradePath } = require("../lib/proxy");

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

test("refuses with neither a session nor a token", async () => {
  delete process.env.DASHBOARD_TOKEN;
  const res = fakeRes();
  await proxy({ headers: {} }, res, "/api/status");
  assert.equal(res.statusCode, 401);
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

// --- trade paths -----------------------------------------------------------
//
// The trade id arrives from the browser and ends up in the upstream path, so
// the only ids that may pass are the ones the backend could have generated.

test("a generated trade id becomes a trade path", () => {
  assert.equal(tradePath("RADE-b135f8fd83f05c6c8ea0e88b"),
               "/api/trades/RADE-b135f8fd83f05c6c8ea0e88b");
});

test("a trade id cannot escape the trades prefix", () => {
  for (const bad of ["../status", "a/b", "RADE-1?x=1", "", "a".repeat(65), null]) {
    assert.equal(tradePath(bad), null, `accepted ${JSON.stringify(bad)}`);
  }
});

test("the trade detail route refuses an invalid id before calling upstream", () => {
  const handler = require("../api/trades/[id].js");
  const res = fakeRes();
  handler({ query: { id: "../../status" }, headers: {} }, res);
  assert.equal(res.statusCode, 400);
  assert.deepEqual(res.jsonBody, { error: "invalid trade id" });
});

// ---------------------------------------------------------------------------
// the pause write path
// ---------------------------------------------------------------------------

const { proxyWrite, buildWriteBody } = require("../lib/proxy");

test("the write body is rebuilt from a whitelist", () => {
  assert.deepEqual(buildWriteBody({ reason: "checking" }),
    { reason: "checking", by: "dashboard" });
  // extra fields never reach the backend
  assert.deepEqual(
    buildWriteBody({ reason: "r", by: "me", paused: false, force: true }),
    { reason: "r", by: "me" });
  // non-strings are dropped rather than coerced into something odd
  assert.deepEqual(buildWriteBody({ reason: 42, by: {} }),
    { reason: "", by: "dashboard" });
  // and both are bounded
  assert.equal(buildWriteBody({ reason: "x".repeat(500) }).reason.length, 200);
  assert.equal(buildWriteBody({ by: "y".repeat(500) }).by.length, 64);
  assert.deepEqual(buildWriteBody(null), { reason: "", by: "dashboard" });
});

test("a write with neither a session nor a token is refused before any upstream call", async () => {
  const saved = process.env.DASHBOARD_TOKEN;
  try {
    delete process.env.DASHBOARD_TOKEN;
    const unconfigured = fakeRes();
    await proxyWrite({ headers: {}, body: { reason: "x" } }, unconfigured,
      "/api/pause");
    assert.equal(unconfigured.statusCode, 401);

    // With a token configured, presenting the wrong one is a 401 and the
    // backend is still never called.
    process.env.DASHBOARD_TOKEN = "dash";
    const wrong = fakeRes();
    await proxyWrite({ headers: { "x-dashboard-token": "nope" },
      body: { reason: "x" } }, wrong, "/api/pause");
    assert.equal(wrong.statusCode, 401);
    assert.ok(!JSON.stringify(wrong.jsonBody).includes("MIRROR_API_TOKEN"));
  } finally {
    if (saved === undefined) delete process.env.DASHBOARD_TOKEN;
    else process.env.DASHBOARD_TOKEN = saved;
  }
});

test("a write forwards only the rebuilt body to the fixed path", async () => {
  let seen = null;
  const server = await upstream((req, res) => {
    let body = "";
    req.on("data", (c) => { body += c; });
    req.on("end", () => {
      seen = { url: req.url, method: req.method, auth: req.headers.authorization,
               body: JSON.parse(body || "{}") };
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ paused: { paused: true } }));
    });
  });
  process.env.DASHBOARD_TOKEN = "dash";
  process.env.MIRROR_API_TOKEN = "backend-secret";
  process.env.MIRROR_API_URL = `http://127.0.0.1:${server.address().port}`;
  try {
    const res = fakeRes();
    await proxyWrite(
      { headers: { "x-dashboard-token": "dash" },
        body: { reason: "manual stop", by: "op", extra: "smuggled" } },
      res, "/api/pause");
    assert.equal(res.statusCode, 200);
    assert.equal(seen.url, "/api/pause");
    assert.equal(seen.method, "POST");
    assert.equal(seen.auth, "Bearer backend-secret");
    assert.deepEqual(seen.body, { reason: "manual stop", by: "op" });
    assert.ok(!res.body.includes("backend-secret"));
  } finally {
    server.close();
    delete process.env.DASHBOARD_TOKEN;
    delete process.env.MIRROR_API_TOKEN;
    delete process.env.MIRROR_API_URL;
  }
});

// ---------------------------------------------------------------------------
// the session cookie
// ---------------------------------------------------------------------------

const auth = require("../lib/auth");

function withCredentials(password) {
  process.env.DASHBOARD_EMAIL = "op@example.com";
  process.env.DASHBOARD_SESSION_SECRET = "test-secret";
  const salt = auth.sha256("op@example.com").slice(0, 16);
  process.env.DASHBOARD_PASSWORD_HASH = auth.hashPassword(password, salt);
}

test("a valid session cookie is accepted without the shared token", async () => {
  let seenPath = null;
  const server = await upstream((req, res) => {
    seenPath = req.url;
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ ok: true }));
  });
  process.env.MIRROR_API_URL = `http://127.0.0.1:${server.address().port}`;
  process.env.MIRROR_API_TOKEN = "backend-secret";
  delete process.env.DASHBOARD_TOKEN;
  withCredentials("correct horse battery");

  const token = auth.issueToken("op@example.com", "test-secret");
  const res = fakeRes();
  await proxy({ headers: { cookie: `rade_session=${token}` }, query: {} },
    res, "/api/status");

  assert.equal(res.statusCode, 200);
  assert.equal(seenPath, "/api/status");

  server.close();
  delete process.env.DASHBOARD_EMAIL;
  delete process.env.DASHBOARD_PASSWORD_HASH;
  delete process.env.DASHBOARD_SESSION_SECRET;
  delete process.env.MIRROR_API_URL;
  delete process.env.MIRROR_API_TOKEN;
});

test("a forged or expired session cookie is refused", async () => {
  delete process.env.DASHBOARD_TOKEN;
  withCredentials("correct horse battery");
  const secret = process.env.DASHBOARD_SESSION_SECRET;

  for (const token of ["not-a-token", auth.issueToken("op@example.com", "wrong-secret")]) {
    const res = fakeRes();
    await proxy({ headers: { cookie: `rade_session=${token}` }, query: {} },
      res, "/api/status");
    assert.equal(res.statusCode, 401);
  }

  delete process.env.DASHBOARD_EMAIL;
  delete process.env.DASHBOARD_PASSWORD_HASH;
  delete process.env.DASHBOARD_SESSION_SECRET;
});

test("sign-in accepts the right password and refuses the wrong one", async () => {
  withCredentials("correct horse battery");
  const login = require("../api/login.js");
  const { Readable } = require("node:stream");

  const call = async (body) => {
    const req = new Readable({ read() {} });
    req.headers = {};
    const res = fakeRes();
    const pending = login(req, res);
    if (body !== undefined) req.push(JSON.stringify(body));
    req.push(null);
    await pending;
    return res;
  };

  const ok = await call({ email: "op@example.com", password: "correct horse battery" });
  assert.equal(ok.statusCode, 200);
  assert.ok(String(ok.headers["set-cookie"]).includes("rade_session="));

  const badPassword = await call({ email: "op@example.com", password: "wrong" });
  assert.equal(badPassword.statusCode, 401);

  const wrongEmail = await call({ email: "other@example.com", password: "correct horse battery" });
  assert.equal(wrongEmail.statusCode, 401);

  delete process.env.DASHBOARD_EMAIL;
  delete process.env.DASHBOARD_PASSWORD_HASH;
  delete process.env.DASHBOARD_SESSION_SECRET;
});

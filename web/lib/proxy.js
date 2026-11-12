// Server-side proxy for the dashboard.
//
// The browser never sees MIRROR_API_TOKEN or the Supabase key. It sends a
// dashboard token, this function checks it, and only then calls the Render
// backend with the backend token. Nothing secret is ever returned to the
// client, and the query string is rebuilt from a whitelist rather than passed
// through.

const crypto = require("crypto");

const TIMEOUT_MS = 8000;

function safeEqual(a, b) {
  const left = Buffer.from(String(a));
  const right = Buffer.from(String(b));
  if (left.length !== right.length) return false;
  return crypto.timingSafeEqual(left, right);
}

function buildQuery(query) {
  const params = new URLSearchParams();
  const limit = parseInt(query.limit, 10);
  if (Number.isFinite(limit) && limit > 0) params.set("limit", String(Math.min(limit, 200)));
  const status = query.status;
  if (typeof status === "string" && /^[a-z_,]+$/.test(status)) params.set("status", status);
  const suffix = params.toString();
  return suffix ? `?${suffix}` : "";
}

async function proxy(req, res, path) {
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("X-Content-Type-Options", "nosniff");

  const expected = process.env.DASHBOARD_TOKEN || "";
  if (!expected) {
    res.status(503).json({ error: "dashboard token is not configured" });
    return;
  }
  const presented = req.headers["x-dashboard-token"] || "";
  if (!safeEqual(presented, expected)) {
    res.status(401).json({ error: "unauthorised" });
    return;
  }

  const base = (process.env.MIRROR_API_URL || "").replace(/\/+$/, "");
  const apiToken = process.env.MIRROR_API_TOKEN || "";
  if (!base || !apiToken) {
    res.status(503).json({ error: "backend is not configured" });
    return;
  }

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const upstream = await fetch(`${base}${path}${buildQuery(req.query || {})}`, {
      headers: { Authorization: `Bearer ${apiToken}` },
      signal: controller.signal,
    });
    const body = await upstream.text();
    res.status(upstream.status);
    res.setHeader("Content-Type", "application/json");
    res.send(body);
  } catch (err) {
    // No stack, no upstream URL: a free Render instance that is spinning up
    // takes about a minute and this is the expected symptom.
    res.status(502).json({
      error: "backend unreachable",
      detail: err && err.name === "AbortError" ? "timed out" : String(err && err.name),
      hint: "a free Render service spins up in about a minute after idling",
    });
  } finally {
    clearTimeout(timer);
  }
}

module.exports = { proxy, buildQuery, safeEqual };

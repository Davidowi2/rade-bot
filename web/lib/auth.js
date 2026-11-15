// Session and credential checks for the dashboard.
//
// The dashboard is a static site plus serverless functions. There is no
// database for users, so the account lives in the environment:
//
//   DASHBOARD_EMAIL          the sign-in address
//   DASHBOARD_PASSWORD_HASH  salted SHA-256 of the password (hex)
//   DASHBOARD_SESSION_SECRET signs the session cookie
//
// A password is never stored or logged in clear. The session is a signed,
// expiring token in an httpOnly cookie, so it is not readable from script and
// is not kept in localStorage where any XSS could steal it.

const crypto = require("crypto");

const SESSION_COOKIE = "rade_session";
const SESSION_TTL_S = 12 * 60 * 60; // 12 hours

function safeEqual(a, b) {
  const left = Buffer.from(String(a));
  const right = Buffer.from(String(b));
  if (left.length !== right.length) return false;
  return crypto.timingSafeEqual(left, right);
}

function sha256(value) {
  return crypto.createHash("sha256").update(String(value)).digest("hex");
}

// Salted and iterated so a leaked hash is not a leaked password.
function hashPassword(password, salt) {
  let digest = sha256(`${salt}:${password}`);
  for (let i = 0; i < 20000; i += 1) digest = sha256(digest);
  return digest;
}

function credentialsConfigured() {
  const email = process.env.DASHBOARD_EMAIL || "";
  const hash = process.env.DASHBOARD_PASSWORD_HASH || "";
  const secret = process.env.DASHBOARD_SESSION_SECRET || "";
  if (!email || !hash || !secret) return null;
  return { email, hash, secret };
}

function checkPassword(password) {
  const creds = credentialsConfigured();
  if (!creds) return null;
  const salt = sha256(creds.email).slice(0, 16);
  const presented = hashPassword(String(password || ""), salt);
  if (!safeEqual(presented, creds.hash)) return null;
  return creds;
}

function issueToken(email, secret) {
  const payload = Buffer.from(
    JSON.stringify({ e: email, exp: Math.floor(Date.now() / 1000) + SESSION_TTL_S }),
  ).toString("base64url");
  const signature = crypto.createHmac("sha256", secret).update(payload).digest("base64url");
  return `${payload}.${signature}`;
}

function verifyToken(token, secret) {
  const parts = String(token || "").split(".");
  if (parts.length !== 2) return null;
  const [payload, signature] = parts;
  const expected = crypto.createHmac("sha256", secret).update(payload).digest("base64url");
  if (!safeEqual(signature, expected)) return null;
  let data;
  try {
    data = JSON.parse(Buffer.from(payload, "base64url").toString("utf8"));
  } catch (_err) {
    return null;
  }
  if (!data || !data.exp || data.exp < Date.now() / 1000) return null;
  return data.e || null;
}

function parseCookies(header) {
  const out = {};
  for (const part of String(header || "").split(";")) {
    const index = part.indexOf("=");
    if (index === -1) continue;
    out[part.slice(0, index).trim()] = decodeURIComponent(part.slice(index + 1).trim());
  }
  return out;
}

// The signed-in address from the cookie, or null.
function sessionEmail(req) {
  const creds = credentialsConfigured();
  if (!creds) return null;
  const cookies = parseCookies(req.headers.cookie);
  return verifyToken(cookies[SESSION_COOKIE], creds.secret);
}

function sessionCookie(token) {
  const secure = process.env.VERCEL === "1" ? "; Secure" : "";
  return `${SESSION_COOKIE}=${token}; HttpOnly; SameSite=Lax; Path=/; Max-Age=${SESSION_TTL_S}${secure}`;
}

function clearCookie() {
  return `${SESSION_COOKIE}=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0`;
}

module.exports = {
  SESSION_COOKIE,
  checkPassword,
  clearCookie,
  credentialsConfigured,
  hashPassword,
  issueToken,
  safeEqual,
  sessionCookie,
  sessionEmail,
  sha256,
  verifyToken,
};

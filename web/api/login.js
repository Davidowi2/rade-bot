// POST /api/login — exchange the seeded credentials for a session cookie.
//
// The password is compared against a salted hash in the environment and is
// never logged, echoed or stored. On success the response carries an httpOnly
// cookie; the password is not in the body.

const { checkPassword, credentialsConfigured, issueToken, sessionCookie } = require("../lib/auth");

function readBody(req) {
  return new Promise((resolve) => {
    let raw = "";
    req.on("data", (chunk) => {
      raw += chunk;
      if (raw.length > 4096) req.destroy();
    });
    req.on("end", () => {
      try {
        resolve(raw ? JSON.parse(raw) : {});
      } catch (_err) {
        resolve({});
      }
    });
  });
}

module.exports = async function login(req, res) {
  res.setHeader("Cache-Control", "no-store");
  res.setHeader("X-Content-Type-Options", "nosniff");

  const creds = credentialsConfigured();
  if (!creds) {
    res.status(503).json({ error: "sign-in is not configured on this deployment" });
    return;
  }

  const body = await readBody(req);
  const email = typeof body.email === "string" ? body.email.trim().toLowerCase() : "";
  const password = typeof body.password === "string" ? body.password : "";

  if (!email || !password) {
    res.status(400).json({ error: "email and password are required" });
    return;
  }

  const matched = checkPassword(password);
  if (!matched || email !== matched.email.toLowerCase()) {
    // Deliberately the same answer for a wrong email and a wrong password.
    res.status(401).json({ error: "invalid credentials" });
    return;
  }

  res.setHeader("Set-Cookie", sessionCookie(issueToken(matched.email, matched.secret)));
  res.status(200).json({ ok: true, email: matched.email });
};

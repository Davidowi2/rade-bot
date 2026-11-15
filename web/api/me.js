// GET /api/me — who is signed in. The dashboard calls this on load; a 401
// means the session has expired and the login screen should be shown.

const { sessionEmail } = require("../lib/auth");

module.exports = function me(req, res) {
  res.setHeader("Cache-Control", "no-store");
  const email = sessionEmail(req);
  if (!email) {
    res.status(401).json({ error: "not signed in" });
    return;
  }
  res.status(200).json({ email });
};

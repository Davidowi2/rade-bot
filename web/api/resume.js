const { proxyWrite } = require("../lib/proxy");

// Resume execution. Symmetric with pause, and just as narrow: one fixed
// upstream path, one boolean.
module.exports = (req, res) => proxyWrite(req, res, "/api/resume");

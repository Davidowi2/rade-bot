const { proxyWrite } = require("../lib/proxy");

// Pause execution. This is the only kind of write the dashboard can make, and
// it reaches exactly one upstream path: a boolean that gates new work. It
// cannot place, modify or close anything at the broker.
module.exports = (req, res) => proxyWrite(req, res, "/api/pause");

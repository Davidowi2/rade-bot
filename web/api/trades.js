const { proxy } = require("../lib/proxy");

// The trade list. The query string is rebuilt by the proxy from a whitelist,
// so a caller cannot smuggle parameters through to the backend.
module.exports = (req, res) => proxy(req, res, "/api/trades");

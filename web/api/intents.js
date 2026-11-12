const { proxy } = require("../lib/proxy");

module.exports = (req, res) => proxy(req, res, "/api/intents");

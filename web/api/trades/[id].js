const { proxy, tradePath } = require("../../lib/proxy");

// One trade and its transitions. The id is validated before it is placed in
// the upstream path: the browser must not be able to address anything other
// than a trade.
module.exports = (req, res) => {
  const path = tradePath(req.query && req.query.id);
  if (!path) {
    res.status(400).json({ error: "invalid trade id" });
    return;
  }
  return proxy(req, res, path);
};

// Tests for the dashboard's inline script and structure.
//
// The proxy tests cover the serverless functions; nothing covered the page
// itself. A syntax error in the inline script leaves a completely blank page
// with a healthy HTTP 200, which is exactly what shipped once.

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf8");

function inlineScript() {
  const match = html.match(/<script>([\s\S]*?)<\/script>/);
  assert.ok(match, "index.html has no inline <script> block");
  return match[1];
}

test("the inline script parses", () => {
  // new Function parses without executing, so this catches a syntax error
  // without needing a DOM.
  assert.doesNotThrow(() => new Function(inlineScript()),
    "the inline script has a syntax error; the page would render blank");
});

test("every panel the code looks up exists in the markup", () => {
  // getElementById returns null on a typo and the render throws at runtime,
  // which again reads as a blank page rather than a failure.
  const ids = [...inlineScript().matchAll(/getElementById\("([^"]+)"\)/g)]
    .map((m) => m[1]);
  assert.ok(ids.length > 10, "expected the script to look up several elements");
  for (const id of new Set(ids)) {
    assert.ok(html.includes(`id="${id}"`), `#${id} is referenced but not in the markup`);
  }
});

test("the password is never kept in localStorage", () => {
  assert.ok(!/localStorage/.test(inlineScript()),
    "a session belongs in an httpOnly cookie, not in script-readable storage");
});

test("the intake panel reports the stop width the ceiling allows", () => {
  // The mismatch between the lot and the ceiling is invisible otherwise.
  assert.ok(inlineScript().includes("max_stop_distance"),
    "the dashboard must surface max_stop_distance");
  assert.ok(html.includes("Intake readiness"),
    "the intake panel heading is missing");
});

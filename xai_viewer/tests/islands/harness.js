// Shared pieces for the island checks: locate the shipped script, stub just enough DOM,
// report results. Dependency-free on purpose (see README.md) - node's built-ins only.

const fs = require("fs");
const vm = require("vm");

// ── reporting ────────────────────────────────────────────────────────────────

const failures = [];

function section(title) {
  console.log(title);
}

function check(ok, label, detail) {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${label}${detail ? ` — ${detail}` : ""}`);
  if (!ok) failures.push(label);
  return ok;
}

function finish() {
  if (failures.length) {
    console.log(`\n${failures.length} check(s) failed: ${failures.join(", ")}`);
    process.exit(1);
  }
  console.log("\nall island checks passed");
}

// ── locating the shipped script ──────────────────────────────────────────────

/** Bodies of all attribute-less <script> blocks (skips src= and the JSON payloads). */
function extractScripts(html) {
  return [...html.matchAll(/<script>\n([\s\S]*?)<\/script>/g)].map((m) => m[1]);
}

/**
 * The one script block containing `marker` - islands are identified by a stable token
 * (e.g. "__xaiScrollSync") rather than by position, so reordering templates cannot
 * silently point a check at the wrong block.
 */
function pickScript(file, marker) {
  const blocks = extractScripts(fs.readFileSync(file, "utf8")).filter((b) => b.includes(marker));
  if (blocks.length !== 1) {
    console.log(`FAIL  expected exactly one script block containing "${marker}", found ${blocks.length}`);
    process.exit(1);
  }
  return blocks[0];
}

// ── DOM stubs ────────────────────────────────────────────────────────────────

/** Real classList semantics - the link toggle relies on toggle()'s return value. */
class ClassList {
  constructor(initial = []) {
    this._set = new Set(initial);
  }
  add(c) { this._set.add(c); }
  remove(c) { this._set.delete(c); }
  contains(c) { return this._set.has(c); }
  toggle(c, force) {
    if (force === undefined) {
      if (this._set.has(c)) { this._set.delete(c); return false; }
      this._set.add(c);
      return true;
    }
    if (force) this._set.add(c); else this._set.delete(c);
    return !!force;
  }
  toString() { return [...this._set].join(" "); }
}

/**
 * Element stub. `scrollTop` counts writes: the echo guard is only observable as
 * "was it written again?", so the counter is what makes that testable.
 * The scroll-sync island does `el instanceof Element`, hence a real class.
 */
class Element {
  constructor({ classes = [], dataset = {}, scrollHeight = 0, clientHeight = 0, scrollTop = 0,
                tagName = "DIV" } = {}) {
    this.classList = new ClassList(classes);
    this.dataset = dataset;
    this.scrollHeight = scrollHeight;
    this.clientHeight = clientHeight;
    this.tagName = tagName;
    this.title = "";
    this.writes = 0;
    this._scrollTop = Math.round(scrollTop);
  }
  get scrollTop() { return this._scrollTop; }
  /**
   * Browsers store scrollTop snapped to whole pixels. That rounding is not cosmetic here:
   * a panel with a small scroll range answers a written position with a *different*
   * fraction than it was given - which is precisely what the echo guard exists for.
   * Without rounding in the stub, removing the guard would go unnoticed (verified by
   * mutating base.html), so this is what makes the guard observable at all.
   */
  set scrollTop(v) { this._scrollTop = Math.round(v); this.writes++; }
  addEventListener() {}
}

/**
 * Document stub. `elements` maps id -> element; `query(selector)` answers
 * querySelector/querySelectorAll. Registered listeners are exposed via `listeners`
 * so a check can fire them directly and assert how many were registered.
 */
function makeDocument({ elements = {}, query = () => [] } = {}) {
  const listeners = {};      // type -> [fn]
  const bodyListeners = {};
  const document = {
    getElementById: (id) => elements[id] || null,
    querySelector: (sel) => query(sel)[0] || null,
    querySelectorAll: (sel) => query(sel),
    addEventListener: (type, fn) => (listeners[type] = listeners[type] || []).push(fn),
    body: {
      addEventListener: (type, fn) => (bodyListeners[type] = bodyListeners[type] || []).push(fn),
    },
  };
  return { document, listeners, bodyListeners };
}

/** Evaluates `script` in a fresh context built from `sandbox`; returns the context object. */
function run(script, sandbox) {
  const ctx = { console, JSON, Element, ...sandbox };
  vm.runInContext(script, vm.createContext(ctx), { filename: "island.js" });
  return ctx;
}

module.exports = { section, check, finish, extractScripts, pickScript, ClassList, Element,
                   makeDocument, run };

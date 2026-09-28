// Coupled gallery scrolling (base.html, "__xaiScrollSync") - the highest-risk island:
// echo guard, proportional mapping, restore across HTMX swaps.
//
// Usage: node check_scroll_sync.js <rendered-workspace.html>
//
// Listeners are invoked directly, so this covers the LOGIC, not whether the browser really
// delivers scroll events in the capture phase. See README.md.

const { section, check, finish, pickScript, Element, run } = require("./harness");

const script = pickScript(process.argv[2], "__xaiScrollSync");

/** A workspace with two galleries of different height → proportional mapping is observable. */
function makeWorld({ aLinked = "1", bLinked = "1", aScrollTop = 0, bScrollHeight = 2000 } = {}) {
  // A: scrollable range 500 (1000-500). B: range 1500 (2000-500) unless overridden.
  const a = new Element({ classes: ["panel-gallery"], scrollHeight: 1000, clientHeight: 500,
                          scrollTop: aScrollTop, dataset: { panelId: "1", linked: aLinked } });
  const b = new Element({ classes: ["panel-gallery"], scrollHeight: bScrollHeight, clientHeight: 500,
                          dataset: { panelId: "2", linked: bLinked } });
  const galleries = [a, b];
  const query = (sel) => (sel.includes('[data-linked="1"]')
    ? galleries.filter((g) => g.dataset.linked === "1")
    : galleries.filter((g) => g.classList.contains("panel-gallery")));

  const listeners = {};
  const bodyListeners = {};
  const window = {};
  const document = {
    getElementById: () => null,
    querySelector: (sel) => query(sel)[0] || null,
    querySelectorAll: query,
    addEventListener: (type, fn) => (listeners[type] = listeners[type] || []).push(fn),
    body: { addEventListener: (type, fn) => (bodyListeners[type] = bodyListeners[type] || []).push(fn) },
  };
  run(script, { window, document });
  const fireScroll = (el) => listeners.scroll.forEach((fn) => fn({ target: el }));
  const fireSwap = () => (bodyListeners["htmx:afterSwap"] || []).forEach((fn) => fn());
  return { a, b, galleries, fireScroll, fireSwap, listeners, bodyListeners, window, document, query };
}

section("proportional mapping (panels differ in image count → fraction, not pixels)");
{
  const w = makeWorld();
  w.a.scrollTop = 250;            // 250/500 = 50%
  w.a.writes = 0;
  w.fireScroll(w.a);
  check(w.b.scrollTop === 750, "B follows A proportionally", `B.scrollTop=${w.b.scrollTop} (expected 750)`);
}

section("echo guard (a programmatic scroll must not bounce back)");
{
  // B holds few images → scroll range of just 3px. A at 50% maps to 1.5px, which the browser
  // snaps to 2px = 67% of B's range. B's echo event therefore reports a DIFFERENT fraction
  // than A sent. Without the guard that echo yanks A from 250 to ~333 — the feedback loop.
  // (With equal proportions an echo is harmless and the 1px threshold already absorbs it,
  // so this asymmetric setup is what actually exercises the guard.)
  const w = makeWorld({ bScrollHeight: 503 });
  w.a.scrollTop = 250;            // 50% of A
  w.fireScroll(w.a);
  check(w.b.scrollTop === 2, "B moved to its rounded proportional position",
        `B.scrollTop=${w.b.scrollTop} (1.5px → 2px)`);
  const aBefore = w.a.scrollTop;
  const writesBefore = w.a.writes;
  w.fireScroll(w.b);              // the browser's echo event for B
  check(w.a.scrollTop === aBefore, "echo does not yank A to B's rounded fraction",
        `A.scrollTop ${aBefore} → ${w.a.scrollTop} (unguarded would be ~333)`);
  check(w.a.writes === writesBefore, "A is not written at all",
        `A.writes ${writesBefore} → ${w.a.writes}`);
}

section("already-in-place threshold (no write → no echo to suppress)");
{
  const w = makeWorld();
  w.b.scrollTop = 750;            // B already where A would put it
  w.b.writes = 0;
  w.a.scrollTop = 250;
  w.fireScroll(w.a);
  check(w.b.writes === 0, "B is not written when within 1px of target", `B.writes=${w.b.writes}`);
}

section("unlinked panels");
{
  const w = makeWorld({ aLinked: "0" });
  w.b.writes = 0;
  w.a.scrollTop = 250;
  w.fireScroll(w.a);
  check(w.b.writes === 0, "scrolling an unlinked panel moves nothing", `B.writes=${w.b.writes}`);
}

section("non-gallery elements are ignored");
{
  const w = makeWorld();
  const other = new Element({ classes: ["something-else"], scrollHeight: 100, clientHeight: 10 });
  w.b.writes = 0;
  let threw = null;
  try { w.fireScroll(other); } catch (e) { threw = e; }
  check(!threw, "no crash on a foreign scroll event", threw ? `${threw.name}: ${threw.message}` : "");
  check(w.b.writes === 0, "galleries untouched", `B.writes=${w.b.writes}`);
}

section("no scrollable range (gallery fits → division by zero would yield NaN)");
{
  const w = makeWorld();
  w.a.scrollHeight = 500;         // == clientHeight → max = 0
  w.a.scrollTop = 0;
  w.fireScroll(w.a);
  check(w.b.scrollTop === 0 && !Number.isNaN(w.b.scrollTop), "fraction is 0, not NaN",
        `B.scrollTop=${w.b.scrollTop}`);
}

section("restore across HTMX swaps (galleries are replaced by new nodes)");
{
  const w = makeWorld();
  w.a.scrollTop = 250;            // 50%
  w.fireScroll(w.a);
  // HTMX replaced the markup: same panel ids, fresh elements scrolled to the top.
  w.galleries.length = 0;
  const a2 = new Element({ classes: ["panel-gallery"], scrollHeight: 1000, clientHeight: 500,
                           dataset: { panelId: "1", linked: "1" } });
  const b2 = new Element({ classes: ["panel-gallery"], scrollHeight: 2000, clientHeight: 500,
                           dataset: { panelId: "2", linked: "1" } });
  w.galleries.push(a2, b2);
  w.fireSwap();
  check(a2.scrollTop === 250, "P1 restored to its fraction", `a2.scrollTop=${a2.scrollTop}`);
  check(b2.scrollTop === 750, "P2 restored to its fraction", `b2.scrollTop=${b2.scrollTop}`);
  // The restore writes scrollTop, which the browser answers with a scroll event; that echo
  // must not re-broadcast a stale position to the other panel.
  const writes = b2.writes;
  w.fireScroll(a2);
  check(b2.writes === writes, "restore echo does not re-broadcast", `b2.writes ${writes} → ${b2.writes}`);
}

section("idempotence guard (one listener, however often the block runs)");
{
  const w = makeWorld();
  const before = w.listeners.scroll.length;
  run(script, { window: w.window, document: w.document });   // same window → guard must bite
  check(w.listeners.scroll.length === before, "no second scroll listener",
        `listeners: ${before} → ${w.listeners.scroll.length}`);
}

finish();

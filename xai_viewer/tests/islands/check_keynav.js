// Arrow-key stepping (base.html, "__xaiKeyNav"): gate logic around htmx.ajax.
//
// Usage: node check_keynav.js <rendered-workspace.html>
//
// The gate matters because the app is full of text inputs (note title/description): arrow
// keys must move the caret there, not step every linked panel to the next image.

const { section, check, finish, pickScript, run } = require("./harness");

const script = pickScript(process.argv[2], "__xaiKeyNav");

const LINKED = { linked_single_panel_ids: [1, 2] };

/** Builds a world and returns a `press` helper that reports what the island did. */
function makeWorld({ pageData = JSON.stringify(LINKED), hasPageData = true } = {}) {
  const calls = [];
  const listeners = {};
  const window = {};
  const document = {
    getElementById: (id) =>
      (id === "data-page" && hasPageData ? { textContent: pageData } : null),
    addEventListener: (type, fn) => (listeners[type] = listeners[type] || []).push(fn),
    body: { addEventListener() {} },
  };
  const htmx = { ajax: (method, url, opts) => calls.push({ method, url, opts }) };
  run(script, { window, document, htmx });

  function press(key, target = null) {
    calls.length = 0;
    let prevented = false;
    const event = { key, target, preventDefault: () => { prevented = true; } };
    (listeners.keydown || []).forEach((fn) => fn(event));
    return { calls, prevented };
  }
  return { press, listeners, window, document, htmx };
}

section("stepping direction");
{
  const w = makeWorld();
  const right = w.press("ArrowRight");
  check(right.calls.length === 1 && right.calls[0].url === "/step-all/next"
        && right.calls[0].method === "POST", "ArrowRight → POST /step-all/next",
        JSON.stringify(right.calls.map((c) => `${c.method} ${c.url}`)));
  const left = w.press("ArrowLeft");
  check(left.calls.length === 1 && left.calls[0].url === "/step-all/prev",
        "ArrowLeft → POST /step-all/prev", JSON.stringify(left.calls.map((c) => c.url)));
  check(right.prevented && left.prevented, "default prevented when stepping");
  const swap = right.calls[0] && right.calls[0].opts;
  check(!!swap && swap.target === "#workspace" && swap.swap === "outerHTML",
        "swaps the workspace", JSON.stringify(swap));
}

section("keys we must not hijack");
{
  const w = makeWorld();
  for (const key of ["a", "Enter", "ArrowUp", "ArrowDown", " "]) {
    const r = w.press(key);
    check(r.calls.length === 0 && !r.prevented, `"${key}" ignored`);
  }
}

section("focus gate (typing must not step panels)");
{
  const w = makeWorld();
  for (const tagName of ["INPUT", "TEXTAREA", "SELECT"]) {
    const r = w.press("ArrowRight", { tagName });
    check(r.calls.length === 0 && !r.prevented, `focus in <${tagName.toLowerCase()}> → no stepping`);
  }
  const editable = w.press("ArrowRight", { tagName: "DIV", isContentEditable: true });
  check(editable.calls.length === 0, "contenteditable → no stepping");
  const plain = w.press("ArrowRight", { tagName: "DIV" });
  check(plain.calls.length === 1, "focus on a plain element → stepping works");
}

section("state gate");
{
  const none = makeWorld({ pageData: JSON.stringify({ linked_single_panel_ids: [] }) });
  check(none.press("ArrowRight").calls.length === 0, "no linked single panels → no request");

  const missingKey = makeWorld({ pageData: JSON.stringify({ panel_count: 2 }) });
  check(missingKey.press("ArrowRight").calls.length === 0, "key absent → no request");

  const noData = makeWorld({ hasPageData: false });
  check(noData.press("ArrowRight").calls.length === 0, "no #data-page (e.g. login page) → no request");

  const broken = makeWorld({ pageData: "{not json" });
  let threw = null;
  let calls = [];
  try { calls = broken.press("ArrowRight").calls; } catch (e) { threw = e; }
  check(!threw, "malformed page data does not throw", threw ? `${threw.name}: ${threw.message}` : "");
  check(calls.length === 0, "malformed page data → no request");
}

section("idempotence guard (one listener, however often the block runs)");
{
  const w = makeWorld();
  const before = w.listeners.keydown.length;
  run(script, { window: w.window, document: w.document, htmx: w.htmx });
  check(w.listeners.keydown.length === before, "no second keydown listener",
        `listeners: ${before} → ${w.listeners.keydown.length}`);
  const r = w.press("ArrowRight");
  check(r.calls.length === 1, "still exactly one request per keypress", `calls=${r.calls.length}`);
}

finish();

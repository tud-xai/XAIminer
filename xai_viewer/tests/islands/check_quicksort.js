// Quick-sort keys (base.html, "__xaiQuickSort"): A/D file the left panel's image, Ctrl+Z undoes.
//
// Usage: node check_quicksort.js <rendered-workspace.html>
//
// What matters: the keys must never fire while typing (and Ctrl+Z must stay the text field's own
// undo there), never behind an open dialog, and the undo stack must hand the server exactly the
// filing that the last toast reported - newest first.

const { section, check, finish, pickScript, run, ClassList } = require("./harness");

const script = pickScript(process.argv[2], "__xaiQuickSort");

/** A world with a toast slot whose content the fake server response replaces. */
function makeWorld({ hasPageData = true, modalHtml = "", zoomOpen = false } = {}) {
  const calls = [];
  const listeners = {};
  const timers = [];
  const wrap = { classList: new ClassList(["d-none"]) };
  const toast = { dataset: {} };
  let nextUndo = null;                    // what the next response's toast carries
  const elements = {
    "quicksort-toast-wrap": wrap,
    "quicksort-toast": toast,
    "modal-container": { innerHTML: modalHtml },
    "image-zoom": { open: zoomOpen },
  };
  if (hasPageData) elements["data-page"] = { textContent: "{}" };
  const window = {};
  const document = {
    getElementById: (id) => elements[id] || null,
    addEventListener: (type, fn) => (listeners[type] = listeners[type] || []).push(fn),
    body: { addEventListener() {} },
  };
  const htmx = {
    ajax: (method, url, opts) => {
      calls.push({ method, url, opts });
      // The response replaces the toast element: data-undo only when a filing happened.
      toast.dataset = nextUndo ? { undo: JSON.stringify(nextUndo) } : {};
      nextUndo = null;
      return { then: (fn) => fn() };      // settles synchronously in the stub
    },
  };
  const setTimeout = (fn, ms) => { timers.push({ fn, ms, cleared: false }); return timers.length; };
  const clearTimeout = (id) => { if (id) timers[id - 1].cleared = true; };
  run(script, { window, document, htmx, setTimeout, clearTimeout });

  function press(key, { target = null, ctrlKey = false, metaKey = false, altKey = false,
                        shiftKey = false, repeat = false } = {}) {
    calls.length = 0;
    let prevented = false;
    const event = { key, target, ctrlKey, metaKey, altKey, shiftKey, repeat,
                    preventDefault: () => { prevented = true; } };
    (listeners.keydown || []).forEach((fn) => fn(event));
    return { calls, prevented };
  }
  return { press, listeners, window, document, htmx, toast, wrap, timers, elements,
           respondWith: (undo) => { nextUndo = undo; } };
}

section("A / D file the image");
{
  const w = makeWorld();
  const a = w.press("a");
  check(a.calls.length === 1 && a.calls[0].url === "/quicksort/keep" && a.calls[0].method === "POST",
        "a → POST /quicksort/keep", JSON.stringify(a.calls.map((c) => c.url)));
  check(a.prevented, "default prevented");
  const d = w.press("D");
  check(d.calls.length === 1 && d.calls[0].url === "/quicksort/drop", "D (caps) → POST /quicksort/drop");
  const opts = d.calls[0] && d.calls[0].opts;
  check(!!opts && opts.target === "#workspace" && opts.swap === "outerHTML", "swaps the workspace",
        JSON.stringify(opts));
}

section("keys we must not hijack");
{
  const w = makeWorld();
  for (const [key, mods, label] of [
    ["a", { ctrlKey: true }, "Ctrl+A (select all)"],
    ["d", { ctrlKey: true }, "Ctrl+D (bookmark)"],
    ["a", { metaKey: true }, "Cmd+A"],
    ["a", { altKey: true }, "Alt+A"],
    ["a", { repeat: true }, "held-down a (auto-repeat)"],
    ["s", {}, "s"],
    ["z", {}, "plain z"],
  ]) {
    const r = w.press(key, mods);
    check(r.calls.length === 0 && !r.prevented, `${label} ignored`);
  }
}

section("typing gate (A/D and Ctrl+Z belong to the text field)");
{
  const w = makeWorld();
  for (const tagName of ["INPUT", "TEXTAREA", "SELECT"]) {
    const r = w.press("a", { target: { tagName } });
    check(r.calls.length === 0 && !r.prevented, `a in <${tagName.toLowerCase()}> → typed, not filed`);
    const z = w.press("z", { target: { tagName }, ctrlKey: true });
    check(z.calls.length === 0 && !z.prevented, `Ctrl+Z in <${tagName.toLowerCase()}> → native undo`);
  }
  const editable = w.press("d", { target: { tagName: "DIV", isContentEditable: true } });
  check(editable.calls.length === 0, "contenteditable → not filed");
}

section("context gate");
{
  const noPage = makeWorld({ hasPageData: false });
  check(noPage.press("a").calls.length === 0, "no #data-page (not the workspace) → nothing");
  const modal = makeWorld({ modalHtml: "<div class='modal'></div>" });
  check(modal.press("a").calls.length === 0, "dialog open → nothing");
  check(modal.press("z", { ctrlKey: true }).calls.length === 0, "dialog open → no undo either");
  const zoom = makeWorld({ zoomOpen: true });
  check(zoom.press("d").calls.length === 0, "large view open → nothing");
  check(zoom.press("z", { ctrlKey: true }).calls.length === 0, "large view open → no undo either");
}

section("undo stack: newest first, one entry per filing");
{
  const w = makeWorld();
  const first = { image_id: "img1", restore: [{ id: 1, member: false }, { id: 2, member: false }] };
  const second = { image_id: "img2", restore: [{ id: 1, member: false }, { id: 2, member: true }] };
  w.respondWith(first); w.press("a");
  w.press("a");                                   // e.g. "already filed": no undo entry
  w.respondWith(second); w.press("d");

  const u1 = w.press("z", { ctrlKey: true });
  check(u1.prevented && u1.calls.length === 1 && u1.calls[0].url === "/quicksort/undo",
        "Ctrl+Z → POST /quicksort/undo");
  const v1 = u1.calls[0] && u1.calls[0].opts.values;
  check(!!v1 && v1.image_id === "img2" && v1.restore === JSON.stringify(second.restore),
        "first undo reverts the LAST filing", JSON.stringify(v1));
  const u2 = w.press("Z", { ctrlKey: true });     // caps lock
  const v2 = u2.calls[0] && u2.calls[0].opts.values;
  check(!!v2 && v2.image_id === "img1", "second undo reverts the one before", JSON.stringify(v2));
  const u3 = w.press("z", { metaKey: true });
  const v3 = u3.calls[0] && u3.calls[0].opts.values;
  check(u3.calls.length === 1 && !!v3 && !v3.image_id,
        "empty stack → still asks the server (it answers 'nothing to undo')", JSON.stringify(v3));
  check(w.press("z", { ctrlKey: true, shiftKey: true }).calls.length === 0,
        "Ctrl+Shift+Z (redo) is not undo");
}

section("toast: shown, hidden after 3 s, a new one restarts the clock");
{
  const w = makeWorld();
  w.press("a");
  check(!w.wrap.classList.contains("d-none"), "toast visible after the response");
  check(w.timers.length === 1 && w.timers[0].ms === 3000, "hide scheduled after 3 s",
        JSON.stringify(w.timers.map((t) => t.ms)));
  w.press("d");
  check(w.timers[0].cleared && w.timers.length === 2, "second toast cancels the first timer");
  w.timers[1].fn();
  check(w.wrap.classList.contains("d-none"), "hidden when the timer fires");
}

section("idempotence guard (one listener, however often the block runs)");
{
  const w = makeWorld();
  const before = w.listeners.keydown.length;
  run(script, { window: w.window, document: w.document, htmx: w.htmx,
                setTimeout() {}, clearTimeout() {} });
  check(w.listeners.keydown.length === before, "no second keydown listener",
        `listeners: ${before} → ${w.listeners.keydown.length}`);
}

finish();

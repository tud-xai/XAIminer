// Link toggle (base.html, "xaiToggleLink"): flips the button client-side while the POST
// only persists in the background.
//
// Usage: node check_link_toggle.js <rendered-workspace.html>
//
// Small island, but it owns the seam the scroll coupling reads: data-linked on the gallery.
// If that stops being written, coupling silently stops working.

const { section, check, finish, pickScript, Element, run } = require("./harness");

const script = pickScript(process.argv[2], "xaiToggleLink");

function makeWorld({ withGallery = true } = {}) {
  const gallery = new Element({ classes: ["panel-gallery"], dataset: { panelId: "1", linked: "0" } });
  const window = {};
  const document = {
    getElementById: () => null,
    querySelector: (sel) => (withGallery && sel.includes('data-panel-id="1"') ? gallery : null),
    querySelectorAll: () => [],
    addEventListener() {},
    body: { addEventListener() {} },
  };
  run(script, { window, document });
  const button = new Element({ classes: ["btn-outline-secondary"], tagName: "BUTTON" });
  return { toggle: () => window.xaiToggleLink(button, 1), button, gallery };
}

section("toggling on");
{
  const w = makeWorld();
  w.toggle();
  check(w.button.classList.contains("btn-primary"), "button marked as linked");
  check(!w.button.classList.contains("btn-outline-secondary"), "unlinked style removed");
  check(w.gallery.dataset.linked === "1", "gallery data-linked=1 (seam for scroll coupling)",
        `data-linked=${w.gallery.dataset.linked}`);
}

section("toggling off again");
{
  const w = makeWorld();
  w.toggle();
  const linkedTitle = w.button.title;
  w.toggle();
  check(!w.button.classList.contains("btn-primary"), "linked style removed");
  check(w.button.classList.contains("btn-outline-secondary"), "unlinked style restored");
  check(w.gallery.dataset.linked === "0", "gallery data-linked=0",
        `data-linked=${w.gallery.dataset.linked}`);
  // Titles come from the i18n catalogue - assert they differ and are non-empty rather than
  // hard-coding German/English strings.
  check(!!linkedTitle && !!w.button.title && linkedTitle !== w.button.title,
        "title switches between both states", `"${linkedTitle}" vs "${w.button.title}"`);
}

section("panel without a gallery (single-image view)");
{
  const w = makeWorld({ withGallery: false });
  let threw = null;
  try { w.toggle(); } catch (e) { threw = e; }
  check(!threw, "no crash when the gallery is absent", threw ? `${threw.name}: ${threw.message}` : "");
  check(w.button.classList.contains("btn-primary"), "button still toggles");
}

finish();

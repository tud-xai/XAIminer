// Usage-tracking island (base.html). The backend tests prove what the SERVER stores; this
// proves what the browser hands it — which is where the two properties that matter live:
// the shortcut must not fire by accident, and a password must never enter the queue.
//
// Dependency-free like its siblings (see README.md): node built-ins plus a DOM stub that is
// just rich enough for closest()/querySelector on the markup we actually ship.

const { section, check, finish, pickScript, run } = require("./harness");

const file = process.argv[2];
const script = pickScript(file, "__xaiTracking");

// ── a DOM small enough to read, real enough to exercise closest() ─────────────

const allElements = [];

/** Selector support: tag, .class, #id, [attr], and comma-separated lists of those. */
function matchesOne(el, sel) {
  sel = sel.trim();
  if (!sel) return false;
  if (sel[0] === ".") return el.classes.indexOf(sel.slice(1)) !== -1;
  if (sel[0] === "#") return el.id === sel.slice(1);
  if (sel[0] === "[") {
    const body = sel.slice(1, -1);
    const eq = body.indexOf("=");
    if (eq === -1) return el.getAttribute(body) !== null || body in el.dataset;
    const name = body.slice(0, eq);
    const want = body.slice(eq + 1).replace(/^["']|["']$/g, "");
    return String(el.getAttribute(name)) === want;
  }
  // "label[for=x]" — tag plus attribute part
  const bracket = sel.indexOf("[");
  if (bracket > 0) {
    return el.tagName === sel.slice(0, bracket).toUpperCase()
           && matchesOne(el, sel.slice(bracket));
  }
  return el.tagName === sel.toUpperCase();
}

function matches(el, selector) {
  return selector.split(",").some((part) => matchesOne(el, part));
}

class El {
  constructor(spec = {}, parent = null) {
    this.tagName = (spec.tag || "div").toUpperCase();
    this.id = spec.id || "";
    this.name = spec.name || "";
    this.type = spec.type || "";
    this.value = spec.value === undefined ? "" : spec.value;
    this.checked = !!spec.checked;
    this.textContent = spec.text || "";
    this.classes = spec.classes || [];
    this.dataset = spec.dataset || {};
    this.attrs = spec.attrs || {};
    this.isContentEditable = false;
    this.parentElement = parent;
    this.innerHTML = "";
    allElements.push(this);
  }
  get title() { return this.attrs.title || ""; }
  getAttribute(name) {
    if (name === "type") return this.type || null;
    return this.attrs[name] === undefined ? null : this.attrs[name];
  }
  closest(selector) {
    let node = this;
    while (node) {
      if (matches(node, selector)) return node;
      node = node.parentElement;
    }
    return null;
  }
  querySelector(selector) {
    return allElements.find((e) => e !== this && isAncestor(this, e) && matches(e, selector)) || null;
  }
}

function isAncestor(root, node) {
  for (let n = node.parentElement; n; n = n.parentElement) if (n === root) return true;
  return false;
}

// The markup we actually ship, reduced to the parts the island reads:
//   #config-modal (a .modal)
//     .form-check > input#f-umgebung-bahnhof + label[for] "Bahnhof (3)"
//     button#cfg-cancel "Abbrechen" > i.bi        (the icon is what a click targets)
//   .panel-card#panel-2
//   input#password (type=password)
const modal = new El({ id: "config-modal", classes: ["modal"] });
const formCheck = new El({ classes: ["form-check"] }, modal);
const checkbox = new El({ tag: "input", type: "checkbox", id: "f-umgebung-bahnhof",
                          name: "filter_umgebung", value: "bahnhof" }, formCheck);
const checkboxLabel = new El({ tag: "label", text: "Bahnhof (3)",
                               attrs: { for: "f-umgebung-bahnhof" } }, formCheck);
const cancelButton = new El({ tag: "button", id: "cfg-cancel", text: "Abbrechen" }, modal);
const cancelIcon = new El({ tag: "i", classes: ["bi", "bi-x"] }, cancelButton);
const panel = new El({ id: "panel-2", classes: ["panel-card"] });
const galleryButton = new El({ tag: "button", text: "Einzelbild",
                               attrs: { "hx-post": "/panel/2/view" } }, panel);
const passwordField = new El({ tag: "input", type: "password", id: "password",
                               name: "password", value: "hunter2" });
const markerSlot = new El({ id: "tracking-marker" });
// Containers whose textContent is the whole subtree — they must not be labelled by it.
const configForm = new El({ tag: "form", text: "Panel P1 konfigurieren KI-Modell ConvNeXt-T …",
                            attrs: { "hx-post": "/panel/1/config" } }, modal);
const navbar = new El({ tag: "nav", text: "XAIminer Demonstrator (R) Notizen Sammlungen Hilfe …" });

// ── stubs around it ──────────────────────────────────────────────────────────

const listeners = {};         // document
const bodyListeners = {};
const windowListeners = {};
const posts = [];             // {url, body} from fetch("/track")
const beacons = [];           // {url, body} from navigator.sendBeacon
const toggleCalls = [];
const timers = [];            // pending setTimeout callbacks (never fired on their own)

let toggleResponse = {
  ok: true,
  headers: { get: (name) => (name === "X-Tracking" ? "on" : null) },
  text: () => Promise.resolve('<span data-tracking="on" title="…">(R)</span>'),
};

const document = {
  title: "XAIminer – Arbeitsbereich",
  visibilityState: "visible",
  getElementById: (id) => allElements.find((e) => e.id === id) || null,
  querySelector: (sel) => allElements.find((e) => matches(e, sel)) || null,
  querySelectorAll: (sel) => allElements.filter((e) => matches(e, sel)),
  addEventListener: (type, fn) => (listeners[type] = listeners[type] || []).push(fn),
  body: { addEventListener: (type, fn) => (bodyListeners[type] = bodyListeners[type] || []).push(fn) },
};

const win = {
  addEventListener: (type, fn) => (windowListeners[type] = windowListeners[type] || []).push(fn),
};

const sandbox = {
  window: win,
  document,
  location: { pathname: "/", search: "" },
  navigator: {
    sendBeacon: (url, blob) => { beacons.push({ url, body: blob.parts.join("") }); return true; },
  },
  Blob: class { constructor(parts, opts) { this.parts = parts; this.opts = opts; } },
  fetch: (url, opts) => {
    if (url === "/tracking/toggle") { toggleCalls.push(opts); return Promise.resolve(toggleResponse); }
    posts.push({ url, body: (opts || {}).body });
    return Promise.resolve({ ok: true });
  },
  setTimeout: (fn) => { timers.push(fn); return timers.length; },
  clearTimeout: (id) => { if (id) timers[id - 1] = null; },
};

const ctx = run(script, sandbox);
const state = win.__xaiTracking;

/** Runs whatever the island scheduled — the flush that a real timer would trigger. */
function fireTimers() {
  const pending = timers.splice(0, timers.length).filter(Boolean);
  pending.forEach((fn) => fn());
}

function keydown(over) {
  const event = Object.assign({ key: "t", code: "KeyT", ctrlKey: true, altKey: true,
                                shiftKey: true, target: cancelButton,
                                prevented: false }, over);
  event.preventDefault = () => { event.prevented = true; };
  (listeners.keydown || []).forEach((fn) => fn(event));
  return event;
}

function queuedKinds() {
  return state.queue.map((e) => e.kind);
}

function sentEvents() {
  return posts.concat(beacons).flatMap((p) => JSON.parse(p.body).events);
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

// ── the checks ───────────────────────────────────────────────────────────────

async function main() {
  section("island is wired up");
  check(!!state, "the island publishes its state on window");
  check(state.on === false, "starts switched off", `on=${state && state.on}`);
  check(state.inScope === true, "the workspace is in scope");
  check((listeners.keydown || []).length === 2,
        "two keydown listeners: the shortcut and the tracked keys",
        `${(listeners.keydown || []).length}`);
  check(state.queue.length === 0,
        "nothing is queued while switched off (not even the initial page event)",
        queuedKinds().join(","));

  section("the shortcut is hard to hit by accident");
  keydown({ ctrlKey: false });
  keydown({ altKey: false });
  keydown({ shiftKey: false });
  keydown({ code: "KeyR" });
  check(toggleCalls.length === 0, "no modifier missing and no other key may toggle",
        `${toggleCalls.length} call(s)`);

  const event = keydown({ key: "†" });   // macOS reports the composed character for Alt+T
  check(toggleCalls.length === 1, "Ctrl+Alt+Shift+T toggles — matched on e.code, not e.key");
  check(event.prevented, "the combination is kept out of a focused text field");
  await tick();
  check(state.on === true, "the state follows the X-Tracking header");
  check(markerSlot.innerHTML.indexOf("(R)") !== -1, "the marker is swapped into the navbar slot",
        markerSlot.innerHTML);

  section("what a click records");
  listeners.click[0]({ target: cancelIcon });
  const cancel = state.queue[state.queue.length - 1];
  check(cancel.kind === "click" && cancel.el_id === "cfg-cancel",
        "a click on the icon is attributed to its button", JSON.stringify(cancel));
  check(cancel.label === "Abbrechen", "the visible text is recorded (this is why the feature exists: "
        + "cancelling triggers no request)", cancel.label);
  check(cancel.dialog === "config-modal", "the surrounding dialog is recorded", cancel.dialog);

  listeners.click[0]({ target: galleryButton });
  const inPanel = state.queue[state.queue.length - 1];
  check(inPanel.panel === "2", "the surrounding panel is recorded", inPanel.panel);
  check(inPanel.hx === "post /panel/2/view", "the HTMX request the element triggers", inPanel.hx);

  // A form or the navbar has no caption of its own: its textContent is the whole subtree, a
  // blob that says nothing about what was clicked (seen in the first real recordings).
  listeners.submit[0]({ target: configForm });
  const submitted = state.queue[state.queue.length - 1];
  check(submitted.kind === "submit" && submitted.label === undefined,
        "a container's subtree text is not recorded as its label", JSON.stringify(submitted));
  listeners.click[0]({ target: navbar });
  const stray = state.queue[state.queue.length - 1];
  check(stray.tag === "NAV" && stray.label === undefined,
        "a click on non-interactive chrome is kept, but without the text blob",
        JSON.stringify(stray));

  section("what a change records");
  checkbox.checked = true;
  listeners.change[0]({ target: checkbox });
  const ticked = state.queue[state.queue.length - 1];
  check(ticked.value === "checked bahnhof", "a checkbox reports its new state", ticked.value);
  check(ticked.label === "Bahnhof (3)", "…under the name the person saw", ticked.label);

  section("a password never enters the queue");
  listeners.change[0]({ target: passwordField });
  const secret = state.queue[state.queue.length - 1];
  check(!JSON.stringify(state.queue).includes("hunter2"),
        "the value of a password field is not recorded", JSON.stringify(secret));

  section("delivery");
  fireTimers();
  const delivered = sentEvents();
  check(posts.length === 1 && posts[0].url === "/track", "the batch is posted to /track",
        `${posts.length} post(s)`);
  check(delivered.length >= 4, "every queued event travels", `${delivered.length}`);
  check(state.queue.length === 0, "the queue is emptied by the flush");

  listeners.click[0]({ target: cancelButton });
  windowListeners.pagehide[0]({});
  check(beacons.length === 1 && beacons[0].url === "/track",
        "a page that is going away flushes via sendBeacon (fetch would be cancelled)",
        `${beacons.length} beacon(s)`);

  section("switching off");
  toggleResponse = { ok: true, headers: { get: () => "off" }, text: () => Promise.resolve("") };
  listeners.click[0]({ target: cancelButton });
  keydown({});
  await tick();
  check(state.on === false, "the shortcut switches the recording off again");
  check(state.queue.length === 0, "what was not sent yet is dropped, not delivered later",
        queuedKinds().join(","));
  listeners.click[0]({ target: cancelButton });
  check(state.queue.length === 0, "nothing is recorded once it is off");

  finish();
}

main();

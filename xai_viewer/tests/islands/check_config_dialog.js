// Config dialog islands (_config_modal.html): mode switch + XAI availability.
//
// Usage: node check_config_dialog.js <rendered-dialog.html>
//
// Why this exists: HTMX re-inserts this inline <script> on every dialog open. A `const` at
// global scope therefore threw "already declared" on the SECOND open - a SyntaxError, which
// kills the whole block. Markup correct, behaviour dead.

const { section, check, finish, pickScript, extractScripts, Element, run } = require("./harness");
const fs = require("fs");

const html = fs.readFileSync(process.argv[2], "utf8");
const script = pickScript(process.argv[2], "cfg-xai");
const MODELCARD = "__modelcard__";

/** <select> stub with just the child-list API the availability island uses. */
function makeSelect(values, selectedValue) {
  const select = { value: selectedValue, children: [], listeners: {} };
  select.addEventListener = (type, fn) => (select.listeners[type] = fn);
  Object.defineProperty(select, "options", { get: () => select.children.slice() });
  const detach = (opt) => {
    const i = select.children.indexOf(opt);
    if (i >= 0) select.children.splice(i, 1);
  };
  select.insertBefore = (opt, ref) => {
    detach(opt);
    select.children.splice(select.children.indexOf(ref), 0, opt);
  };
  select.children = values.map((value) => ({
    value, selected: value === selectedValue, remove() { detach(this); },
  }));
  return select;
}

function makeWorld({ xaiValue = "original", availability = { VGG16: ["Grad-CAM"] },
                     model = "VGG16", source = "filter", confActive = false } = {}) {
  // The two confidence range inputs are addressed by class, not by id.
  const confInputs = [{ disabled: false }, { disabled: false }];
  const modelSelect = { value: model, listeners: {} };
  modelSelect.addEventListener = (type, fn) => (modelSelect.listeners[type] = fn);
  const elements = {
    "data-xai-availability": { textContent: JSON.stringify(availability) },
    "cfg-model": modelSelect,
    "cfg-xai": makeSelect(["original", "Grad-CAM", "LRP", "", MODELCARD], xaiValue),
    "cfg-xai-hint": { textContent: "" },
    "cfg-source": { value: source, addEventListener() {} },
    "cfg-conf-active": { checked: confActive, addEventListener() {} },
    "filter-section": new Element(),
    "filter-disabled-hint": new Element(),
    "images-section": new Element(),
    "modelcard-section": new Element(),
  };
  const document = {
    getElementById: (id) => elements[id],
    querySelector: () => null,
    querySelectorAll: (sel) => (sel === ".cfg-conf-input" ? confInputs : []),
    addEventListener() {},
    body: { addEventListener() {} },
  };
  return { elements, confInputs, document, window: {} };
}

/** d-none is toggled via classList; read it back through the stub's real ClassList. */
const hidden = (el) => el.classList.contains("d-none");

section("repeated evaluation (HTMX re-inserts the script on every dialog open)");
{
  const w = makeWorld();
  const ctx = { console, JSON, Element, window: w.window, document: w.document };
  const vm = require("vm");
  const context = vm.createContext(ctx);
  let ok = true;
  for (const run_ of [1, 2, 3]) {
    try {
      vm.runInContext(script, context, { filename: "dialog.js" });
      check(true, `run ${run_}`);
    } catch (e) {
      check(false, `run ${run_}`, `${e.name}: ${e.message}`);
      ok = false;
      break;   // a SyntaxError kills the block; further runs add no information
    }
  }
  if (!ok) finish();
}

section("only one plain <script> block in the dialog (checks stay unambiguous)");
{
  const blocks = extractScripts(html);
  check(blocks.length === 1, "exactly one island block", `found ${blocks.length}`);
}

section("mode island (display dropdown → which section is active and submitted)");
{
  for (const [value, isCard] of [[MODELCARD, true], ["original", false]]) {
    const w = makeWorld({ xaiValue: value });
    run(script, { window: w.window, document: w.document });
    const images = w.elements["images-section"];
    const card = w.elements["modelcard-section"];
    const state = `images(disabled=${images.disabled},d-none=${hidden(images)}) ` +
                  `card(disabled=${card.disabled},d-none=${hidden(card)})`;
    check(images.disabled === isCard && hidden(images) === isCard
          && card.disabled === !isCard && hidden(card) === !isCard,
          `#cfg-xai=${value}`, state);
  }
}

section("availability island (unavailable methods removed; modelcard is a mode, not a rendering)");
{
  const w = makeWorld({ xaiValue: "LRP", availability: { VGG16: ["Grad-CAM"], ResNet50: ["LRP", "Grad-CAM"] } });
  run(script, { window: w.window, document: w.document });
  const xai = w.elements["cfg-xai"];
  const values = () => xai.options.map((o) => o.value).join(",");
  check(values() === `original,Grad-CAM,,${MODELCARD}`, "unavailable method removed", values());
  check(xai.value === "original", "selection reset to original", `value=${xai.value}`);
  check(w.elements["cfg-xai-hint"].textContent !== "", "reset hint shown");

  const model = w.elements["cfg-model"];
  model.value = "ResNet50";
  model.listeners.change();
  check(values() === `original,Grad-CAM,LRP,,${MODELCARD}`, "re-inserted in original order", values());
  check(xai.options.every((o) => o.value !== "LRP" || !o.selected), "re-inserted option not selected");
  check(w.elements["cfg-xai-hint"].textContent === "", "hint cleared when nothing was reset");

  model.value = "unknown";
  model.listeners.change();
  check(values() === `original,,${MODELCARD}`, "original, separator and modelcard always offered", values());
}

section("confidence island (range inputs greyed out — and thus not submitted — while off)");
{
  for (const active of [false, true]) {
    const w = makeWorld({ confActive: active });
    run(script, { window: w.window, document: w.document });
    const states = w.confInputs.map((el) => el.disabled);
    check(states.every((d) => d === !active),
          `checkbox=${active} → inputs disabled=${!active}`, `got [${states}]`);
  }
}

finish();

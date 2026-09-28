"""
Generates `metadata.json` for a *real* dataset (e.g. RailPer) from the raw sources
and writes it to the dataset directory.

Target schema and decisions: see `../DATA.md` (data reference).
Canonical names = real names (VGG16, ResNet50, ConvNeXt-T / Grad-CAM / low, mid, high).

Inputs per dataset (`<DATASETS_ROOT>/<name>/`):
  originals/imgs/<basename>.{jpg,png}
  originals/annots/<basename>.json            (only class "person" present)
  originals/all_labels.csv                     (image_name,label) – authoritative for true_class
  originals/manual_labels.json                 OPTIONAL, real datasets: manually assigned labels
  originals/scene_attributes.csv               OPTIONAL, synthetic datasets: real scene facts
  xai/<Model>/<Method>/<level>/<basename>_<m>.{jpg,png}
  inference/inference_results_<Model>_<level>.csv  (image_name,sigmoid_output,classification,ground_truth)

Filter attributes not derivable from the data (umgebung/tageszeit/wetter/distances)
are mocked deterministically per basename and flagged as "mock" in the `provenance` block.
Two real sources take precedence, in this order:
  * `manual_labels.json` (hand labels, export format) – ALL categories are real, "manual";
  * `scene_attributes.csv` (synthetic datasets) – every column it carries is real, "scene":
    objekte/wetter/tageszeit always, umgebung and the two distances where the generator
    writes them (since 2026-09). What the file omits stays mocked.
Manual labels never carry distances, so those stay mocked wherever they are the source.

Usage (from `xai_viewer/`):
        python tools/build_metadata.py [DATASET_NAME]   # Default: RailPer
        python tools/build_metadata.py --root /path/to/datasets RailPer
"""

import argparse
import csv
import json
import random
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
XAI_DIR = HERE.parent
# The tools sit one level below the app modules, which are flat files and not a package.
# A tool run as `python tools/<name>.py` therefore puts the app directory on sys.path
# itself; when imported as `tools.<name>` (the tests do that) it is already there.
if str(XAI_DIR) not in sys.path:
    sys.path.insert(0, str(XAI_DIR))

from filter_options import DISTANCE_FILTERS, FILTER_CATEGORIES        # noqa: E402
from labeling_store import STORE_NAME, parse_categories, store_path   # noqa: E402

DEFAULT_DATASETS_ROOT = XAI_DIR.parent.parent / "datasets"

# Logical order of training levels (directory/file names are English, A11). Both spellings of
# the middle level occur in delivered data ("mid" in the synthetic sets, "middle" in RailPer);
# without both, "high" would sort before the middle level.
LEVEL_ORDER = ["low", "mid", "middle", "high"]

# Files to be ignored while scanning (OS junk).
IGNORE_NAMES = {".DS_Store", "Thumbs.db"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}

# CRP manifest: relevance ranking of concepts per image. Its presence in a level folder
# (which otherwise holds only <basename>_crp/ subfolders, no direct images) is the signal
# that CRP is available for that model/level. See DATA.md.
CONCEPTS_CSV = "concepts.csv"

# Optional, synthetic datasets only: ground-truth scene facts next to the images. Every filter
# column it carries is used instead of a mocked value; see scene_attributes() and DATA.md.
SCENE_ATTRIBUTES_CSV = "scene_attributes.csv"

# Optional: labels assigned by hand. Where they exist they are
# authoritative for ALL filter categories, including the category set itself. See DATA.md.
# Two shapes are accepted, and both are recognised by their content, not by their file name:
#   * the tool's EXPORT (schema key) – the format that travels between people;
#   * the tool's live STORE (categories_text key) – readable directly on the machine that
#     labelled, so labelling and rebuilding need no export/copy step in between.
MANUAL_LABELS_JSON = "manual_labels.json"
MANUAL_LABELS_SCHEMA = "xai-viewer-manual-labels/1"

# How many filter categories the viewer renders side by side. More would not fit the filter
# dialog; a file carrying more is truncated with a warning rather than silently.
MAX_FILTER_CATEGORIES = 4

# Category tokens renamed with the RailPer labelling round (2026-08). Applied when reading the
# older sources so that synthetic datasets keep their real weather instead of falling back to
# mocked values.
TOKEN_ALIASES = {"wetter": {"sonne": "sonnig", "wolken": "bewoelkt"}}

# Optional model metadata: one row per (model, level) feeding the modelcard block.
# Numeric columns are parsed as numbers, the rest kept as strings; absent → field renders "N/A".
MODEL_META_CSV = "model_meta.csv"
MODEL_META_INT_FIELDS = ("params", "epochs")
MODEL_META_FLOAT_FIELDS = ("nominal_accuracy",)
MODEL_META_STR_FIELDS = ("trained_at", "algorithm", "train_dataset", "test_dataset")

# CRAFT: per-image attribution maps live in this sub-folder of the level dir (the level dir
# itself has no direct images). Its presence is the availability signal for CRAFT. See DATA.md.
CRAFT_MAPS_SUBDIR = "concept_attribution_maps"
# CRAFT: GLOBAL concept prototypes (concept<N>.<ext>) shared across all images of a model/level.
CRAFT_CONCEPTS_SUBDIR = "concepts"
CRAFT_CONCEPT_RE = re.compile(r"^concept(\d+)\.", re.IGNORECASE)

CLASS_LABELS = {"person": "Person", "not_person": "No person"}


def warn(msg: str):
    print(f"WARN: {msg}", file=sys.stderr)


def basename_key(raw: str) -> str:
    """Join key from a CSV image_name: strip a trailing *image* extension if present, but keep
    dots that are part of the name. osdar names carry float timestamps (…_1631531579.700000016),
    where Path(...).stem would wrongly strip the fractional part as if it were a suffix."""
    name = (raw or "").strip()
    p = Path(name)
    return p.stem if p.suffix.lower() in IMAGE_SUFFIXES else name


def norm_class(raw: str) -> str:
    """Normalises the inconsistent class tokens to person / not_person (A13)."""
    s = (raw or "").strip().lower()
    if s == "person":
        return "person"
    if s in ("not person", "no person", "not_person", "no_person"):
        return "not_person"
    raise ValueError(f"unbekanntes Klassen-Token: {raw!r}")


def sort_levels(levels) -> list:
    """Sorts levels by LEVEL_ORDER; unknown levels appended alphabetically."""
    known = [lv for lv in LEVEL_ORDER if lv in levels]
    extra = sorted(lv for lv in levels if lv not in LEVEL_ORDER)
    return known + extra


# ── Read raw sources ──────────────────────────────────────────────────────────

def read_images(imgs_dir: Path) -> dict:
    """{basename: filename} from originals/imgs (jpg/png), joined by basename (A8)."""
    images = {}
    for p in sorted(imgs_dir.iterdir()):
        if p.name in IGNORE_NAMES or p.suffix.lower() not in IMAGE_SUFFIXES:
            continue
        if p.stem in images:
            warn(f"doppelter Basisname in imgs: {p.stem} ({images[p.stem]} vs {p.name})")
        images[p.stem] = p.name
    return images


def read_labels(csv_path: Path) -> dict:
    """{basename: true_class} from all_labels.csv (authoritative, Q7)."""
    labels = {}
    with open(csv_path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            basename = basename_key(row["image_name"])
            labels[basename] = norm_class(row["label"])
    return labels


def read_annots(annots_dir: Path) -> dict:
    """{basename: has_person} – annotations contain only class 'person' in practice (A-TBD1)."""
    has_person = {}
    for p in sorted(annots_dir.iterdir()):
        if p.suffix.lower() != ".json" or p.name in IGNORE_NAMES:
            continue
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            warn(f"annot nicht lesbar: {p.name} ({exc})")
            continue
        annotations = data.get("annotations", [])
        has_person[p.stem] = any(a.get("class") == "person" for a in annotations)
    return has_person


def scan_xai_availability(xai_dir: Path) -> dict:
    """From the filesystem: {Model: {Method: [levels]}} (Q9a). Empty levels are omitted."""
    availability = {}
    if not xai_dir.is_dir():
        return availability
    for model_dir in sorted(d for d in xai_dir.iterdir() if d.is_dir()):
        methods = {}
        for method_dir in sorted(d for d in model_dir.iterdir() if d.is_dir()):
            levels = []
            for level_dir in (d for d in method_dir.iterdir() if d.is_dir()):
                has_image = any(
                    f.suffix.lower() in IMAGE_SUFFIXES and f.name not in IGNORE_NAMES
                    for f in level_dir.iterdir()
                )
                # CRP has no direct images (only <basename>_crp/ subfolders); its concepts.csv
                # manifest stands in as the availability signal.
                has_concepts = (level_dir / CONCEPTS_CSV).exists()
                # CRAFT has no direct images either; its per-image maps sit in a sub-folder.
                maps = level_dir / CRAFT_MAPS_SUBDIR
                has_craft = maps.is_dir() and any(
                    f.suffix.lower() in IMAGE_SUFFIXES and f.name not in IGNORE_NAMES
                    for f in maps.iterdir()
                )
                if has_image or has_concepts or has_craft:
                    levels.append(level_dir.name)
            if levels:
                methods[method_dir.name] = sort_levels(levels)
        if methods:
            availability[model_dir.name] = methods
    return availability


def read_inference(inference_dir: Path, model: str, level: str, true_class: dict) -> dict:
    """Reads inference_results_<Model>_<level>.csv (forward construction).

    Returns {basename: {predicted_class, confidences}}. confidences.person = sigmoid_output.
    Compares ground_truth against true_class and warns on mismatch.
    """
    # Both separators occur in delivered data: RailPer ships "…_VGG16-low.csv", the older
    # synthetic sets "…_VGG16_low.csv". The model name itself may contain "-" (ConvNeXt-T),
    # so the separator cannot simply be normalised away.
    candidates = [inference_dir / f"inference_results_{model}{sep}{level}.csv" for sep in ("_", "-")]
    path = next((c for c in candidates if c.exists()), None)
    if path is None:
        warn(f"Inference-Datei fehlt: {candidates[0].name}")
        return {}

    result = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            basename = basename_key(row["image_name"])
            p_person = float(row["sigmoid_output"])
            predicted = norm_class(row["classification"])
            gt = norm_class(row["ground_truth"])
            if basename in true_class and gt != true_class[basename]:
                warn(
                    f"ground_truth-Mismatch {model}-{level} {basename}: "
                    f"inference={gt} vs all_labels={true_class[basename]} → all_labels gilt"
                )
            result[basename] = {
                "predicted_class": predicted,
                "confidences": {
                    "person": round(p_person, 6),
                    "not_person": round(1.0 - p_person, 6),
                },
            }
    return result


def read_model_meta(dataset_dir: Path) -> dict:
    """Reads model_meta.csv → {model: {level: {field: value}}}.

    One row per (model, level). Numeric columns (params, epochs, nominal_accuracy) are parsed
    as numbers, the rest kept as strings; blank cells are omitted (the field then renders "N/A").
    Returns {} if the file is absent. Malformed rows are skipped with a warning. Display
    formatting (millions, percent) happens in the app, so raw values are stored here.
    """
    path = dataset_dir / MODEL_META_CSV
    if not path.exists():
        return {}

    result = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            model = (row.get("model") or "").strip()
            level = (row.get("level") or "").strip()
            if not model or not level:
                warn(f"{MODEL_META_CSV}: Zeile ohne model/level übersprungen: {row!r}")
                continue
            entry = {}
            for field in MODEL_META_INT_FIELDS:
                raw = (row.get(field) or "").strip()
                if raw:
                    try:
                        entry[field] = int(float(raw))
                    except ValueError:
                        warn(f"{MODEL_META_CSV} {model}/{level}: {field}={raw!r} keine Zahl → weggelassen")
            for field in MODEL_META_FLOAT_FIELDS:
                raw = (row.get(field) or "").strip()
                if raw:
                    try:
                        entry[field] = float(raw)
                    except ValueError:
                        warn(f"{MODEL_META_CSV} {model}/{level}: {field}={raw!r} keine Zahl → weggelassen")
            for field in MODEL_META_STR_FIELDS:
                raw = (row.get(field) or "").strip()
                if raw:
                    entry[field] = raw
            if entry:
                result.setdefault(model, {})[level] = entry
    return result


def read_concepts(level_dir: Path) -> dict:
    """Reads a CRP concepts.csv → {basename: [{rank, concept_id, relevance}, ...]}.

    Entries per image are sorted by rank ascending (rank 1 = highest relevance, the tile).
    Returns {} if the manifest is absent. Malformed rows are skipped with a warning.
    """
    path = level_dir / CONCEPTS_CSV
    if not path.exists():
        return {}

    per_image = {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                basename = basename_key(row["image_name"])
                entry = {
                    "rank": int(row["rank"]),
                    "concept_id": int(row["concept_id"]),
                    "relevance": round(float(row["relevance"]), 6),
                }
            except (KeyError, ValueError) as exc:
                warn(f"ungültige concepts.csv-Zeile in {path.parent.name}: {row} ({exc})")
                continue
            per_image.setdefault(basename, []).append(entry)

    for entries in per_image.values():
        entries.sort(key=lambda e: e["rank"])
    return per_image


def read_craft_concepts(level_dir: Path, global_info: dict | None = None) -> list:
    """Global CRAFT concept prototypes from concepts/: [{concept_id, file, color?, global_importance?}]
    sorted by id. Shared across ALL images of a model/level (unlike CRP's per-image concepts).

    `color` and `global_importance` come from the concepts.csv manifest (`global_info`, keyed by
    concept_id) where present; absent otherwise (older exports without the manifest)."""
    global_info = global_info or {}
    cdir = level_dir / CRAFT_CONCEPTS_SUBDIR
    if not cdir.is_dir():
        return []
    concepts = []
    for f in cdir.iterdir():
        if f.suffix.lower() not in IMAGE_SUFFIXES or f.name in IGNORE_NAMES:
            continue
        m = CRAFT_CONCEPT_RE.match(f.name)
        if m:
            cid = int(m.group(1))
            info = global_info.get(cid, {})
            entry = {"concept_id": cid, "file": f.name}
            if info.get("color"):
                entry["color"] = info["color"]
            if info.get("global_importance") is not None:
                entry["global_importance"] = info["global_importance"]
            concepts.append(entry)
    concepts.sort(key=lambda c: c["concept_id"])
    return concepts


def read_craft_manifest(level_dir: Path) -> tuple[dict, dict]:
    """Reads a CRAFT concepts.csv → (per_image, global_info).

    per_image[basename] = [{rank, concept_id, activation_score}, ...] sorted by rank (per image).
    global_info[concept_id] = {"global_importance": float|None, "color": str|None} — constant per
    concept, so taken from the first row seen. Returns ({}, {}) if the file is absent. Columns:
    image_name,rank,concept_id,activation_score,global_importance,concept_color (see DATA.md)."""
    path = level_dir / CONCEPTS_CSV
    if not path.exists():
        return {}, {}

    per_image, global_info = {}, {}
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                basename = basename_key(row["image_name"])
                cid = int(row["concept_id"])
                entry = {
                    "rank": int(row["rank"]),
                    "concept_id": cid,
                    "activation_score": round(float(row["activation_score"]), 6),
                }
            except (KeyError, ValueError) as exc:
                warn(f"ungültige CRAFT concepts.csv-Zeile in {path.parent.name}: {row} ({exc})")
                continue
            per_image.setdefault(basename, []).append(entry)
            if cid not in global_info:
                gi = (row.get("global_importance") or "").strip()
                color = (row.get("concept_color") or row.get("color") or "").strip()
                global_info[cid] = {
                    "global_importance": round(float(gi), 6) if gi else None,
                    "color": color or None,
                }

    for entries in per_image.values():
        entries.sort(key=lambda e: e["rank"])
    return per_image, global_info


# ── Manual labels (real datasets) ─────────────────────────────────────────────

def _capped(categories: list, where: str) -> list:
    """At most MAX_FILTER_CATEGORIES – never silently, an ignored category is worth a warning."""
    if len(categories) <= MAX_FILTER_CATEGORIES:
        return categories
    dropped = [c["key"] for c in categories[MAX_FILTER_CATEGORIES:]]
    warn(f"{where}: {len(categories)} Kategorien, der Viewer zeigt "
         f"{MAX_FILTER_CATEGORIES} → ignoriert: {dropped}")
    return categories[:MAX_FILTER_CATEGORIES]


def read_export_labels(path: Path) -> tuple:
    """Hand-label EXPORT → ``(labels, categories)``; ``({}, [])`` if unusable.

    labels: ``{basename: {category_key: [token, ...]}}`` – exactly what the tool wrote.
    categories: ``[{"key", "label", "options": [{"token", "label"}, ...]}, ...]`` in the order the
    labeller defined them; goes into `metadata.json` so the viewer can offer *this* dataset's
    categories instead of the built-in default set (see DATA.md).
    """
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    schema = payload.get("schema")
    if schema != MANUAL_LABELS_SCHEMA:
        warn(f"{path.name}: unerwartetes schema '{schema}' (erwartet '{MANUAL_LABELS_SCHEMA}') "
             f"→ ignoriert")
        return {}, []

    categories = []
    for key, spec in (payload.get("categories") or {}).items():
        options = [{"token": o["token"], "label": o.get("label") or o["token"]}
                   for o in (spec.get("options") or []) if o.get("token")]
        if not options:
            warn(f"{path.name}: Kategorie '{key}' ohne Optionen → ignoriert")
            continue
        categories.append({"key": key, "label": spec.get("label") or key, "options": options})

    labels = {basename_key(name): row for name, row in (payload.get("images") or {}).items()}
    return labels, _capped(categories, path.name)


def read_store_labels(path: Path) -> tuple:
    """Live label STORE → ``(labels, categories)``, same shape as the export.

    The store keeps the categories as the editable text the labeller typed; `parse_categories()`
    (the tool's own parser, so both agree on the tokens) turns it into the same structure the
    export carries. Reading it directly is what makes the export/copy step unnecessary on the
    machine that did the labelling.
    """
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)

    groups, errors = parse_categories(payload.get("categories_text") or "")
    if errors:
        warn(f"{path.name}: Kategorientext fehlerhaft ({errors[0].get('code')}) → ignoriert")
        return {}, []

    labels = {basename_key(name): row for name, row in (payload.get("labels") or {}).items()}
    return labels, _capped(groups, path.name)


def resolve_manual_labels(dataset_dir: Path, dataset: str, explicit: Path = None) -> tuple:
    """Picks the label source and reads it → ``(labels, categories, source)``.

    Precedence, first hit wins (``source`` is None when nothing was found):
      1. ``--labels PATH``                              – an export or a store, anywhere;
      2. ``<dataset>/originals/manual_labels.json``     – the copy that travelled with the data;
      3. ``xai_viewer/labeling_<dataset>.json``         – the live label store on this machine.
    Which file was used is printed, because "why are the filters still mocked?" is otherwise
    a guessing game.
    """
    candidates = [explicit] if explicit else [dataset_dir / "originals" / MANUAL_LABELS_JSON,
                                              store_path(XAI_DIR, dataset)]
    for path in candidates:
        if path is None or not Path(path).exists():
            continue
        path = Path(path)
        with open(path, encoding="utf-8") as f:
            head = json.load(f)
        # Recognised by content, not by file name: --labels may point at either shape, and a
        # renamed copy must not silently fall through.
        if "categories_text" in head:
            labels, categories = read_store_labels(path)
        else:
            labels, categories = read_export_labels(path)
        if labels or categories:
            return labels, categories, path
        warn(f"{path} enthält keine brauchbaren Labels → weiter mit der nächsten Quelle")
    return {}, [], None


def manual_attributes(basename: str, row: dict, categories: list) -> tuple:
    """Filter attributes for an image a human has labelled.

    Every category is real; unknown tokens (a label file out of step with its own category
    block) are dropped with a warning. The distances remain mocked – nobody labelled those.
    """
    rng = random.Random(basename)
    attrs, provenance = {}, {"true_class": "all_labels"}
    for category in categories:
        known = {o["token"] for o in category["options"]}
        tokens = [tok for tok in (row.get(category["key"]) or []) if tok in known]
        unknown = [tok for tok in (row.get(category["key"]) or []) if tok not in known]
        if unknown:
            warn(f"unbekannte {category['key']}-Tokens {unknown} für {basename} → verworfen")
        attrs[category["key"]] = tokens
        provenance[category["key"]] = "manual"

    for key, spec in DISTANCE_FILTERS.items():
        attrs[key] = round(rng.uniform(spec["min"], spec["max"]), 1)
        provenance[key] = "mock"
    return attrs, provenance


# ── Scene attributes (synthetic datasets) ─────────────────────────────────────

def read_scene_attributes(originals_dir: Path) -> dict:
    """Optional `scene_attributes.csv` → {basename: row}. Empty if the file is absent.

    Only synthetic datasets have it (the generator knows the ground truth of the scene);
    for RailPer there is no such source and everything stays mocked. See DATA.md.
    """
    path = originals_dir / SCENE_ATTRIBUTES_CSV
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return {basename_key(r["image_name"]): r for r in csv.DictReader(f)}


def alias_token(key: str, token: str) -> str:
    """Maps a category token of an older source onto its current name (see TOKEN_ALIASES)."""
    return TOKEN_ALIASES.get(key, {}).get(token, token)


def scene_distances(row: dict) -> dict:
    """Distances from `scene_attributes.csv`, or None if that source carries none.

    The COLUMN is the signal, not its content: where the generator writes these columns, its
    numbers are the truth and an empty cell means "no person in this scene" – a fact, not a
    gap. Such a `None` reaches `image_passes_filter()` as "unknown" and never excludes the
    image. A CSV without the columns (every dataset exported before 2026-09) keeps the mocked
    values, so old datasets rebuild exactly as before.
    """
    if not any(key in row for key in DISTANCE_FILTERS):
        return None
    values = {}
    for key in DISTANCE_FILTERS:
        raw = (row.get(key) or "").strip()
        try:
            values[key] = float(raw) if raw else None
        except ValueError:
            warn(f"unlesbares {key} '{raw}' in {SCENE_ATTRIBUTES_CSV} → ohne Wert")
            values[key] = None
    return values


def scene_attributes(basename: str, row: dict, has_person: bool) -> tuple:
    """Filter attributes for an image the generator knows the scene of.

    Real where the CSV carries it: `objekte` (mensch/gebaeude from the scene flags),
    `wetter`, `tageszeit`, `umgebung` and the two distances. Anything the CSV leaves out
    stays mocked, which keeps datasets exported by an older generator unchanged.
    Every category value is a *list* of tokens (the scene simply never yields more than one).
    """
    rng = random.Random(basename)
    objekte = ["mensch"] if has_person else []
    if row.get("building_present") == "1":
        objekte.append("gebaeude")

    scene_values = {key: alias_token(key, (row.get(key) or "").strip())
                    for key in ("umgebung", "tageszeit", "wetter")}
    for key, value in scene_values.items():
        if value and value not in FILTER_CATEGORIES[key]["options"]:
            warn(f"unbekanntes {key} '{value}' in scene_attributes.csv ({basename}) → gemockt")
            scene_values[key] = ""

    # `or` is lazy, so a value the scene supplies costs no draw – a dataset whose CSV lacks the
    # new columns therefore mocks exactly what it mocked before they existed.
    attrs = {
        "umgebung": [scene_values["umgebung"] or
                     rng.choice(list(FILTER_CATEGORIES["umgebung"]["options"]))],
        "objekte": objekte,
        "tageszeit": [scene_values["tageszeit"] or
                      rng.choice(list(FILTER_CATEGORIES["tageszeit"]["options"]))],
        "wetter": [scene_values["wetter"] or
                   rng.choice(list(FILTER_CATEGORIES["wetter"]["options"]))],
    }
    distances = scene_distances(row)
    for key, spec in DISTANCE_FILTERS.items():
        mock = round(rng.uniform(spec["min"], spec["max"]), 1)
        attrs[key] = mock if distances is None else distances[key]

    provenance = {
        "true_class": "all_labels",
        "objekte": "scene",
        "umgebung": "scene" if scene_values["umgebung"] else "mock",
        "tageszeit": "scene" if scene_values["tageszeit"] else "mock",
        "wetter": "scene" if scene_values["wetter"] else "mock",
        "dist_lateral_m": "mock" if distances is None else "scene",
        "dist_longitudinal_m": "mock" if distances is None else "scene",
    }
    return attrs, provenance


# ── Mocked attributes (A-TBD1, A5) ───────────────────────────────────────────

def mock_attributes(basename: str, has_person: bool) -> tuple:
    """Deterministically (seed = basename) mocked filter attributes + provenance.

    Only `mensch` in `objekte` is real (derived from true_class) – everything else is mocked.
    """
    rng = random.Random(basename)
    objekte = ["mensch"] if has_person else []
    for obj in ("signal", "gebaeude", "bruecke"):
        if rng.random() < 0.3:
            objekte.append(obj)

    attrs = {
        "umgebung": [rng.choice(list(FILTER_CATEGORIES["umgebung"]["options"]))],
        "objekte": objekte,
        "tageszeit": [rng.choice(list(FILTER_CATEGORIES["tageszeit"]["options"]))],
        "wetter": [rng.choice(list(FILTER_CATEGORIES["wetter"]["options"]))],
    }
    for key, spec in DISTANCE_FILTERS.items():
        attrs[key] = round(rng.uniform(spec["min"], spec["max"]), 1)

    provenance = {
        "true_class": "all_labels",
        "objekte": "mock",  # only 'mensch' is derived, the rest is mocked
        "umgebung": "mock",
        "tageszeit": "mock",
        "wetter": "mock",
        "dist_lateral_m": "mock",
        "dist_longitudinal_m": "mock",
    }
    return attrs, provenance


# ── Main run ───────────────────────────────────────────────────────────────────

def build(dataset_dir: Path, labels_path: Path = None) -> dict:
    imgs_dir = dataset_dir / "originals" / "imgs"
    annots_dir = dataset_dir / "originals" / "annots"
    labels_csv = dataset_dir / "originals" / "all_labels.csv"
    xai_dir = dataset_dir / "xai"
    inference_dir = dataset_dir / "inference"

    for required in (imgs_dir, labels_csv):
        if not required.exists():
            raise SystemExit(f"Pflicht-Quelle fehlt: {required}")

    images_files = read_images(imgs_dir)
    labels = read_labels(labels_csv)
    has_person = read_annots(annots_dir) if annots_dir.is_dir() else {}
    scene = read_scene_attributes(dataset_dir / "originals")
    manual, manual_categories, manual_source = resolve_manual_labels(
        dataset_dir, dataset_dir.name, labels_path)
    # Name the source that actually applies: scene_attributes.csv fills the same categories,
    # so "keine Handlabels" alone must not be reported as "wird gemockt".
    if manual_source:
        print(f"Handlabels: {manual_source}")
    elif scene:
        print("Handlabels: keine gefunden → Filter-Attribute aus scene_attributes.csv, "
              "was sie nicht führt wird gemockt")
    else:
        print("Handlabels: keine gefunden → Filter-Attribute werden gemockt")

    # Cross-check classes from annotations against all_labels (Q7).
    for basename in images_files:
        if basename not in labels:
            warn(f"kein Label in all_labels.csv für {basename} → als not_person behandelt")
        annot_person = has_person.get(basename)
        if annot_person is not None and basename in labels:
            annot_class = "person" if annot_person else "not_person"
            if annot_class != labels[basename]:
                warn(
                    f"Klassen-Mismatch {basename}: annots={annot_class} vs "
                    f"all_labels={labels[basename]} → all_labels gilt"
                )

    images = []
    for basename, filename in images_files.items():
        true_class = labels.get(basename, "not_person")
        if basename in manual:
            attrs, provenance = manual_attributes(basename, manual[basename], manual_categories)
        elif basename in scene:
            attrs, provenance = scene_attributes(basename, scene[basename], true_class == "person")
        else:
            attrs, provenance = mock_attributes(basename, true_class == "person")
        images.append({
            "id": basename,
            "filename": filename,
            "true_class": true_class,
            "attributes": attrs,
            "provenance": provenance,
        })

    true_class_map = {img["id"]: img["true_class"] for img in images}
    xai_availability = scan_xai_availability(xai_dir)

    # Derive models/levels from the XAI availability; read their predictions.
    models = sorted(xai_availability)
    levels = sort_levels({lv for m in xai_availability.values()
                          for lvls in m.values() for lv in lvls})

    predictions = {}
    if inference_dir.is_dir():
        for model in models:
            model_levels = sort_levels({lv for lvls in xai_availability[model].values()
                                        for lv in lvls})
            predictions[model] = {}
            for level in model_levels:
                preds = read_inference(inference_dir, model, level, true_class_map)
                if preds:
                    predictions[model][level] = preds
    else:
        warn(f"kein inference/-Verzeichnis in {dataset_dir} → keine Predictions")

    # Per-image concept manifests: model → method → level → basename → [{rank, concept_id, …}].
    #   CRP:   [{rank, concept_id, relevance}]        (concepts.csv, relevance column)
    #   CRAFT: [{rank, concept_id, activation_score}] (concepts.csv, activation/importance/color)
    xai_concepts = {}
    # CRAFT global concept prototypes: model → CRAFT → level → [{concept_id, file, color?, global_importance?}].
    xai_global_concepts = {}
    for model, methods in xai_availability.items():
        for method, mlevels in methods.items():
            for level in mlevels:
                level_dir = xai_dir / model / method / level
                if method == "CRP":
                    concepts = read_concepts(level_dir)
                    if concepts:
                        xai_concepts.setdefault(model, {}).setdefault(method, {})[level] = concepts
                elif method == "CRAFT":
                    per_image, global_info = read_craft_manifest(level_dir)
                    craft = read_craft_concepts(level_dir, global_info)
                    if craft:
                        xai_global_concepts.setdefault(model, {}).setdefault(method, {})[level] = craft
                    if per_image:
                        xai_concepts.setdefault(model, {}).setdefault(method, {})[level] = per_image

    if manual and len(manual) < len(images_files):
        warn(f"{len(images_files) - len(manual)} Bilder ohne Eintrag in "
             f"{MANUAL_LABELS_JSON} → deren Filter-Attribute bleiben gemockt")

    metadata = {
        "dataset": dataset_dir.name,
        "classes": ["person", "not_person"],
        "class_labels": CLASS_LABELS,
        "models": models,
        "levels": levels,
        "xai_availability": xai_availability,
        "images": images,
        "predictions": predictions,
        "xai_concepts": xai_concepts,
        "xai_global_concepts": xai_global_concepts,
        "model_meta": read_model_meta(dataset_dir),
    }
    # Only when the categories come from this dataset's own label file. Without it the viewer
    # falls back to filter_options.FILTER_CATEGORIES + the i18n catalogue, which is the better
    # source for a dataset nobody has labelled by hand.
    if manual_categories:
        metadata["filter_categories"] = manual_categories
    return metadata


LABEL_SOURCE_HELP = f"""\
Handlabels (Filter-Attribute) werden aus der ERSTEN dieser Quellen gelesen:

  1. --labels PFAD                              explizit angegebene Datei
  2. <datensatz>/originals/{MANUAL_LABELS_JSON}  mitgelieferte Kopie im Datensatz
  3. xai_viewer/{STORE_NAME.format(dataset='<datensatz>')}       der Labeling-Store dieser Maschine

Wer selbst gelabelt hat, braucht also weder Export noch Kopieren: labeln, dieses Skript
aufrufen, App neu starten. Die Export-Datei ist das Transportformat für
andere Rechner — sie passt als --labels PFAD oder als Kopie unter (2).
Ohne jede dieser Quellen bleiben die Filter-Attribute gemockt (Provenance "mock").

Welche Quelle benutzt wurde, gibt das Skript beim Lauf aus.
"""


def main():
    parser = argparse.ArgumentParser(
        description="metadata.json für einen Datensatz erzeugen",
        epilog=LABEL_SOURCE_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("dataset", nargs="?", default="RailPer", help="Datensatzname (Default: RailPer)")
    parser.add_argument("--root", type=Path, default=DEFAULT_DATASETS_ROOT,
                        help=f"Datensatz-Wurzel (Default: {DEFAULT_DATASETS_ROOT})")
    parser.add_argument("--labels", type=Path, default=None, metavar="PFAD",
                        help="Handlabels aus dieser Datei lesen (Export ODER Labeling-Store; "
                             "das Format wird am Inhalt erkannt). Ohne die Angabe siehe unten.")
    args = parser.parse_args()

    if args.labels is not None and not args.labels.is_file():
        raise SystemExit(f"--labels: Datei nicht gefunden: {args.labels}")

    dataset_dir = (args.dataset if Path(args.dataset).is_absolute()
                   else args.root / args.dataset)
    dataset_dir = Path(dataset_dir)
    if not dataset_dir.is_dir():
        raise SystemExit(f"Datensatz-Verzeichnis nicht gefunden: {dataset_dir}")

    metadata = build(dataset_dir, args.labels)
    out_path = dataset_dir / "metadata.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)

    n_pred = sum(len(lvls) for lvls in metadata["predictions"].values())
    n_scene = sum(1 for img in metadata["images"] if img["provenance"].get("objekte") == "scene")
    n_manual = sum(1 for img in metadata["images"] if img["provenance"].get("objekte") == "manual")
    extra = ([f"{n_manual} handgelabelt"] if n_manual else []) + \
            ([f"{n_scene} mit echten Szenen-Attributen"] if n_scene else [])
    print(f"Datensatz '{metadata['dataset']}': {len(metadata['images'])} Bilder"
          + (f" ({', '.join(extra)})" if extra else ""))
    print(f"Modelle: {metadata['models']}  Stufen: {metadata['levels']}")
    print(f"XAI-Verfügbarkeit: {metadata['xai_availability']}")
    print(f"Prediction-Tabellen (Modell×Stufe): {n_pred}")
    print(f"Geschrieben: {out_path}")


if __name__ == "__main__":
    main()

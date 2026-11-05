"""
Filter categories for the XAI viewer.

Deliberately a separate, easily modifiable module (see brainstorming doc 3.6).
Contains only the *structure* (keys, value tokens, order); displayable labels are in the
i18n catalogue (``i18n/<lang>.toml`` under ``filter.*``) – this module stays free of
Flask/session dependencies.

A dataset may bring **its own** categories: where `metadata.json` carries a
``filter_categories`` block (it does when the dataset was labelled by hand, see DATA.md),
``filter_categories()`` returns those instead of the constants below, together with the
labels the labeller wrote. The i18n catalogue still wins where it knows a key – so the
built-in set stays translated, and a foreign dataset falls back to its own wording.
``FILTER_CATEGORIES`` is therefore the *default*, not the truth.
"""

# Categorical filters: key -> {options (ordered tokens)}. Every category is multi-valued: an
# image attribute is a *list* of tokens, because the manual labelling showed that every one of
# them genuinely occurs several times per image (a scene is semi-urban *and* a level crossing,
# it is dawn *and* indoor).
# Label per token: i18n key filter.cat.<key>.label or filter.cat.<key>.opt.<token>.
FILTER_CATEGORIES = {
    "umgebung": {
        "options": ["ueberland", "semi_urban", "stadt", "bahnhof", "tunnel", "bahnuebergang"],
    },
    "objekte": {
        "options": ["mensch", "gebaeude", "signal", "andere_zuege", "ueberwiegend_vegetation",
                    "gewaesser", "schutzwand", "bruecke"],
    },
    "tageszeit": {
        "options": ["tag", "daemmerung", "nacht", "indoor"],
    },
    "wetter": {
        "options": ["sonnig", "bewoelkt", "regen", "schnee", "nebel", "gegenlicht"],
    },
}

# How the ticked options of one category combine: "or" (one suffices, the default) or "and" (all
# required, possible because every category is multi-valued). Kept in a sub-dict of the filter
# configuration and only for categories deviating from the default: categories belong to the
# dataset, so a flat key next to them could collide, and panels or notes saved before this
# setting existed simply read as "or".
CATEGORY_MODES_KEY = "category_modes"
CATEGORY_MODE_OR = "or"
CATEGORY_MODE_AND = "and"

# Excluded options per category (`{key: [token]}`): an image carrying any of them drops out,
# whatever the category's mode — the mode only combines the *ticked* options. Same sub-dict
# reasoning as CATEGORY_MODES_KEY; configurations without it exclude nothing. An image without
# the token counts as "without" it, also when the category is empty or missing for that image:
# the same closed-world reading the ticked options have, and all datasets label every category
# (manual or scene attributes), so "empty" means "labelled, none".
EXCLUDED_KEY = "excluded"

# Prediction-based filter: not derived from image attributes but from the panel's prediction
# (model + level) → applied in _panel_images, not in image_passes_filter().
# Single-select; empty value = no restriction. Labels:
# i18n key filter.classification.label or filter.classification.opt.<token> ("" -> "all").
CLASSIFICATION_FILTER = {
    "key": "klassifikation",
    "options": ["", "korrekt", "inkorrekt"],
}

# Confidence filter (range in percent): like CLASSIFICATION_FILTER prediction-based, not an image
# attribute – it is the confidence of the PREDICTED class (model + level of the panel), so it is
# applied in _confidence_matches()/_panel_images, not in image_passes_filter().
# Unlike the distance ranges it carries an explicit on/off flag: "0–100 %" is a meaningful
# restriction of its own (it drops images without a prediction), so "range = full" cannot double
# as "inactive". Labels: i18n keys filter.confidence.*.
CONFIDENCE_FILTER = {
    "key": "konfidenz",
    "active_key": "konfidenz_aktiv",
    "min": 0,
    "max": 100,
    "step": 0.1,  # 99.9 % must be expressible
}

# Distance filter (slider with min/max): key -> {min, max}. Label/unit: i18n key
# filter.dist.<key>.label or filter.dist.<key>.unit.
DISTANCE_FILTERS = {
    "dist_lateral_m": {"min": 0, "max": 20},
    "dist_longitudinal_m": {"min": 0, "max": 1000},
}


def filter_categories(meta: dict) -> dict:
    """Categories in effect for a dataset: ``key -> {"options": [token], "label", "labels"}``.

    ``label``/``labels`` are the dataset's own wording (empty for the built-in set, where the
    i18n catalogue is the better source). Falls back to FILTER_CATEGORIES when `metadata.json`
    carries no ``filter_categories`` block.
    """
    own = (meta or {}).get("filter_categories")
    if not own:
        return {key: {"options": list(spec["options"]), "label": "", "labels": {}}
                for key, spec in FILTER_CATEGORIES.items()}
    return {
        category["key"]: {
            "options": [o["token"] for o in category["options"]],
            "label": category.get("label") or "",
            "labels": {o["token"]: o.get("label") or o["token"] for o in category["options"]},
        }
        for category in own
    }


def empty_filter_config(categories: dict = None):
    """Empty filter configuration: no restriction."""
    categories = FILTER_CATEGORIES if categories is None else categories
    cfg = {key: [] for key in categories}
    cfg[CATEGORY_MODES_KEY] = {}
    cfg[EXCLUDED_KEY] = {}
    for key, spec in DISTANCE_FILTERS.items():
        cfg[key] = [spec["min"], spec["max"]]
    cfg[CLASSIFICATION_FILTER["key"]] = ""
    cfg[CONFIDENCE_FILTER["active_key"]] = False
    cfg[CONFIDENCE_FILTER["key"]] = [CONFIDENCE_FILTER["min"], CONFIDENCE_FILTER["max"]]
    return cfg


def attribute_tokens(attributes: dict, key: str) -> list:
    """Tokens an image carries in a category – tolerant of the pre-2026-08 single-valued format
    where the attribute was a bare string (a `metadata.json` that has not been rebuilt yet)."""
    value = attributes.get(key)
    if value is None:
        return []
    return list(value) if isinstance(value, (list, tuple, set)) else [value]


def category_mode(filter_config: dict, key: str) -> str:
    """How the ticked options of a category combine; configurations without the setting mean OR."""
    return filter_config.get(CATEGORY_MODES_KEY, {}).get(key, CATEGORY_MODE_OR)


def excluded_options(filter_config: dict, key: str) -> list:
    """Options of a category the image must NOT carry; configurations without the setting: none."""
    return filter_config.get(EXCLUDED_KEY, {}).get(key, [])


def image_passes_filter(attributes: dict, filter_config: dict, skip_key: str = None,
                        categories: dict = None) -> bool:
    """Checks whether an image (via its attributes) satisfies the filter configuration.

    Semantics: within a category OR or AND (per category, see category_mode()), between
    categories always AND. Excluded options (EXCLUDED_KEY) drop every image carrying one.
    Empty selection in a category = no restriction.
    Distances: a narrowed range also drops images that carry no value for it (see below).
    `skip_key`: ignore the *ticked* options of this category when checking (for faceted counts,
    to exclude the own facet) — its exclusions still apply. The prediction filter
    `klassifikation` is not handled here.
    """
    for key in (FILTER_CATEGORIES if categories is None else categories):
        tokens = set(attribute_tokens(attributes, key))
        if tokens & set(excluded_options(filter_config, key)):
            return False
        if key == skip_key:
            continue
        selected = filter_config.get(key, [])
        if not selected:
            continue
        if category_mode(filter_config, key) == CATEGORY_MODE_AND:
            if not set(selected) <= tokens:
                return False
        elif not set(selected) & tokens:
            return False

    for key, spec in DISTANCE_FILTERS.items():
        lo, hi = filter_config.get(key, [spec["min"], spec["max"]])
        value = attributes.get(key)
        if value is None:
            # No value at all – a synthetic scene without a person has no distance to one.
            # While the slider sits at its full range it restricts nothing and the image stays.
            # Narrowed, it is a question about a person ("which images have one within 2 m"),
            # and an image that cannot answer it is not an answer: it drops out. Otherwise the
            # whole negative set would ride along in every distance filter.
            if [lo, hi] != [spec["min"], spec["max"]]:
                return False
            continue
        if not (lo <= value <= hi):
            return False

    return True

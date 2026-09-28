"""
Generates a **small synthetic dataset** `datasets/mock/` in exactly the raw structure
of a real dataset (RailPer layout), so that the same `tools/build_metadata.py` can process it
and tests/dev have a lightweight fixture.

Intentionally small (few images), deterministic (fixed seed). Mixes jpg/png and
the two negative-class spellings (`not person` / `no person`) to exercise
normalisation (A13) as well.

Usage:  python tools/make_mock_dataset.py   (from `xai_viewer/`)
"""

import csv
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
XAI_DIR = HERE.parent
DATASETS_ROOT = XAI_DIR.parent.parent / "datasets"
MOCK_DIR = DATASETS_ROOT / "mock"

MODELS = ["VGG16", "ResNet50", "ConvNeXt-T"]
METHOD = "Grad-CAM"
LEVELS = ["low", "mid", "high"]
LEVEL_CORRECT_PROB = {"low": 0.6, "mid": 0.8, "high": 0.95}

# CRP (concept structure) exists — like the real data — only for one model/level: a GLOBAL pool of
# concept prototypes (concepts/) + per-image heatmaps (concept_heatmaps/) + a concepts.csv manifest.
CRP_MODEL, CRP_LEVEL, CRP_N_CONCEPTS, CRP_CONCEPT_POOL = "VGG16", "high", 3, 8

# CRAFT (only one model/level): a per-image attribution map in concept_attribution_maps/
# + a small set of GLOBAL concept prototypes in concepts/ (shared across all images). See DATA.md.
CRAFT_MODEL, CRAFT_LEVEL, CRAFT_N_CONCEPTS = "VGG16", "high", 3
# Fixed per-concept colour + global importance for the mock CRAFT manifest (concepts.csv).
CRAFT_CONCEPT_META = {0: ("#d62728", 0.5), 1: ("#2ca02c", 0.3), 2: ("#1f77b4", 0.2)}

# Model metadata: plausible per-model constants + per-level training facts. Feeds
# model_meta.csv (one row per model×level) → metadata.json model_meta → the modelcard block.
MODEL_META_CONST = {
    "VGG16":      {"params": 138357544, "algorithm": "SGD (lr=1e-2, momentum=0.9)"},
    "ResNet50":   {"params": 25557032,  "algorithm": "Adam (lr=1e-4)"},
    "ConvNeXt-T": {"params": 28589128,  "algorithm": "AdamW (lr=4e-3)"},
}
MODEL_META_LEVEL = {  # level → (epochs, trained_at, nominal_accuracy)
    "low":  (5,  "2026-05-04", 0.72),
    "mid":  (20, "2026-05-11", 0.86),
    "high": (60, "2026-05-19", 0.96),
}
MODEL_META_TRAIN_DS = "mock-train"
MODEL_META_TEST_DS = "mock-test"

N_IMAGES = 8           # 4 person / 4 not_person
PNG_INDICES = {6, 7}   # diese Bilder als PNG, Rest JPG
SIZE = (80, 60)
PERSON_BBOX = (30, 15, 50, 55)


def basename(i: int) -> str:
    return f"mock_{i:04d}"


def make_original(person: bool, tint=None) -> Image.Image:
    img = Image.new("RGB", SIZE, (60, 70, 80))
    draw = ImageDraw.Draw(img)
    if person:
        draw.rectangle(PERSON_BBOX, fill=(210, 180, 140))
    if tint is not None:
        overlay = Image.new("RGB", SIZE, tint)
        img = Image.blend(img, overlay, 0.35)
    return img


def write_csv(path: Path, header: list, rows: list):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def generate():
    rng = random.Random(2026)

    imgs_dir = MOCK_DIR / "originals" / "imgs"
    annots_dir = MOCK_DIR / "originals" / "annots"
    inference_dir = MOCK_DIR / "inference"
    for d in (imgs_dir, annots_dir, inference_dir):
        d.mkdir(parents=True, exist_ok=True)

    # Model/level-specific tinting of the XAI images (for visual distinction only).
    tints = {
        ("VGG16", "low"): (200, 40, 40), ("VGG16", "mid"): (200, 90, 40),
        ("VGG16", "high"): (200, 140, 40), ("ResNet50", "low"): (40, 160, 60),
        ("ResNet50", "mid"): (40, 160, 120), ("ResNet50", "high"): (40, 160, 180),
        ("ConvNeXt-T", "low"): (60, 60, 200), ("ConvNeXt-T", "mid"): (110, 60, 200),
        ("ConvNeXt-T", "high"): (160, 60, 200),
    }

    meta = []  # (basename, person, ext)
    label_rows = []
    for i in range(N_IMAGES):
        bn = basename(i)
        person = i % 2 == 0
        ext = "png" if i in PNG_INDICES else "jpg"
        meta.append((bn, person, ext))

        make_original(person).save(imgs_dir / f"{bn}.{ext}")

        annotations = []
        if person:
            x0, y0, x1, y1 = PERSON_BBOX
            annotations.append({
                "bbox": {"x_min": x0, "y_min": y0, "x_max": x1, "y_max": y1},
                "class": "person",
            })
        annot = {
            "metadata": {
                "dataset_name": "MockSet",
                "original_image_name": f"{bn}.{ext}",
                "scene_name": "synthetic",
            },
            "annotations": annotations,
        }
        with open(annots_dir / f"{bn}.json", "w", encoding="utf-8") as f:
            json.dump(annot, f, ensure_ascii=False, indent=2)

        # all_labels: negative class as "not person" (A13: different spelling from ground_truth).
        label_rows.append([f"{bn}.{ext}", "person" if person else "not person"])

    write_csv(MOCK_DIR / "originals" / "all_labels.csv", ["image_name", "label"], label_rows)

    # XAI images: xai/<Model>/<Method>/<level>/<basename>_gradcam.<ext>
    for model in MODELS:
        for level in LEVELS:
            leaf = MOCK_DIR / "xai" / model / METHOD / level
            leaf.mkdir(parents=True, exist_ok=True)
            for bn, person, ext in meta:
                xai_img = make_original(person, tint=tints[(model, level)])
                xai_img.save(leaf / f"{bn}_gradcam.{ext}")

    # CRP concept structure (only CRP_MODEL/CRP_LEVEL, mirroring the real data): a GLOBAL pool of
    # concept prototypes in concepts/ (concept<id>_3x3.jpg, shared across images) + per-image
    # heatmaps in concept_heatmaps/ (<bn>_concept<id>_rank<r>_crp.jpg) + a concepts.csv manifest.
    # Relevance is SIGNED; rank is by |relevance| descending (rank 1 = most influential). See DATA.md.
    crp_leaf = MOCK_DIR / "xai" / CRP_MODEL / "CRP" / CRP_LEVEL
    heatmaps_dir = crp_leaf / "concept_heatmaps"
    crp_concepts_dir = crp_leaf / "concepts"
    heatmaps_dir.mkdir(parents=True, exist_ok=True)
    crp_concepts_dir.mkdir(parents=True, exist_ok=True)
    crp_pool = sorted(random.Random("crp-pool").sample(range(100, 500), CRP_CONCEPT_POOL))
    for cid in crp_pool:
        make_original(False, tint=(90, 90, 90)).save(crp_concepts_dir / f"concept{cid}_3x3.jpg")
    concept_rows = []
    for bn, person, ext in meta:
        crng = random.Random(f"crp-{bn}")
        picks = crng.sample(crp_pool, CRP_N_CONCEPTS)
        ranked = sorted(((cid, round(crng.uniform(-0.5, 0.5), 4)) for cid in picks),
                        key=lambda t: -abs(t[1]))
        for rank, (cid, rel) in enumerate(ranked, start=1):
            tint = (200, 60, 60) if rank == 1 else (120, 120, 120)
            make_original(person, tint=tint).save(
                heatmaps_dir / f"{bn}_concept{cid}_rank{rank}_crp.jpg")
            concept_rows.append([bn, rank, cid, f"{rel:.4f}"])
    write_csv(crp_leaf / "concepts.csv",
              ["image_name", "rank", "concept_id", "relevance"], concept_rows)

    # CRAFT (only CRAFT_MODEL/CRAFT_LEVEL): per-image attribution map in concept_attribution_maps/
    # (a normal single overlay, like Grad-CAM/LRP) + a few GLOBAL concept prototypes in concepts/
    # (shared across all images, unlike CRP's per-image concepts).
    craft_leaf = MOCK_DIR / "xai" / CRAFT_MODEL / "CRAFT" / CRAFT_LEVEL
    maps_dir = craft_leaf / "concept_attribution_maps"
    concepts_dir = craft_leaf / "concepts"
    maps_dir.mkdir(parents=True, exist_ok=True)
    concepts_dir.mkdir(parents=True, exist_ok=True)
    for bn, person, ext in meta:
        make_original(person, tint=(180, 60, 200)).save(maps_dir / f"{bn}_craft.jpg")
    for n in range(CRAFT_N_CONCEPTS):
        make_original(False, tint=(60 + n * 50, 160, 60)).save(concepts_dir / f"concept{n}.jpg")
    # Per-image CRAFT manifest: activation per concept (drives rank), constant global_importance +
    # colour per concept. Columns match Kilian's export (see DATA.md).
    craft_rows = []
    for bn, person, ext in meta:
        crng = random.Random(f"craft-{bn}")
        acts = sorted(((cid, round(crng.uniform(0.0, 1.0), 4)) for cid in range(CRAFT_N_CONCEPTS)),
                      key=lambda t: -t[1])
        for rank, (cid, act) in enumerate(acts, start=1):
            color, gimp = CRAFT_CONCEPT_META[cid]
            craft_rows.append([bn, rank, cid, f"{act:.4f}", gimp, color])
    write_csv(craft_leaf / "concepts.csv",
              ["image_name", "rank", "concept_id", "activation_score", "global_importance", "concept_color"],
              craft_rows)

    # Inference CSVs: one per (model, level).
    for model in MODELS:
        for level in LEVELS:
            rows = []
            for bn, person, ext in meta:
                correct = rng.random() < LEVEL_CORRECT_PROB[level]
                predicted_person = person if correct else not person
                sigmoid = (rng.uniform(0.7, 0.99) if predicted_person
                           else rng.uniform(0.001, 0.3))
                classification = "person" if predicted_person else "not person"
                # ground_truth intentionally as "no person" (A13: differs from all_labels' "not person").
                ground_truth = "person" if person else "no person"
                rows.append([f"{bn}.{ext}", f"{sigmoid:.6f}", classification, ground_truth])
            write_csv(
                inference_dir / f"inference_results_{model}_{level}.csv",
                ["image_name", "sigmoid_output", "classification", "ground_truth"],
                rows,
            )

    # Model metadata: one row per model×level (constants repeat per level).
    meta_rows = []
    for model in MODELS:
        const = MODEL_META_CONST[model]
        for level in LEVELS:
            epochs, trained_at, accuracy = MODEL_META_LEVEL[level]
            meta_rows.append([model, level, const["params"], epochs, trained_at,
                              const["algorithm"], MODEL_META_TRAIN_DS, MODEL_META_TEST_DS, accuracy])
    write_csv(
        MOCK_DIR / "model_meta.csv",
        ["model", "level", "params", "epochs", "trained_at", "algorithm",
         "train_dataset", "test_dataset", "nominal_accuracy"],
        meta_rows,
    )

    print(f"Mock-Datensatz erzeugt: {MOCK_DIR}")
    print(f"{N_IMAGES} Bilder, Modelle {MODELS}, Stufen {LEVELS}, Methode {METHOD}")
    print(f"CRP: {CRP_MODEL}/{CRP_LEVEL}, {CRP_N_CONCEPTS} Konzepte/Bild + concepts.csv")
    print(f"CRAFT: {CRAFT_MODEL}/{CRAFT_LEVEL}, Attribution-Maps + {CRAFT_N_CONCEPTS} globale Konzepte")


if __name__ == "__main__":
    generate()

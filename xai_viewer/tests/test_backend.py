"""
Backend tests using Flask's test_client (no browser involved).

Tests for the panel concept (usage concept v2): workspace, panel management,
filter application, and the entry flow (login, role selection, project).
"""

import contextlib
import glob
import io
import json
import os
import re
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
import docs_render
import legal
from app import (COLLECTIONS_STORE, MODELCARD_META_FIELDS, MODELCARD_META_NA, MODELCARD_OPTION,
                 NOTES_STORE, REQUIREMENTS, REQUIREMENTS_MASTER_PATH, REQUIREMENTS_STORE,
                 _auto_collection_title, _auto_title,
                 _confidence_matches, _matrix_cell_images, _model_metadata, _modelcard_stats,
                 _predicted_confidence, app as flask_app, load_metadata)
from notes import NotesStore
import tracking
from tracking import TrackingStore
from requirements_catalog import label_for, load_requirements, parse_requirements, text_for
from requirements_store import RequirementsStore
from version import __product__, __version__
from filter_options import (CLASSIFICATION_FILTER, CONFIDENCE_FILTER, DISTANCE_FILTERS,
                            FILTER_CATEGORIES, attribute_tokens, empty_filter_config,
                            filter_categories, image_passes_filter)
from i18n import CATALOGS, LANGUAGES
from thumbnails import iter_source_images
from tools.build_metadata import (MANUAL_LABELS_JSON, MANUAL_LABELS_SCHEMA, MAX_FILTER_CATEGORIES,
                            manual_attributes, read_export_labels, read_store_labels,
                            resolve_manual_labels)
from tools import analyze_tracking

XAI_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGAL_EXAMPLE = Path(XAI_DIR) / "legal.example.toml"

# Tests run against the small mock fixture (datasets/mock).
TEST_DATASET = "mock"
# The owner key of the test session. Notes and collections are owned by it; the ``user`` field
# on the fixtures below is only the displayed author (see auth.py).
OWNER = "user:testuser"
# What the footer prints: product name (version.py) plus the demonstrator note (i18n).
PRODUCT_FOOTER = f"{__product__} (Demonstrator)"
METADATA = load_metadata(TEST_DATASET)
XAI_AVAILABILITY = METADATA["xai_availability"]

# ── helpers: derive valid test fixtures from current metadata ─────────────────

# An XAI method that is NOT available for the first model (for the fallback test).
_TEST_MODEL = METADATA["models"][0]
_UNAVAILABLE_METHOD = next(
    m for m in ("LRP", "CRAFT", "CRP", "Grad-CAM")
    if m not in XAI_AVAILABILITY.get(_TEST_MODEL, {})
)

_UMGEBUNG_COUNTS = {}
for img in METADATA["images"]:
    for _token in attribute_tokens(img["attributes"], "umgebung"):
        _UMGEBUNG_COUNTS[_token] = _UMGEBUNG_COUNTS.get(_token, 0) + 1
_FILTER_UMGEBUNG, _FILTER_COUNT = min(_UMGEBUNG_COUNTS.items(), key=lambda kv: kv[1])


def _page_data(html: str) -> dict:
    match = re.search(r'<script id="data-page"[^>]*>(.*?)</script>', html, re.DOTALL)
    assert match, "data-page script not found"
    return json.loads(match.group(1))


def _auth_session(client):
    """Sets up a complete auth session (login + role + project)."""
    with client.session_transaction() as sess:
        sess["user"] = "testuser"
        sess["owner"] = OWNER
        sess["role"] = "validierer"
        sess["project"] = {
            "name": "Testprojekt",
            "type": "test",
            "dataset": TEST_DATASET,
            "created_at": date.today().isoformat(),
        }


def _configure(client, pid, **fields):
    """Simulates clicking "Apply": a single POST to /config marks the panel as configured
    so its gallery is loaded. Without `fields`, model/level/XAI are kept (server falls back
    to current values) and filters are cleared. Required since new panels only load their
    gallery after the first config confirmation (develop: fcc4efa)."""
    return client.post(f"/panel/{pid}/config", data=fields)


# ── Setup-Flow ────────────────────────────────────────────────────────────────

class TestSetupFlow(unittest.TestCase):

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()

    def test_b12__login_page_returns_200(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Anmelden", resp.data)

    def test_b13__workspace_without_session_redirects_to_login(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_b14__login_post_with_username_redirects_to_role(self):
        """No account, no password → guest entry, as before (see auth.py)."""
        resp = self.client.post("/login", data={"username": "robin", "password": ""})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/role", resp.headers["Location"])

    def test_b15__login_post_empty_username_stays_on_login(self):
        resp = self.client.post("/login", data={"username": "", "password": ""})
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Anmelden", resp.data)

    def test_b16__role_page_without_login_redirects_to_login(self):
        resp = self.client.get("/role")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_b17__role_post_validierer_redirects_to_project(self):
        self.client.post("/login", data={"username": "robin"})
        resp = self.client.post("/role", data={"role": "validierer"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/project", resp.headers["Location"])

    def test_b17b__an_inactive_role_does_not_start_a_session(self):
        """The cards for the other two roles are disabled in the page — the route must refuse
        them as well, or a hand-made POST would walk past the display."""
        self.client.post("/login", data={"username": "robin"})
        resp = self.client.post("/role", data={"role": "gutachter"})
        self.assertEqual(resp.status_code, 200)  # stays on the page
        with self.client.session_transaction() as sess:
            self.assertNotIn("role", sess)

    def test_b18__project_page_without_role_redirects_to_role(self):
        self.client.post("/login", data={"username": "robin"})
        resp = self.client.get("/project")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/role", resp.headers["Location"])

    def test_b19__project_post_creates_project_and_clears_panels(self):
        self.client.post("/login", data={"username": "robin"})
        self.client.post("/role", data={"role": "validierer"})
        # First create panels that should be cleared when a new project is created
        with self.client.session_transaction() as sess:
            sess["panels"] = [{"id": 99}]
        resp = self.client.post("/project", data={"name": "Mein Projekt", "type": "pruefung"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/", resp.headers["Location"])
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["project"]["name"], "Mein Projekt")
            # The type selection is hidden, so a posted type is ignored (see b19a/b19b).
            self.assertEqual(sess["project"]["type"], app_module.DEFAULT_PROJECT_TYPE)
            self.assertNotIn("panels", sess)
            self.assertTrue(sess.get("open_panel1_config"))

    def test_b19a__project_page_hides_the_type_selection(self):
        self.client.post("/login", data={"username": "robin"})
        self.client.post("/role", data={"role": "validierer"})
        html = self.client.get("/project").data.decode()
        self.assertNotIn('name="type"', html)

    def test_b19b__posted_type_counts_again_once_the_selection_is_enabled(self):
        """Guards the way back: flipping the flag on must revive the choice end to end."""
        self.client.post("/login", data={"username": "robin"})
        self.client.post("/role", data={"role": "validierer"})
        orig = app_module.ALLOW_PROJECT_TYPE_SELECTION
        app_module.ALLOW_PROJECT_TYPE_SELECTION = True
        try:
            self.client.post("/project", data={"name": "P", "type": "pruefung"})
        finally:
            app_module.ALLOW_PROJECT_TYPE_SELECTION = orig
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["project"]["type"], "pruefung")

    def test_b20__project_post_uses_date_default_when_name_empty(self):
        self.client.post("/login", data={"username": "robin"})
        self.client.post("/role", data={"role": "validierer"})
        self.client.post("/project", data={"name": "", "type": "test"})
        with self.client.session_transaction() as sess:
            self.assertIn(str(date.today().year), sess["project"]["name"])

    def test_b20a__project_page_offers_the_datasets_present_on_the_server(self):
        self.client.post("/login", data={"username": "robin"})
        self.client.post("/role", data={"role": "validierer"})
        resp = self.client.get("/project")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'name="dataset"', resp.data)
        self.assertIn(TEST_DATASET.encode(), resp.data)

    def test_b20b__project_post_takes_the_chosen_dataset(self):
        self.client.post("/login", data={"username": "robin"})
        self.client.post("/role", data={"role": "validierer"})
        self.client.post("/project", data={"name": "P", "type": "test", "dataset": TEST_DATASET})
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["project"]["dataset"], TEST_DATASET)

    def test_b20c__project_post_rejects_a_dataset_outside_the_root(self):
        """A raw form value would reach _dataset_dir() as a path fragment → whitelist it."""
        self.client.post("/login", data={"username": "robin"})
        self.client.post("/role", data={"role": "validierer"})
        self.client.post("/project", data={"name": "P", "type": "test", "dataset": "../../etc"})
        with self.client.session_transaction() as sess:
            self.assertNotIn("..", sess["project"]["dataset"])

    def test_b21__workspace_after_setup_returns_200_with_config_modal(self):
        self.client.post("/login", data={"username": "robin"})
        self.client.post("/role", data={"role": "validierer"})
        # Explicitly the fixture: without a dataset the default (RailPer) applies, which only exists
        # on machines that carry the real data — a fresh clone or CI has just `mock`.
        self.client.post("/project", data={"name": "Analyse", "type": "test", "dataset": TEST_DATASET})
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        # Config modal should be injected on the first visit
        self.assertIn(b"cfg-model", resp.data)

    def test_b22__config_modal_flag_consumed_on_second_visit(self):
        self.client.post("/login", data={"username": "robin"})
        self.client.post("/role", data={"role": "validierer"})
        self.client.post("/project", data={"name": "Analyse", "type": "test"})
        self.client.get("/")  # first load – consumes the flag
        resp = self.client.get("/")  # second load – no modal any more
        # modal_html is empty → no cfg-model in the <script> block
        html = resp.data.decode()
        script_blocks = re.findall(r"<script>(.*?)</script>", html, re.DOTALL)
        modal_injected = any("cfg-model" in b for b in script_blocks)
        self.assertFalse(modal_injected)

    def test_b23__logout_clears_session_and_redirects_to_login(self):
        _auth_session(self.client)
        resp = self.client.post("/logout")
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        with self.client.session_transaction() as sess:
            self.assertNotIn("user", sess)
            self.assertNotIn("role", sess)
            self.assertNotIn("project", sess)


# ── Workspace + Panel Management ──────────────────────────────────────────────

class TestWorkspace(unittest.TestCase):

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        _auth_session(self.client)

    def test_b01__workspace_returns_200_and_creates_default_panel(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        data = _page_data(resp.data.decode())
        self.assertEqual(data["panel_count"], 1)

    def test_b158__default_panel_prefers_convnext_then_resnet_then_vgg(self):
        def model_for(models):
            return app_module._default_panel(1, {**METADATA, "models": models})["model"]
        self.assertEqual(model_for(["VGG16", "ResNet50", "ConvNeXt-T"]), "ConvNeXt-T")
        self.assertEqual(model_for(["VGG16", "ResNet50"]), "ResNet50")
        self.assertEqual(model_for(["VGG16"]), "VGG16")
        self.assertEqual(model_for(["ResNet18"]), "ResNet18")

    def test_b02__panel_new_adds_panel(self):
        self.client.get("/")
        resp = self.client.post("/panel/new")
        data = _page_data(resp.data.decode())
        self.assertEqual(data["panel_count"], 2)

    def test_b03__config_apply_filters_image_set(self):
        self.client.get("/")
        resp = self.client.post("/panel/1/config", data={
            "model": "VGG16",
            "level": "high",
            "xai_method": "original",
            "filter_umgebung": _FILTER_UMGEBUNG,
        })
        self.assertEqual(resp.status_code, 200)
        expected = f"{_FILTER_COUNT} " + ("Bild" if _FILTER_COUNT == 1 else "Bilder")
        self.assertIn(expected, resp.data.decode())

    def test_b04__incompatible_xai_method_falls_back_to_original(self):
        self.client.get("/")
        resp = self.client.post("/panel/1/config", data={
            "model": _TEST_MODEL,
            "level": "high",
            "xai_method": _UNAVAILABLE_METHOD,
        })
        self.assertIn("· Original", resp.data.decode())

    def test_b05__duplicate_copies_configuration(self):
        self.client.get("/")
        self.client.post("/panel/1/config", data={
            "model": "ResNet50",
            "level": "mid",
            "xai_method": "Grad-CAM",
            "filter_umgebung": _FILTER_UMGEBUNG,
        })
        resp = self.client.post("/panel/1/duplicate")
        data = _page_data(resp.data.decode())
        self.assertEqual(data["panel_count"], 2)
        headers = re.findall(r"P\d+ · ResNet50 · mid\s*· Grad-CAM", resp.data.decode())
        self.assertEqual(len(headers), 2)

    def test_b06__delete_removes_panel(self):
        self.client.get("/")
        self.client.post("/panel/new")
        resp = self.client.post("/panel/2/delete")
        data = _page_data(resp.data.decode())
        self.assertEqual(data["panel_count"], 1)

    def test_b07__toggle_hides_panel_without_deleting(self):
        self.client.get("/")
        resp = self.client.post("/panel/1/toggle")
        data = _page_data(resp.data.decode())
        self.assertEqual(data["panel_count"], 1)
        self.assertEqual(data["visible_panel_ids"], [])

    def test_b08__config_dialog_returns_form(self):
        self.client.get("/")
        resp = self.client.get("/panel/1/config")
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Panel P1 konfigurieren", resp.data)

    def test_b09__crp_panel_renders_rank1_concept_heatmap(self):
        # CRP has no single overlay; the gallery tile is the rank-1 concept's heatmap
        # (Phase 1). Availability and tile path both come from metadata's xai_concepts.
        self.assertIn("high", XAI_AVAILABILITY.get("VGG16", {}).get("CRP", []))
        concepts = METADATA["xai_concepts"]["VGG16"]["CRP"]["high"]["mock_0000"]
        top = concepts[0]  # rank 1 = highest |relevance|

        self.client.get("/")
        resp = self.client.post("/panel/1/config", data={
            "model": "VGG16", "level": "high", "xai_method": "CRP",
        })
        html = resp.data.decode()
        # Header reflects CRP (no fallback to Original) and the tile is the rank-1 heatmap.
        self.assertRegex(html, r"P\d+ · VGG16 · high\s*· CRP")
        self.assertIn(
            f"xai/VGG16/CRP/high/concept_heatmaps/mock_0000_concept{top['concept_id']}_rank{top['rank']}_crp.jpg",
            html)

    def test_b111__crp_single_view_shows_rank1_heatmap_and_dropdowns(self):
        # Single view keeps ONE image (default: rank-1 heatmap) plus a concept dropdown (by rank)
        # and an image-type dropdown – it does not stack all concept images (panel logic intact).
        concepts = METADATA["xai_concepts"]["VGG16"]["CRP"]["high"]["mock_0000"]
        top = concepts[0]
        self.client.get("/")
        self.client.post("/panel/1/config", data={
            "model": "VGG16", "level": "high", "xai_method": "CRP",
        })
        html = self.client.post("/panel/1/open/0").data.decode()  # index 0 == mock_0000
        self.assertIn('name="crp_rank"', html)   # concept dropdown present
        self.assertIn('name="crp_view"', html)   # image-type dropdown present
        self.assertIn(  # default image = rank-1 per-image heatmap
            f"concept_heatmaps/mock_0000_concept{top['concept_id']}_rank{top['rank']}_crp.jpg", html)
        for c in concepts:                        # every rank offered as an option
            self.assertIn(f'value="{c["rank"]}"', html)

    def test_b112__crp_dropdowns_switch_concept_and_image_type(self):
        concepts = METADATA["xai_concepts"]["VGG16"]["CRP"]["high"]["mock_0000"]
        r2 = concepts[1]  # rank 2
        self.client.get("/")
        self.client.post("/panel/1/config", data={
            "model": "VGG16", "level": "high", "xai_method": "CRP",
        })
        self.client.post("/panel/1/open/0")
        # pick concept rank 2 → shown image becomes its per-image heatmap
        html = self.client.post("/panel/1/crp-select", data={"crp_rank": "2"}).data.decode()
        self.assertIn(f"concept_heatmaps/mock_0000_concept{r2['concept_id']}_rank{r2['rank']}_crp.jpg", html)
        # switch image type to the example grid → shown image becomes the GLOBAL prototype of that concept
        html = self.client.post("/panel/1/crp-select", data={"crp_view": "grid"}).data.decode()
        self.assertIn(f"concepts/concept{r2['concept_id']}_3x3.jpg", html)

    def test_b113__crp_stepping_keeps_concept_and_view(self):
        # Stepping to another image KEEPS the selected rank AND image type (comparison across
        # images); only opening a gallery image resets them.
        self.client.get("/")
        self.client.post("/panel/1/config", data={
            "model": "VGG16", "level": "high", "xai_method": "CRP",
        })
        self.client.post("/panel/1/open/0")
        self.client.post("/panel/1/crp-select", data={"crp_rank": "2"})
        self.client.post("/panel/1/crp-select", data={"crp_view": "grid"})
        html = self.client.post("/panel/1/step/next").data.decode()  # → image index 1
        nxt = METADATA["images"][1]["id"]
        nxt_r2 = METADATA["xai_concepts"]["VGG16"]["CRP"]["high"][nxt][1]  # rank 2 of the new image
        nxt_top = METADATA["xai_concepts"]["VGG16"]["CRP"]["high"][nxt][0]
        # still rank 2 + grid → the global prototype of the new image's rank-2 concept
        self.assertIn(f"concepts/concept{nxt_r2['concept_id']}_3x3.jpg", html)
        # NOT reset to the rank-1 heatmap
        self.assertNotIn(
            f"concept_heatmaps/{nxt}_concept{nxt_top['concept_id']}_rank{nxt_top['rank']}_crp.jpg", html)

    def test_b114__craft_panel_renders_attribution_map(self):
        # CRAFT's per-image attribution map is a normal single overlay (like Grad-CAM/LRP), but it
        # lives in the concept_attribution_maps/ sub-folder of the level dir.
        self.assertIn("high", XAI_AVAILABILITY.get("VGG16", {}).get("CRAFT", []))
        self.client.get("/")
        html = self.client.post("/panel/1/config", data={
            "model": "VGG16", "level": "high", "xai_method": "CRAFT",
        }).data.decode()
        self.assertRegex(html, r"P\d+ · VGG16 · high\s*· CRAFT")
        self.assertIn("xai/VGG16/CRAFT/high/concept_attribution_maps/mock_0000_craft.jpg", html)

    def test_b115__craft_dropdown_lists_all_concept_colours_ordered_by_activation(self):
        # Single view keeps ONE image (default: attribution map). A custom dropdown lists every
        # concept with its colour swatch (match a coloured map region to its concept), ordered by
        # the image's activation; selecting one shows its global prototype.
        glob = {c["concept_id"]: c for c in METADATA["xai_global_concepts"]["VGG16"]["CRAFT"]["high"]}
        ranked = METADATA["xai_concepts"]["VGG16"]["CRAFT"]["high"]["mock_0000"]  # by activation
        top = glob[ranked[0]["concept_id"]]
        self.client.get("/")
        self.client.post("/panel/1/config", data={
            "model": "VGG16", "level": "high", "xai_method": "CRAFT",
        })
        html = self.client.post("/panel/1/open/0").data.decode()  # default = map
        self.assertIn('data-bs-toggle="dropdown"', html)                     # custom dropdown
        self.assertIn("concept_attribution_maps/mock_0000_craft.jpg", html)  # default = map
        # EVERY concept's colour swatch is in the list (pick by colour)
        for g in glob.values():
            self.assertIn(f"background-color:{g['color']}", html)
        # menu ordered by activation: rank-1 concept before the last-ranked one
        self.assertLess(html.index(f'"craft_concept": "{ranked[0]["concept_id"]}"'),
                        html.index(f'"craft_concept": "{ranked[-1]["concept_id"]}"'))
        # selecting a concept → its global prototype is shown
        html = self.client.post("/panel/1/craft-select",
                                data={"craft_concept": str(top["concept_id"])}).data.decode()
        self.assertIn(f"xai/VGG16/CRAFT/high/concepts/{top['file']}", html)
        # back to the map
        html = self.client.post("/panel/1/craft-select", data={"craft_concept": "map"}).data.decode()
        self.assertIn("concept_attribution_maps/mock_0000_craft.jpg", html)

    def test_b115a__craft_dropdown_shows_per_image_activation_share(self):
        # Besides the global importance, each concept's per-image activation is shown as its SHARE of
        # the total concept activation in this image (raw score / sum, in %), a distribution summing
        # to ~100 % across concepts — no artificial 100 % maximum.
        ranked = METADATA["xai_concepts"]["VGG16"]["CRAFT"]["high"]["mock_0000"]  # by activation
        acts = [e["activation_score"] for e in ranked]
        total = sum(acts)
        self.client.get("/")
        self.client.post("/panel/1/config", data={
            "model": "VGG16", "level": "high", "xai_method": "CRAFT",
        })
        html = self.client.post("/panel/1/open/0").data.decode()
        # the share label ("Anteil … %") is present with each concept's own share
        for e in ranked:
            want = round(e["activation_score"] / total * 100, 1)
            self.assertIn(f"Anteil {want} %", html)

    def test_b116__craft_stepping_keeps_selected_concept(self):
        # Stepping keeps the selected concept, too (Q2b). CRAFT concepts are global, so the shown
        # prototype stays the same file across images; only opening a gallery image resets to the map.
        concepts = METADATA["xai_global_concepts"]["VGG16"]["CRAFT"]["high"]
        cid, cfile = concepts[0]["concept_id"], concepts[0]["file"]
        self.client.get("/")
        self.client.post("/panel/1/config", data={
            "model": "VGG16", "level": "high", "xai_method": "CRAFT",
        })
        self.client.post("/panel/1/open/0")
        self.client.post("/panel/1/craft-select", data={"craft_concept": str(cid)})
        html = self.client.post("/panel/1/step/next").data.decode()  # → image index 1
        nxt = METADATA["images"][1]["id"]
        # concept selection kept → still the (global) prototype, NOT reset to the attribution map
        self.assertIn(f"xai/VGG16/CRAFT/high/concepts/{cfile}", html)
        self.assertNotIn(f"concept_attribution_maps/{nxt}_craft.jpg", html)

    def test_b24__open_switches_panel_to_single_view(self):
        self.client.get("/")
        _configure(self.client, 1)
        resp = self.client.post("/panel/1/open/2")
        self.assertEqual(resp.status_code, 200)
        data = _page_data(resp.data.decode())
        self.assertEqual(data["single_panel_ids"], [1])
        body = resp.data.decode()
        self.assertIn("panel-single-fig", body)
        self.assertIn("3 / ", body)  # index 2 → position 3

    def test_b25__step_advances_single_view_index(self):
        self.client.get("/")
        _configure(self.client, 1)
        self.client.post("/panel/1/open/0")
        resp = self.client.post("/panel/1/step/next")
        self.assertIn("2 / ", resp.data.decode())

    def test_b26__gallery_returns_from_single_view(self):
        self.client.get("/")
        _configure(self.client, 1)
        self.client.post("/panel/1/open/0")
        resp = self.client.post("/panel/1/gallery")
        data = _page_data(resp.data.decode())
        self.assertEqual(data["single_panel_ids"], [])
        self.assertIn("panel-gallery", resp.data.decode())

    def test_b168__the_single_view_names_the_original_in_the_singular(self):
        """Only one image is on screen there, so the gallery's plural would be wrong."""
        self.client.get("/")
        _configure(self.client, 1, xai_method="original")
        gallery = self.client.post("/panel/1/gallery").get_data(as_text=True)
        self.assertIn("Original-Bilder", gallery)
        single = self.client.post("/panel/1/open/0").get_data(as_text=True)
        self.assertNotIn("Original-Bilder", single)
        self.assertIn("Original-Bild", single)
        # …and back: the gallery is plural again.
        self.assertIn("Original-Bilder", self.client.post("/panel/1/gallery").get_data(as_text=True))

    def test_b27__step_all_advances_linked_single_panels(self):
        self.client.get("/")
        self.client.post("/panel/new")  # Panel 2
        _configure(self.client, 1)
        _configure(self.client, 2)
        self.client.post("/panel/1/link")
        self.client.post("/panel/2/link")
        self.client.post("/panel/1/open/0")
        self.client.post("/panel/2/open/0")
        resp = self.client.post("/step-all/next")
        data = _page_data(resp.data.decode())
        self.assertEqual(sorted(data["linked_single_panel_ids"]), [1, 2])
        self.assertEqual(resp.data.decode().count("2 / "), 2)  # both at position 2

    def test_b175__a_linked_panels_own_arrows_step_all_linked_panels(self):
        """Coupling is symmetric: the panel's arrows do what the sync control does."""
        self.client.get("/")
        self.client.post("/panel/new")  # Panel 2
        _configure(self.client, 1)
        _configure(self.client, 2)
        self.client.post("/panel/1/link")
        self.client.post("/panel/2/link")
        self.client.post("/panel/1/open/0")
        self.client.post("/panel/2/open/0")
        body = self.client.post("/panel/1/step/next").get_data(as_text=True)
        self.assertEqual(body.count("2 / "), 2)  # both advanced, not just panel 1
        # An unlinked panel keeps its arrows private.
        self.client.post("/panel/2/link")        # unlink panel 2
        body = self.client.post("/panel/1/step/next").get_data(as_text=True)
        self.assertIn("3 / ", body)              # panel 1 advanced again
        self.assertIn("2 / ", body)              # panel 2 stayed behind

    def test_b28__step_all_skips_unlinked_single_panel(self):
        self.client.get("/")
        self.client.post("/panel/new")  # Panel 2
        _configure(self.client, 1)
        _configure(self.client, 2)
        self.client.post("/panel/1/link")          # only panel 1 linked
        self.client.post("/panel/1/open/0")
        self.client.post("/panel/2/open/0")
        resp = self.client.post("/step-all/next")
        data = _page_data(resp.data.decode())
        self.assertEqual(data["linked_single_panel_ids"], [1])
        body = resp.data.decode()
        self.assertIn("2 / ", body)   # panel 1 (linked) advanced
        self.assertIn("1 / ", body)   # panel 2 (unlinked) stays at position 1

    def test_b29__link_toggle_persists_and_returns_oob_only(self):
        self.client.get("/")
        _configure(self.client, 1)
        resp = self.client.post("/panel/1/link")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        # Nur OOB-Fragmente (synchron-Steuerung + page_data), keine teure Galerie
        self.assertIn('id="sync-control"', body)
        self.assertIn('id="data-page"', body)
        self.assertNotIn("panel-gallery", body)
        self.assertIn('data-linked="1"', self.client.get("/").get_data(as_text=True))
        self.client.post("/panel/1/link")  # toggle back off
        self.assertIn('data-linked="0"', self.client.get("/").get_data(as_text=True))

    def test_b36__linking_single_panels_reveals_sync_control_immediately(self):
        """Bug: sync buttons were missing after linking until an image was advanced."""
        self.client.get("/")
        self.client.post("/panel/new")        # Panel 2
        self.client.post("/panel/1/open/0")   # Panel 1 → Einzelbild
        self.client.post("/panel/2/open/0")   # Panel 2 → Einzelbild
        self.client.post("/panel/1/link")     # Panel 1 verlinken
        resp = self.client.post("/panel/2/link")  # Panel 2 verlinken → beide single+linked
        body = resp.data.decode()
        # sync controls present immediately (without prior image advance)
        self.assertIn("/step-all/next", body)
        # page_data OOB reports both as linked_single
        match = re.search(r'id="data-page"[^>]*>(.*?)</script>', body, re.S)
        page_data = json.loads(match.group(1))
        self.assertEqual(sorted(page_data["linked_single_panel_ids"]), [1, 2])

    def test_b40__open_couples_to_linked_panels(self):
        self.client.get("/")
        self.client.post("/panel/new")          # Panel 2
        _configure(self.client, 1)
        _configure(self.client, 2)
        self.client.post("/panels/link-all")    # beide koppeln
        body = self.client.post("/panel/1/open/2").get_data(as_text=True)  # EIN Klick
        data = _page_data(body)
        self.assertEqual(sorted(data["single_panel_ids"]), [1, 2])  # beide Einzelbild
        self.assertEqual(body.count("3 / "), 2)                     # beide auf Position 3

    def test_b41__coupled_open_clamps_index_per_panel(self):
        self.assertLess(_FILTER_COUNT, 8)       # Mock: gefilterte Menge < gesamt
        self.client.get("/")
        self.client.post("/panel/new")          # Panel 2 (ungefiltert, 8 Bilder)
        _configure(self.client, 2)
        self.client.post("/panel/1/config", data={
            "model": "VGG16", "level": "high", "xai_method": "original",
            "filter_umgebung": _FILTER_UMGEBUNG,           # Panel 1 → weniger Bilder
        })
        self.client.post("/panels/link-all")    # beide koppeln
        body = self.client.post("/panel/2/open/7").get_data(as_text=True)  # Bild 8 in Panel 2
        self.assertEqual(body.count("panel-single-fig"), 2)        # beide Einzelbild
        self.assertIn("8 / 8", body)                               # Panel 2 am Ende
        self.assertIn(f"{_FILTER_COUNT} / {_FILTER_COUNT}", body)  # Panel 1 geklemmt (F2/a1)

    def test_b42__gallery_couples_to_linked_panels(self):
        self.client.get("/")
        self.client.post("/panel/new")
        _configure(self.client, 1)
        _configure(self.client, 2)
        self.client.post("/panels/link-all")
        self.client.post("/panel/1/open/0")     # beide → Einzelbild (gekoppelt)
        body = self.client.post("/panel/2/gallery").get_data(as_text=True)  # single click to go back
        data = _page_data(body)
        self.assertEqual(data["single_panel_ids"], [])             # both back in gallery
        self.assertEqual(body.count("panel-gallery"), 2)

    def test_b43__link_all_toggles_all_panels(self):
        self.client.get("/")
        self.client.post("/panel/new")
        self.client.post("/panel/new")          # 3 panels, none linked
        _configure(self.client, 1)
        _configure(self.client, 2)
        _configure(self.client, 3)
        body = self.client.post("/panels/link-all").get_data(as_text=True)
        self.assertEqual(body.count('data-linked="1"'), 3)         # alle gekoppelt
        self.assertIn("Alle entkoppeln", body)                     # Button spiegelt Zustand
        body = self.client.post("/panels/link-all").get_data(as_text=True)
        self.assertEqual(body.count('data-linked="0"'), 3)         # alle entkoppelt
        self.assertIn("Alle koppeln", body)

    def test_b44__panel_follows_anothers_image_set(self):
        """Follower panel adopts the base image set of the source (model-dependently smaller than total)
        but displays it with its own configuration → both panels have the same image count."""
        model = METADATA["models"][0]
        preds = METADATA["predictions"][model]["low"]
        true = {i["id"]: i["true_class"] for i in METADATA["images"]}
        n_incorrect = sum(1 for iid, p in preds.items() if p["predicted_class"] != true[iid])
        self.assertGreater(n_incorrect, 0)
        self.assertLess(n_incorrect, len(true))  # echte Teilmenge → unterscheidbar von „alle"

        self.client.get("/")
        self.client.post("/panel/new")  # Panel 2
        self.client.post("/panel/1/config", data={
            "model": model, "level": "low", "xai_method": "original",
            "klassifikation": "inkorrekt",
        })
        # Panel 2 adopts panel 1 as image source, with its own (different) level + empty filter
        self.client.post("/panel/2/config", data={
            "model": model, "level": "high", "xai_method": "original",
            "image_source": "panel:1",
        })
        body = self.client.get("/").get_data(as_text=True)
        expected = f"{n_incorrect} " + ("Bild" if n_incorrect == 1 else "Bilder")
        self.assertEqual(body.count(expected), 2)  # source + follower same count
        self.assertIn("&larr; P1", body)           # subtle follower badge

    def test_b45__source_panel_cannot_become_follower(self):
        """No chaining (Q5a): a panel with followers must not adopt another panel as its source."""
        model = METADATA["models"][0]
        self.client.get("/")
        self.client.post("/panel/new")  # P2
        self.client.post("/panel/new")  # P3
        self.client.post("/panel/2/config", data={
            "model": model, "level": "high", "xai_method": "original",
            "image_source": "panel:1",  # P2 follows P1 → P1 has followers
        })
        self.client.post("/panel/1/config", data={
            "model": model, "level": "high", "xai_method": "original",
            "image_source": "panel:3",  # attempt: P1 tries to follow P3 → must be rejected
        })
        with self.client.session_transaction() as sess:
            p1 = next(p for p in sess["panels"] if p["id"] == 1)
            self.assertEqual(p1["image_source"]["type"], "filter")

    def test_b46__follower_falls_back_when_source_deleted(self):
        """Source deleted → follower falls back gracefully to its own (empty) filter."""
        model = METADATA["models"][0]
        self.client.get("/")
        self.client.post("/panel/new")  # P2
        self.client.post("/panel/1/config", data={
            "model": model, "level": "low", "xai_method": "original",
            "klassifikation": "inkorrekt",
        })
        self.client.post("/panel/2/config", data={
            "model": model, "level": "high", "xai_method": "original",
            "image_source": "panel:1",
        })
        self.client.post("/panel/1/delete")  # Quelle weg
        body = self.client.get("/").get_data(as_text=True)
        total = len(METADATA["images"])
        self.assertIn(f"{total} Bilder", body)  # P2 zeigt nun alle (eigener leerer Filter)
        self.assertNotIn("&larr; P", body)      # keine Folger-Kennzeichnung mehr

    def _panels_by_id(self):
        with self.client.session_transaction() as sess:
            return {p["id"]: p for p in sess["panels"]}

    def test_b162__adopt_base_copies_model_and_follows_leftmost(self):
        """Targets take model/level of the leftmost panel as if set manually and follow its images;
        own filters are kept, an XAI method the new model lacks falls back to original."""
        self.client.get("/")
        self.client.post("/panel/new")  # P2
        self.client.post("/panel/new")  # P3
        _configure(self.client, 1, model="ResNet50", level="low", xai_method="original")
        _configure(self.client, 2, model="VGG16", level="high", xai_method="CRP",
                   filter_umgebung=_FILTER_UMGEBUNG)
        body = self.client.post("/panels/adopt-base").get_data(as_text=True)
        self.assertIn("Basisbilder übernehmen", body)
        panels = self._panels_by_id()
        self.assertEqual(panels[1]["image_source"], {"type": "filter"})
        for pid in (2, 3):
            self.assertEqual((panels[pid]["model"], panels[pid]["level"]), ("ResNet50", "low"))
            self.assertEqual(panels[pid]["image_source"], {"type": "panel", "id": 1})
            self.assertTrue(panels[pid]["configured"])                 # P3 was never confirmed
        self.assertEqual(panels[2]["xai_method"], "original")          # CRP not offered by ResNet50
        self.assertTrue(any(panels[2]["filters"].values()))            # own filter kept

    def test_b163__adopt_base_reference_is_leftmost_not_lowest_id(self):
        self.client.get("/")
        self.client.post("/panel/new")          # P2
        _configure(self.client, 1, model="VGG16")
        self.client.post("/panel/1/duplicate")  # P3, inserted right of P1 → order [1, 3, 2]
        self.client.post("/panel/1/delete")     # order [3, 2]
        self.client.post("/panels/adopt-base")
        panels = self._panels_by_id()
        self.assertEqual(panels[2]["image_source"], {"type": "panel", "id": 3})
        self.assertEqual(panels[3]["image_source"], {"type": "filter"})

    def test_b164__adopt_base_skips_hidden_and_modelcard_but_redirects_their_chains(self):
        self.client.get("/")
        for _ in range(3):
            self.client.post("/panel/new")      # P2..P4
        _configure(self.client, 1, model="VGG16")
        _configure(self.client, 2, model="ResNet50")
        _configure(self.client, 3, model="ResNet50", xai_method=MODELCARD_OPTION)
        _configure(self.client, 4, model="ResNet50", image_source="panel:2")
        self.client.post("/panel/4/toggle")     # hide P4 (follower of P2)
        self.client.post("/panels/adopt-base")
        panels = self._panels_by_id()
        self.assertEqual(panels[2]["image_source"], {"type": "panel", "id": 1})
        self.assertEqual(panels[3]["model"], "ResNet50")               # modelcard untouched
        self.assertEqual(panels[4]["model"], "ResNet50")               # hidden: config untouched …
        self.assertEqual(panels[4]["image_source"], {"type": "panel", "id": 1})  # … but no chain

    def test_b165__adopt_base_when_reference_follows_a_target(self):
        """Reference P1 follows P2 → the common source is P2; P2 itself stays filter-based."""
        self.client.get("/")
        self.client.post("/panel/new")          # P2
        self.client.post("/panel/new")          # P3
        _configure(self.client, 1, model="VGG16", image_source="panel:2")
        self.client.post("/panels/adopt-base")
        panels = self._panels_by_id()
        self.assertEqual(panels[1]["image_source"], {"type": "panel", "id": 2})
        self.assertEqual(panels[2]["image_source"], {"type": "filter"})
        self.assertEqual(panels[3]["image_source"], {"type": "panel", "id": 2})
        self.assertEqual(panels[2]["model"], "VGG16")

    def test_b166__adopt_base_disabled_with_fewer_than_two_affected_panels(self):
        self.client.get("/")
        self.client.post("/panel/new")          # P2
        self.client.post("/panel/2/toggle")     # hide → only one affected panel
        body = self.client.get("/").get_data(as_text=True)
        self.assertRegex(body, r'hx-post="/panels/adopt-base"[^>]*\sdisabled')
        before = self._panels_by_id()
        self.client.post("/panels/adopt-base")
        self.assertEqual(self._panels_by_id(), before)

    def test_b30__classification_filter_shows_only_incorrect(self):
        model = METADATA["models"][0]
        level = "low"  # low level → some misclassifications
        preds = METADATA["predictions"][model][level]
        true = {img["id"]: img["true_class"] for img in METADATA["images"]}
        n_incorrect = sum(1 for iid, p in preds.items()
                          if p["predicted_class"] != true[iid])
        self.assertGreater(n_incorrect, 0)        # test data contains error cases
        self.assertLess(n_incorrect, len(true))   # but not all → filter visibly effective

        self.client.get("/")
        resp = self.client.post("/panel/1/config", data={
            "model": model, "level": level, "xai_method": "original",
            "klassifikation": "inkorrekt",
        })
        self.assertEqual(resp.status_code, 200)
        expected = f"{n_incorrect} " + ("Bild" if n_incorrect == 1 else "Bilder")
        self.assertIn(expected, resp.data.decode())

    def test_b31__filter_dialog_shows_option_counts(self):
        self.client.get("/")
        body = self.client.get("/panel/1/config").get_data(as_text=True)
        total = len(METADATA["images"])
        self.assertIn(f'id="cnt-cls-alle">({total})', body)  # Klassifikation „Alle" = gesamt
        self.assertRegex(body, r'id="cnt-umgebung-\w+">\(\d+\)')  # category counters present

    def test_b35__gallery_count_plural_singular_and_filter_icon(self):
        self.client.get("/")
        _configure(self.client, 1)
        total = len(METADATA["images"])
        body = self.client.get("/").get_data(as_text=True)
        self.assertIn(f"{total} Bilder", body)        # echtes Plural
        self.assertNotIn("Bild(er)", body)            # alte unprofessionelle Form weg
        self.assertNotIn("bi-funnel-fill", body)      # ohne Filter kein Filter-Icon

        # mit Filter: Filter-Icon erscheint
        body = self.client.post("/panel/1/config", data={
            "model": "VGG16", "level": "high", "xai_method": "original",
            "filter_umgebung": _FILTER_UMGEBUNG,
        }).get_data(as_text=True)
        self.assertIn("bi-funnel-fill", body)

        # Singular, falls eine Kombination mit genau 1 Bild existiert (Mock ist deterministisch)
        true = {i["id"]: i["true_class"] for i in METADATA["images"]}
        for model in METADATA["models"]:
            for level in METADATA["levels"]:
                preds = METADATA["predictions"][model][level]
                inc = sum(1 for iid, p in preds.items() if p["predicted_class"] != true[iid])
                for klass, n in (("inkorrekt", inc), ("korrekt", len(preds) - inc)):
                    if n == 1:
                        body = self.client.post("/panel/1/config", data={
                            "model": model, "level": level, "xai_method": "original",
                            "klassifikation": klass,
                        }).get_data(as_text=True)
                        self.assertIn("1 Bild", body)
                        self.assertNotIn("1 Bilder", body)
                        return

    def test_b33__single_view_shows_all_class_confidences(self):
        """single-image view shows confidences for ALL classes, not just the prediction."""
        self.client.get("/")
        _configure(self.client, 1)
        body = self.client.post("/panel/1/open/0").get_data(as_text=True)
        self.assertEqual(body.count("progress-bar"), len(METADATA["classes"]))  # one bar per class
        for label in METADATA["class_labels"].values():
            self.assertIn(label, body)

    def test_b32__faceted_counts_reflect_other_facets(self):
        """"Incorrect" selected → environment counters count only incorrect images (sum == #incorrect)."""
        self.client.get("/")
        model = METADATA["models"][0]
        level = "low"
        preds = METADATA["predictions"][model][level]
        true = {img["id"]: img["true_class"] for img in METADATA["images"]}
        n_incorrect = sum(1 for iid, p in preds.items() if p["predicted_class"] != true[iid])

        resp = self.client.post("/panel/1/filter-counts", data={
            "model": model, "level": level, "xai_method": "original",
            "klassifikation": "inkorrekt",
        })
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        # each image has exactly one environment → sum of environment counters == #incorrect
        umgebung = [int(n) for n in re.findall(
            r'id="cnt-umgebung-\w+" hx-swap-oob="true">\((\d+)\)', body)]
        self.assertEqual(sum(umgebung), n_incorrect)
        # OOB format present
        self.assertIn('hx-swap-oob="true"', body)


# ── Filter Logic ──────────────────────────────────────────────────────────────

class TestFilterLogic(unittest.TestCase):

    def test_b10__empty_filter_passes_everything(self):
        cfg = empty_filter_config()
        for img in METADATA["images"]:
            self.assertTrue(image_passes_filter(img["attributes"], cfg))

    def test_b11__category_filter_and_distance_filter(self):
        cfg = empty_filter_config()
        cfg["umgebung"] = [_FILTER_UMGEBUNG]
        matched = [i for i in METADATA["images"] if image_passes_filter(i["attributes"], cfg)]
        self.assertEqual(len(matched), _FILTER_COUNT)
        cfg["dist_lateral_m"] = [0, 0]
        matched = [i for i in METADATA["images"] if image_passes_filter(i["attributes"], cfg)]
        self.assertLessEqual(len(matched), _FILTER_COUNT)

    def test_b179__a_narrowed_distance_drops_images_without_a_value(self):
        """A synthetic image without a person carries no distance (build_metadata: `scene`).

        Asking "which images have a person within 2 m" must not answer with the images that
        have no person at all – before this, the whole negative set rode along in every
        distance filter. The untouched slider stays a pure no-op.
        """
        spec = DISTANCE_FILTERS["dist_lateral_m"]
        unknown = {"umgebung": ["ueberland"], "objekte": [], "tageszeit": ["tag"],
                   "wetter": ["sonnig"], "dist_lateral_m": None, "dist_longitudinal_m": None}
        known = dict(unknown, dist_lateral_m=1.5, dist_longitudinal_m=9.0)

        cfg = empty_filter_config()
        self.assertTrue(image_passes_filter(unknown, cfg))  # full range = no restriction
        cfg["dist_lateral_m"] = [spec["min"], spec["max"]]
        self.assertTrue(image_passes_filter(unknown, cfg))  # explicitly full = still none

        cfg["dist_lateral_m"] = [0, 2]
        self.assertFalse(image_passes_filter(unknown, cfg))
        self.assertTrue(image_passes_filter(known, cfg))
        # The OTHER distance is still at its full range, so its missing value must not drop
        # the image – only a narrowed slider asks a question the image has to answer.
        self.assertTrue(image_passes_filter(dict(unknown, dist_lateral_m=1.5), cfg))


# ── Konfidenz-Filter ──────────────────────────────────────────────────────────

class TestConfidenceFilter(unittest.TestCase):
    """Range filter on the confidence of the *predicted* class."""

    MODEL = METADATA["models"][0]
    LEVEL = "low"

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        _auth_session(self.client)
        self.client.get("/")

    @classmethod
    def _confidences(cls):
        """Confidence in percent per image id, straight from the metadata (independent path)."""
        preds = METADATA["predictions"][cls.MODEL][cls.LEVEL]
        out = {}
        for iid, pred in preds.items():
            predicted = pred.get("predicted_class")
            if predicted is not None:
                out[iid] = pred["confidences"][predicted] * 100
        return out

    def _counts(self, **form):
        """Faceted counts for a dialog state; returns the 'all' counter of the classification block."""
        resp = self.client.post("/panel/1/filter-counts", data={
            "model": self.MODEL, "level": self.LEVEL, "xai_method": "original", **form})
        self.assertEqual(resp.status_code, 200)
        match = re.search(r'id="cnt-cls-alle" hx-swap-oob="true">\((\d+)\)', resp.data.decode())
        self.assertIsNotNone(match, "counter for 'all' not found")
        return int(match.group(1))

    def test_b33a__matcher_is_inactive_by_default_and_inclusive_when_active(self):
        cfg = empty_filter_config()
        self.assertFalse(cfg[CONFIDENCE_FILTER["active_key"]])
        self.assertEqual(cfg[CONFIDENCE_FILTER["key"]], [CONFIDENCE_FILTER["min"],
                                                         CONFIDENCE_FILTER["max"]])
        self.assertTrue(_confidence_matches(None, cfg))          # inactive → no restriction
        cfg[CONFIDENCE_FILTER["active_key"]] = True
        cfg[CONFIDENCE_FILTER["key"]] = [50.0, 65.0]
        self.assertTrue(_confidence_matches(50.0, cfg))          # bounds inclusive
        self.assertTrue(_confidence_matches(65.0, cfg))
        self.assertFalse(_confidence_matches(65.1, cfg))
        self.assertFalse(_confidence_matches(None, cfg))         # no prediction → out (Q3a)

    def test_b33b__predicted_confidence_reads_the_predicted_class(self):
        self.assertIsNone(_predicted_confidence({}))
        self.assertIsNone(_predicted_confidence({"predicted_class": "person"}))  # no confidences
        pred = {"predicted_class": "person", "confidences": {"person": 0.7, "not_person": 0.3}}
        self.assertAlmostEqual(_predicted_confidence(pred), 70.0)

    def test_b33c__gallery_is_restricted_to_the_confidence_range(self):
        values = self._confidences()
        threshold = round(sorted(values.values())[len(values) // 3], 1)
        expected = sum(1 for v in values.values() if v <= threshold)

        body = self.client.post("/panel/1/config", data={
            "model": self.MODEL, "level": self.LEVEL, "xai_method": "original",
            "konfidenz_aktiv": "1", "konfidenz_min": "0", "konfidenz_max": str(threshold),
        }).get_data(as_text=True)
        self.assertIn(f"{expected} Bild", body)
        # The active range is named in the panel header (A2).
        self.assertIn("Konfidenz", body)

    def test_b33d__bounds_without_the_checkbox_do_not_restrict(self):
        """The inputs are disabled while the checkbox is off, but a stale/handcrafted request
        must not restrict anything either."""
        total = len(METADATA["images"])
        self.assertEqual(self._counts(konfidenz_min="99.9", konfidenz_max="100"), total)

    def test_b33e__counts_follow_the_confidence_range(self):
        values = self._confidences()
        threshold = round(sorted(values.values())[len(values) // 3], 1)
        expected = sum(1 for v in values.values() if v <= threshold)
        self.assertEqual(
            self._counts(konfidenz_aktiv="1", konfidenz_min="0", konfidenz_max=str(threshold)),
            expected)

    def test_b33f__impossible_range_yields_an_empty_set(self):
        self.assertEqual(self._counts(konfidenz_aktiv="1", konfidenz_min="100", konfidenz_max="0"), 0)


# ── modelcard / model overview ───────────────────────────────────────

def _predicted_images(model, level, selection=None):
    """Images of the fixture that have a prediction (and carry `selection`, if given).
    Counted straight from the metadata – independent of the app's own aggregation."""
    preds = METADATA["predictions"][model][level]
    out = []
    for img in METADATA["images"]:
        if preds.get(img["id"], {}).get("predicted_class") is None:
            continue
        if selection:
            key, value = selection
            hit = value in attribute_tokens(img["attributes"], key)
            if not hit:
                continue
        out.append(img)
    return out


class TestModelcard(unittest.TestCase):

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        # Redirect the module-level store to a temp file (tests must not touch notes.json).
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = NOTES_STORE.path
        NOTES_STORE.path = Path(self._tmp.name) / "notes.json"
        _auth_session(self.client)
        self.model = METADATA["models"][0]
        self.level = METADATA["levels"][0]

    def tearDown(self):
        NOTES_STORE.path = self._orig_path
        self._tmp.cleanup()

    def test_b70__stats_matrix_totals_match_the_fixture(self):
        stats = _modelcard_stats(METADATA, self.model, self.level, None)
        expected = _predicted_images(self.model, self.level)
        cells = sum(v for row in stats["matrix"].values() for v in row.values())
        self.assertEqual(cells, len(expected))
        self.assertEqual(stats["total"], len(expected))
        # Diagonal = correct predictions; accuracy is derived from exactly those.
        diagonal = sum(stats["matrix"][c][c] for c in stats["classes"])
        self.assertEqual(diagonal, stats["correct"])
        self.assertAlmostEqual(stats["accuracy"], diagonal / len(expected))

    def test_b70a__row_and_column_sums_partition_the_total(self):
        stats = _modelcard_stats(METADATA, self.model, self.level, None)
        self.assertEqual(sum(stats["row_totals"].values()), stats["total"])
        self.assertEqual(sum(stats["col_totals"].values()), stats["total"])
        for tc in stats["classes"]:
            self.assertEqual(stats["row_totals"][tc], sum(stats["matrix"][tc].values()))
            self.assertEqual(stats["col_totals"][tc],
                             sum(stats["matrix"][r][tc] for r in stats["classes"]))

    def test_b71__selection_restricts_matrix_but_not_option_accuracy(self):
        selection = ("umgebung", _FILTER_UMGEBUNG)
        full = _modelcard_stats(METADATA, self.model, self.level, None)
        scoped = _modelcard_stats(METADATA, self.model, self.level, selection)
        self.assertEqual(scoped["total"], len(_predicted_images(self.model, self.level, selection)))
        self.assertLessEqual(scoped["total"], full["total"])
        # Option accuracies are selection-independent (they must not move when clicking around).
        self.assertEqual(scoped["option_accuracy"], full["option_accuracy"])

    def test_b72a__metadata_block_falls_back_to_na_when_absent(self):
        """A dataset without model_meta → every field reads "N/A" (graceful degradation)."""
        meta = {**METADATA, "model_meta": {}}
        with flask_app.test_request_context():
            fields = _model_metadata(meta, self.model, self.level)
        self.assertEqual(len(fields), len(MODELCARD_META_FIELDS))
        self.assertTrue(all(f["value"] == MODELCARD_META_NA for f in fields))
        # Labels are translated, not raw keys.
        self.assertTrue(all(not f["label"].startswith("modelcard.meta.") for f in fields))

    def test_b72b__metadata_block_reads_and_formats_model_meta(self):
        """Raw values from model_meta[model][level] win and are display-formatted (Q1a):
        params → compact millions, nominal_accuracy → percent, plain fields pass through."""
        entry = {"params": 138357544, "epochs": 42, "trained_at": "2026-05-19",
                 "nominal_accuracy": 0.961}
        meta = {**METADATA, "model_meta": {self.model: {self.level: entry}}}
        with flask_app.test_request_context():
            fields = {f["label"]: f["value"]
                      for f in _model_metadata(meta, self.model, self.level)}
            other_level = _model_metadata(meta, self.model, METADATA["levels"][-1])
        values = set(fields.values())
        self.assertIn("138.4 M", values)   # params formatted, not the raw integer
        self.assertIn("96.1 %", values)    # nominal_accuracy as percent
        self.assertIn("42", values)        # epochs pass through as string
        self.assertIn("2026-05-19", values)
        # A different training level does not inherit the value.
        if METADATA["levels"][-1] != self.level:
            self.assertTrue(all(f["value"] == MODELCARD_META_NA for f in other_level))

    def test_b72c__mock_fixture_supplies_model_meta_for_every_model_level(self):
        """The mock dataset (tools/make_mock_dataset.py) now ships model_meta.csv → the block is
        fully populated for every model×level, no field left at "N/A"."""
        for model in METADATA["models"]:
            for level in METADATA["levels"]:
                with flask_app.test_request_context():
                    fields = _model_metadata(METADATA, model, level)
                self.assertTrue(all(f["value"] != MODELCARD_META_NA for f in fields),
                                f"{model}/{level} still has N/A fields")

    def test_b72__accuracy_is_none_without_predictions(self):
        empty = {**METADATA, "predictions": {self.model: {self.level: {}}}}
        stats = _modelcard_stats(empty, self.model, self.level, None)
        self.assertEqual(stats["total"], 0)
        self.assertIsNone(stats["accuracy"])
        self.assertIsNone(stats["option_accuracy"]["all"])

    def test_b73__mode_switch_keeps_xai_method_and_filters(self):
        """Q6a: the modelcard selection lives in its own field – image config survives."""
        self.client.get("/")
        model = next(m for m, meths in XAI_AVAILABILITY.items() if "Grad-CAM" in meths)
        _configure(self.client, 1, model=model, xai_method="Grad-CAM",
                   filter_umgebung=_FILTER_UMGEBUNG)
        _configure(self.client, 1, model=model, xai_method=MODELCARD_OPTION,
                   modelcard_filter=f"umgebung:{_FILTER_UMGEBUNG}")
        with self.client.session_transaction() as sess:
            panel = sess["panels"][0]
            self.assertEqual(panel["mode"], "modelcard")
            self.assertEqual(panel["xai_method"], "Grad-CAM")  # kept for switching back
            self.assertEqual(panel["filters"]["umgebung"], [_FILTER_UMGEBUNG])
            self.assertEqual(panel["modelcard_filter"],
                             {"key": "umgebung", "value": _FILTER_UMGEBUNG})
        # …and switching back restores the image rendering.
        _configure(self.client, 1, model=model, xai_method="Grad-CAM",
                   filter_umgebung=_FILTER_UMGEBUNG)
        with self.client.session_transaction() as sess:
            panel = sess["panels"][0]
            self.assertEqual(panel["mode"], "images")
            self.assertEqual(panel["modelcard_filter"],
                             {"key": "umgebung", "value": _FILTER_UMGEBUNG})

    def test_b74__invalid_modelcard_selection_falls_back_to_all_images(self):
        self.client.get("/")
        _configure(self.client, 1, xai_method=MODELCARD_OPTION, modelcard_filter="bogus:value")
        with self.client.session_transaction() as sess:
            self.assertIsNone(sess["panels"][0]["modelcard_filter"])

    def test_b75__panel_renders_matrix_instead_of_gallery(self):
        self.client.get("/")
        _configure(self.client, 1, model=self.model, level=self.level,
                   xai_method=MODELCARD_OPTION)
        html = self.client.get("/").data.decode()
        self.assertIn("Modellüberblick", html)
        self.assertNotIn('class="panel-gallery"', html)
        stats = _modelcard_stats(METADATA, self.model, self.level, None)
        for cls in stats["classes"]:
            self.assertIn(METADATA["class_labels"][cls], html)

    def test_b75b__dialog_ships_the_matching_section_enabled(self):
        """Seam for the mode island: the server renders the initial visibility itself, so the
        dialog is correct before the JS runs – and the hidden fieldset is disabled (not
        submitted), which is what keeps the stored state of the other mode intact."""
        self.client.get("/")
        _configure(self.client, 1, xai_method="original")
        html = self.client.get("/panel/1/config").data.decode()
        self.assertRegex(html, r'id="images-section" class=""\s*>')
        self.assertRegex(html, r'id="modelcard-section" class="d-none"\s*disabled>')
        _configure(self.client, 1, xai_method=MODELCARD_OPTION)
        html = self.client.get("/panel/1/config").data.decode()
        self.assertRegex(html, r'id="images-section" class="d-none"\s*disabled>')
        self.assertRegex(html, r'id="modelcard-section" class=""\s*>')

    def test_b75c__dialog_script_has_no_global_declarations(self):
        """HTMX re-inserts this <script> on every dialog open; a top-level const/let/function
        would throw "already declared" on the SECOND open and kill the whole block (islands
        stop working). Everything must stay inside IIFEs."""
        self.client.get("/")
        html = self.client.get("/panel/1/config").data.decode()
        body = html.split("<script>")[-1].split("</script>")[0]
        offenders = [line for line in body.splitlines()
                     if re.match(r"\s{0,2}(const|let|var|function|class)\s", line)]
        self.assertEqual(offenders, [], f"global declarations in dialog script: {offenders}")

    def test_b76__modelcard_panel_is_not_offered_as_image_source(self):
        self.client.get("/")
        self.client.post("/panel/new")
        _configure(self.client, 1, xai_method=MODELCARD_OPTION)
        html = self.client.get("/panel/2/config").data.decode()
        self.assertNotIn('value="panel:1"', html)

    def test_b77__follower_of_a_modelcard_panel_falls_back_to_own_filter(self):
        """A panel adopting P1's image set keeps working when P1 becomes a modelcard."""
        self.client.get("/")
        self.client.post("/panel/new")
        _configure(self.client, 1, filter_umgebung=_FILTER_UMGEBUNG)
        _configure(self.client, 2, image_source="panel:1")
        _configure(self.client, 1, xai_method=MODELCARD_OPTION)
        html = self.client.get("/").data.decode()
        # P2 now shows its own (unfiltered) set, not P1's filtered one, and says so.
        total = len(METADATA["images"])
        self.assertIn(f"{total} " + ("Bild" if total == 1 else "Bilder"), html)

    def test_b78__modelcard_panel_is_excluded_from_stepping(self):
        self.client.get("/")
        _configure(self.client, 1, xai_method=MODELCARD_OPTION)
        with self.client.session_transaction() as sess:
            sess["panels"][0]["view"] = "single"
            sess["panels"][0]["linked"] = True
        data = _page_data(self.client.get("/").data.decode())
        self.assertNotIn(1, data["single_panel_ids"])
        self.assertNotIn(1, data["linked_single_panel_ids"])
        self.client.post("/step-all/next")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["panels"][0]["index"], 0)

    def test_b79__workspace_snapshot_round_trips_the_modelcard_mode(self):
        self.client.get("/")
        _configure(self.client, 1, xai_method=MODELCARD_OPTION,
                   modelcard_filter=f"umgebung:{_FILTER_UMGEBUNG}")
        self.client.post("/notes/new", data={"title": "Karte", "ref_workspace": "1"})
        note = NOTES_STORE.list_notes(OWNER)[0]
        snap = next(rp for rp in note["refpoints"] if rp["type"] == "workspace")
        self.assertEqual(snap["panels"][0]["mode"], "modelcard")
        self.assertEqual(snap["panels"][0]["modelcard_filter"],
                         {"key": "umgebung", "value": _FILTER_UMGEBUNG})
        # Load it back into a fresh workspace.
        _configure(self.client, 1, xai_method="original")
        self.client.post("/notes/1/refpoint/0/load-workspace")
        with self.client.session_transaction() as sess:
            panel = sess["panels"][0]
            self.assertEqual(panel["mode"], "modelcard")
            self.assertEqual(panel["modelcard_filter"],
                             {"key": "umgebung", "value": _FILTER_UMGEBUNG})

    def test_b79a__workspace_snapshot_round_trips_the_craft_and_crp_selection(self):
        """A CRAFT concept / CRP rank+view chosen in single view survives a workspace snapshot,
        instead of resetting to the attribution map / rank 1 when a note reloads it."""
        self.client.get("/")
        _configure(self.client, 1, xai_method="original")
        # Record a single-view selection directly in the panel state.
        with self.client.session_transaction() as sess:
            p = sess["panels"][0]
            p["crp_rank"], p["crp_view"], p["craft_concept"] = 3, "grid", 549
            sess.modified = True
        self.client.post("/notes/new", data={"title": "CRAFT", "ref_workspace": "1"})
        snap = next(rp for rp in NOTES_STORE.list_notes(OWNER)[0]["refpoints"]
                    if rp["type"] == "workspace")
        self.assertEqual(snap["panels"][0]["craft_concept"], 549)
        self.assertEqual(snap["panels"][0]["crp_rank"], 3)
        self.assertEqual(snap["panels"][0]["crp_view"], "grid")
        # Reset the live panel to defaults, then load the snapshot back.
        with self.client.session_transaction() as sess:
            p = sess["panels"][0]
            p["crp_rank"], p["crp_view"], p["craft_concept"] = 1, "heatmap", None
            sess.modified = True
        self.client.post("/notes/1/refpoint/0/load-workspace")
        with self.client.session_transaction() as sess:
            panel = sess["panels"][0]
            self.assertEqual(panel["craft_concept"], 549)
            self.assertEqual(panel["crp_rank"], 3)
            self.assertEqual(panel["crp_view"], "grid")

    def test_b79b__editing_can_refresh_a_workspace_refpoint_to_the_current_state(self):
        """A kept workspace refpoint marked ref_refresh_<i> is re-snapshotted from the current
        workspace; without the flag it stays frozen (editing text must not overwrite it)."""
        self.client.get("/")
        self.client.post("/notes/new", data={"title": "WS", "ref_workspace": "1"})
        # Change the live selection after the note was captured.
        with self.client.session_transaction() as sess:
            sess["panels"][0]["craft_concept"] = 549
            sess.modified = True
        # Plain save (keep, no refresh) must leave the old snapshot untouched.
        self.client.post("/notes/1/edit", data={"title": "WS", "ref_keep_0": "1"})
        snap = next(rp for rp in NOTES_STORE.list_notes(OWNER)[0]["refpoints"]
                    if rp["type"] == "workspace")
        self.assertIsNone(snap["panels"][0]["craft_concept"])
        # Save with the refresh box ticked → snapshot picks up the current selection.
        self.client.post("/notes/1/edit",
                         data={"title": "WS", "ref_keep_0": "1", "ref_refresh_0": "1"})
        snap = next(rp for rp in NOTES_STORE.list_notes(OWNER)[0]["refpoints"]
                    if rp["type"] == "workspace")
        self.assertEqual(snap["panels"][0]["craft_concept"], 549)

    def test_b79c__refreshing_reflects_added_and_removed_panels(self):
        """Refreshing a workspace refpoint captures the CURRENT panel set — panels added or
        removed since the note was written show up (or disappear) in the snapshot."""
        self.client.get("/")
        self.client.post("/notes/new", data={"title": "WS", "ref_workspace": "1"})
        snap = next(rp for rp in NOTES_STORE.list_notes(OWNER)[0]["refpoints"]
                    if rp["type"] == "workspace")
        self.assertEqual(len(snap["panels"]), 1)
        # Add a second panel, then refresh: the snapshot must now hold both.
        with self.client.session_transaction() as sess:
            clone = dict(sess["panels"][0]); clone["id"] = 2
            sess["panels"].append(clone)
            sess.modified = True
        self.client.post("/notes/1/edit",
                         data={"title": "WS", "ref_keep_0": "1", "ref_refresh_0": "1"})
        snap = next(rp for rp in NOTES_STORE.list_notes(OWNER)[0]["refpoints"]
                    if rp["type"] == "workspace")
        self.assertEqual([ps["panel_id"] for ps in snap["panels"]], [1, 2])
        # Remove the first panel, refresh again: only the remaining panel stays.
        with self.client.session_transaction() as sess:
            sess["panels"] = [p for p in sess["panels"] if p["id"] != 1]
            sess.modified = True
        self.client.post("/notes/1/edit",
                         data={"title": "WS", "ref_keep_0": "1", "ref_refresh_0": "1"})
        snap = next(rp for rp in NOTES_STORE.list_notes(OWNER)[0]["refpoints"]
                    if rp["type"] == "workspace")
        self.assertEqual([ps["panel_id"] for ps in snap["panels"]], [2])


# ── collections ──────────────────────────────────────────────

class TestCollections(unittest.TestCase):

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        # Redirect BOTH stores to temp files (tests must not touch the real user data).
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_notes = NOTES_STORE.path
        self._orig_cols = COLLECTIONS_STORE.path
        NOTES_STORE.path = Path(self._tmp.name) / "notes.json"
        COLLECTIONS_STORE.path = Path(self._tmp.name) / "collections.json"
        _auth_session(self.client)
        self.model = METADATA["models"][0]
        self.level = METADATA["levels"][0]
        self.classes = list(METADATA["class_labels"])

    def tearDown(self):
        NOTES_STORE.path = self._orig_notes
        COLLECTIONS_STORE.path = self._orig_cols
        self._tmp.cleanup()

    def _make_matrix_panel(self):
        self.client.get("/")
        _configure(self.client, 1, model=self.model, level=self.level,
                   xai_method=MODELCARD_OPTION)

    def _diagonal_cells(self):
        """The two correct-classification cells (true == predicted), as form values."""
        return [f"{c}:{c}" for c in self.classes]

    def test_b80__from_matrix_creates_snapshot_and_opens_new_panel(self):
        self._make_matrix_panel()
        cells = self._diagonal_cells()
        resp = self.client.post("/collection/from-matrix",
                                data={"panel_id": 1, "cell": cells, "open_new_panel": "1"})
        self.assertEqual(resp.status_code, 200)
        cols = COLLECTIONS_STORE.list_collections(OWNER)
        self.assertEqual(len(cols), 1)
        col = cols[0]
        # Snapshot ids = exactly the correctly classified images (matrix diagonal).
        expected = _matrix_cell_images(METADATA, self.model, self.level, None,
                                       {(c, c) for c in self.classes})
        self.assertEqual(col["image_ids"], expected)
        self.assertEqual(col["origin"]["kind"], "confmatrix")
        # open_new_panel defaults to on → a second panel now follows the collection.
        with self.client.session_transaction() as sess:
            self.assertEqual(len(sess["panels"]), 2)
            new = sess["panels"][1]
            self.assertEqual(new["image_source"], {"type": "collection", "id": col["id"]})
            self.assertEqual(new["model"], self.model)
            self.assertEqual(new["xai_method"], "original")

    def test_b81__auto_title_follows_scheme_and_avoids_collisions(self):
        existing = {f"ConfMatrix-{self.model}-{self.level}-2fields-1"}
        title = _auto_collection_title(self.model, self.level, 2, existing)
        self.assertEqual(title, f"ConfMatrix-{self.model}-{self.level}-2fields-2")

    def test_b82__blank_title_is_auto_generated_on_create(self):
        self._make_matrix_panel()
        self.client.post("/collection/from-matrix",
                         data={"panel_id": 1, "cell": self._diagonal_cells()[:1]})
        col = COLLECTIONS_STORE.list_collections(OWNER)[0]
        self.assertEqual(col["title"], f"ConfMatrix-{self.model}-{self.level}-1fields-1")

    def test_b83__custom_title_is_kept(self):
        self._make_matrix_panel()
        self.client.post("/collection/from-matrix",
                         data={"panel_id": 1, "cell": self._diagonal_cells(),
                               "title": "Meine Sammlung", "open_new_panel": ""})
        col = COLLECTIONS_STORE.list_collections(OWNER)[0]
        self.assertEqual(col["title"], "Meine Sammlung")
        # open_new_panel empty → no extra panel.
        with self.client.session_transaction() as sess:
            self.assertEqual(len(sess["panels"]), 1)

    def test_b84__no_cells_is_rejected(self):
        self._make_matrix_panel()
        resp = self.client.post("/collection/from-matrix", data={"panel_id": 1})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(COLLECTIONS_STORE.list_collections(OWNER), [])

    def test_b85__collection_source_resolves_to_snapshot_ignoring_filters(self):
        """A collection-sourced panel shows exactly the stored ids, unaffected by any filter."""
        col = COLLECTIONS_STORE.create(
            title="C", image_ids=[METADATA["images"][0]["id"]],
            owner=OWNER, user="u", project="p", dataset=TEST_DATASET)
        self.client.get("/")
        _configure(self.client, 1, image_source=f"collection:{col['id']}",
                   filter_umgebung=_FILTER_UMGEBUNG)
        with self.client.session_transaction() as sess:
            panel = sess["panels"][0]
            self.assertEqual(panel["image_source"], {"type": "collection", "id": col["id"]})
        html = self.client.get("/").data.decode()
        self.assertIn("1 Bild", html)  # exactly the one snapshot image

    def test_b167__adopt_base_propagates_the_references_collection_source(self):
        col = COLLECTIONS_STORE.create(
            title="C", image_ids=[METADATA["images"][0]["id"]],
            owner=OWNER, user="u", project="p", dataset=TEST_DATASET)
        self.client.get("/")
        self.client.post("/panel/new")          # P2
        _configure(self.client, 1, image_source=f"collection:{col['id']}")
        self.client.post("/panels/adopt-base")
        with self.client.session_transaction() as sess:
            for panel in sess["panels"]:
                self.assertEqual(panel["image_source"], {"type": "collection", "id": col["id"]})

    def test_b86__deleted_collection_source_yields_no_images(self):
        col = COLLECTIONS_STORE.create(
            title="C", image_ids=[METADATA["images"][0]["id"]],
            owner=OWNER, user="u", project="p", dataset=TEST_DATASET)
        self.client.get("/")
        _configure(self.client, 1, image_source=f"collection:{col['id']}")
        COLLECTIONS_STORE.delete(col["id"], OWNER)
        html = self.client.get("/").data.decode()
        self.assertNotIn("Traceback", html)  # robust, no crash

    def test_b87__config_dialog_offers_collections_as_source(self):
        col = COLLECTIONS_STORE.create(
            title="C42", image_ids=[METADATA["images"][0]["id"]],
            owner=OWNER, user="u", project="p", dataset=TEST_DATASET)
        self.client.get("/")
        html = self.client.get("/panel/1/config").data.decode()
        self.assertIn(f'value="collection:{col["id"]}"', html)
        self.assertIn("C42", html)


# ── collection management + collections as note reference points ──────────────

class TestCollectionManagement(unittest.TestCase):
    """Management dialog (rename/clone/merge/remove/delete) and the freeze rule: a collection
    referenced by a note is never edited in place — destructive operations clone it instead,
    deleting it is refused (Entscheidung 2026-07-29)."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_notes = NOTES_STORE.path
        self._orig_cols = COLLECTIONS_STORE.path
        NOTES_STORE.path = Path(self._tmp.name) / "notes.json"
        COLLECTIONS_STORE.path = Path(self._tmp.name) / "collections.json"
        _auth_session(self.client)
        self.ids = [img["id"] for img in METADATA["images"][:3]]

    def tearDown(self):
        NOTES_STORE.path = self._orig_notes
        COLLECTIONS_STORE.path = self._orig_cols
        self._tmp.cleanup()

    def _collection(self, title="C", ids=None):
        return COLLECTIONS_STORE.create(title=title, image_ids=ids if ids is not None else self.ids,
                                        owner=OWNER, user="u", project="p", dataset=TEST_DATASET)

    def _reference(self, cid, title="Notiz zur Sammlung"):
        """A note that references the collection explicitly (this is what freezes it)."""
        return NOTES_STORE.create(title=title, text="", refpoints=[{"type": "collection", "id": cid}],
                                  owner=OWNER, user="u", project="p", dataset=TEST_DATASET)

    # ── store primitives ──────────────────────────────────────────────────────

    def test_b93__store_management_primitives(self):
        col = self._collection(ids=["a", "b"])
        self.assertEqual(COLLECTIONS_STORE.rename(col["id"], OWNER, "  Neu  ")["title"], "Neu")
        self.assertIsNone(COLLECTIONS_STORE.rename(col["id"], OWNER, "   "))  # blank title ignored
        # add_images appends and skips duplicates, preserving order.
        merged = COLLECTIONS_STORE.add_images(col["id"], OWNER, ["b", "c"])
        self.assertEqual(merged["image_ids"], ["a", "b", "c"])
        self.assertEqual(COLLECTIONS_STORE.remove_images(col["id"], OWNER, ["b", "weg"])["image_ids"],
                         ["a", "c"])
        self.assertIsNone(COLLECTIONS_STORE.remove_images(col["id"], OWNER, ["b"]))  # nothing matched
        clone = COLLECTIONS_STORE.clone(col["id"], OWNER, "Kopie")
        self.assertNotEqual(clone["id"], col["id"])
        self.assertEqual(clone["image_ids"], ["a", "c"])
        self.assertEqual(clone["origin"], {"kind": "clone", "source_id": col["id"],
                                           "source_title": "Neu",
                                           "source_origin": {}})

    def test_b94__management_routes_require_project(self):
        client = flask_app.test_client()  # no session
        self.assertEqual(client.get("/collections").status_code, 403)
        self.assertEqual(client.post("/collections/1/delete").status_code, 403)
        self.assertEqual(client.post("/collections/1/rename", data={"title": "x"}).status_code, 403)

    # ── unreferenced collections are edited in place ──────────────────────────

    def test_b95__rename_and_delete_without_references(self):
        col = self._collection(title="Alt")
        self.client.post(f"/collections/{col['id']}/rename", data={"title": "Neu"})
        self.assertEqual(COLLECTIONS_STORE.get(col["id"], OWNER)["title"], "Neu")
        self.client.post(f"/collections/{col['id']}/delete")
        self.assertIsNone(COLLECTIONS_STORE.get(col["id"], OWNER))

    def test_b96__merge_adds_source_images_to_target_in_place(self):
        src = self._collection(title="Quelle", ids=self.ids[:2])
        dst = self._collection(title="Ziel", ids=self.ids[2:])
        self.client.post(f"/collections/{src['id']}/merge-into", data={"target": dst["id"]})
        self.assertEqual(COLLECTIONS_STORE.get(dst["id"], OWNER)["image_ids"],
                         self.ids[2:] + self.ids[:2])
        # The source is never touched by a merge.
        self.assertEqual(COLLECTIONS_STORE.get(src["id"], OWNER)["image_ids"], self.ids[:2])
        self.assertEqual(len(COLLECTIONS_STORE.list_collections(OWNER)), 2)  # no clone involved

    def test_b97__remove_selected_images_in_place(self):
        col = self._collection()
        resp = self.client.post(f"/collections/{col['id']}/remove-images",
                                data={"image_id": [self.ids[1], self.ids[2]]})
        self.assertEqual(COLLECTIONS_STORE.get(col["id"], OWNER)["image_ids"], [self.ids[0]])
        self.assertIn("2 Bilder", resp.data.decode())  # plural notice
        # The edited collection stays unfolded across the swap (open state is DOM-only).
        self.assertIn("<details class=\"mt-2\" open>", resp.data.decode())

    def test_b97b__empty_or_stale_selection_changes_nothing(self):
        col = self._collection()
        self.client.post(f"/collections/{col['id']}/remove-images", data={})
        self.client.post(f"/collections/{col['id']}/remove-images", data={"image_id": "gibtsnicht"})
        self.assertEqual(COLLECTIONS_STORE.get(col["id"], OWNER)["image_ids"], self.ids)

    # ── freeze rule ───────────────────────────────────────────────────────────

    def test_b98__referenced_collection_is_cloned_instead_of_changed(self):
        col = self._collection(title="Referenziert")
        self._reference(col["id"])
        resp = self.client.post(f"/collections/{col['id']}/remove-images",
                                data={"image_id": self.ids[1]})
        self.assertEqual(resp.status_code, 200)
        # Original untouched …
        self.assertEqual(COLLECTIONS_STORE.get(col["id"], OWNER)["image_ids"], self.ids)
        # … the change lives in a new copy, and the dialog says so.
        cols = COLLECTIONS_STORE.list_collections(OWNER)
        self.assertEqual(len(cols), 2)
        clone = next(c for c in cols if c["id"] != col["id"])
        self.assertEqual(clone["image_ids"], [self.ids[0], self.ids[2]])
        self.assertIn("Referenziert (Kopie)", clone["title"])
        self.assertIn("bleibt unverändert", resp.data.decode())

    def test_b98b__batch_removal_on_frozen_collection_makes_exactly_one_copy(self):
        """The whole point of removing as a batch: per-image removal would clone the still-
        referenced original again for every image, leaving copies of which none is the result."""
        col = self._collection(title="Referenziert")
        self._reference(col["id"])
        self.client.post(f"/collections/{col['id']}/remove-images",
                         data={"image_id": [self.ids[0], self.ids[1]]})
        cols = COLLECTIONS_STORE.list_collections(OWNER)
        self.assertEqual(len(cols), 2)  # one original + exactly one copy
        clone = next(c for c in cols if c["id"] != col["id"])
        self.assertEqual(clone["image_ids"], [self.ids[2]])  # both removals in that one copy

    def test_b98c__stale_selection_on_frozen_collection_leaves_no_copy(self):
        col = self._collection()
        self._reference(col["id"])
        self.client.post(f"/collections/{col['id']}/remove-images", data={"image_id": "gibtsnicht"})
        self.assertEqual(len(COLLECTIONS_STORE.list_collections(OWNER)), 1)

    def test_b99__merge_into_referenced_target_clones_the_target(self):
        src = self._collection(title="Quelle", ids=["x"])
        dst = self._collection(title="Ziel", ids=["y"])
        self._reference(dst["id"])
        self.client.post(f"/collections/{src['id']}/merge-into", data={"target": dst["id"]})
        self.assertEqual(COLLECTIONS_STORE.get(dst["id"], OWNER)["image_ids"], ["y"])  # frozen
        clone = next(c for c in COLLECTIONS_STORE.list_collections(OWNER)
                     if c["id"] not in (src["id"], dst["id"]))
        self.assertEqual(clone["image_ids"], ["y", "x"])

    def test_b100__delete_is_refused_while_referenced_and_names_the_notes(self):
        col = self._collection(title="Referenziert")
        note = self._reference(col["id"], title="Meine Notiz")
        resp = self.client.post(f"/collections/{col['id']}/delete")
        self.assertIsNotNone(COLLECTIONS_STORE.get(col["id"], OWNER))
        body = resp.data.decode()
        self.assertIn("kann nicht gelöscht werden", body)
        self.assertIn(f"#{note['id']} Meine Notiz", body)

    def test_b101__rename_is_allowed_while_referenced(self):
        """Renaming does not change the image set → exempt from the freeze rule (A2)."""
        col = self._collection(title="Alt")
        self._reference(col["id"])
        self.client.post(f"/collections/{col['id']}/rename", data={"title": "Neu"})
        self.assertEqual(COLLECTIONS_STORE.get(col["id"], OWNER)["title"], "Neu")
        self.assertEqual(len(COLLECTIONS_STORE.list_collections(OWNER)), 1)  # no clone

    def test_b102__workspace_snapshot_mention_does_not_freeze(self):
        """A collection merely appearing in a workspace snapshot stays freely editable (Q5a)."""
        col = self._collection()
        NOTES_STORE.create(
            title="WS", text="", owner=OWNER, user="u", project="p", dataset=TEST_DATASET,
            refpoints=[{"type": "workspace",
                        "panels": [{"panel_id": 1,
                                    "image_source": {"type": "collection", "id": col["id"]}}]}])
        self.client.post(f"/collections/{col['id']}/remove-images", data={"image_id": self.ids[0]})
        self.assertEqual(COLLECTIONS_STORE.get(col["id"], OWNER)["image_ids"], self.ids[1:])
        self.assertEqual(len(COLLECTIONS_STORE.list_collections(OWNER)), 1)  # edited in place
        # …but it is shown in the dialog for transparency.
        self.assertIn("Workspace-Momentaufnahmen",
                      self.client.get("/collections").data.decode())

    def test_b103__dialog_lists_collections_with_lock_and_origin(self):
        col = self._collection(title="Meine Sammlung")
        self._reference(col["id"])
        html = self.client.get("/collections").data.decode()
        self.assertIn("Meine Sammlung", html)
        self.assertIn("bi-lock-fill", html)
        self.assertIn('id="collections-body"', html)

    def test_b104__open_panel_from_dialog_adds_collection_panel(self):
        col = self._collection()
        self.client.get("/")
        self.client.post(f"/collections/{col['id']}/open-panel")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["panels"][-1]["image_source"],
                             {"type": "collection", "id": col["id"]})

    # ── collections as note reference points ──────────────────────────────────

    def test_b105__note_form_offers_collections_and_stores_the_refpoint(self):
        col = self._collection(title="Sammlung A")
        self.client.get("/")
        form = self.client.get("/notes/new").data.decode()
        self.assertIn(f'id="ref-collection-{col["id"]}"', form)
        self.assertIn("Sammlung A", form)
        self.client.post("/notes/new", data={"title": "N", "ref_collection": str(col["id"])})
        note = NOTES_STORE.list_notes(OWNER, dataset=TEST_DATASET)[0]
        self.assertEqual(note["refpoints"], [{"type": "collection", "id": col["id"]}])
        # The badge in the list carries the collection title and is clickable.
        html = self.client.get("/notes").data.decode()
        self.assertIn("Sammlung A", html)
        self.assertIn(f"/notes/{note['id']}/refpoint/0/open-collection", html)

    def test_b106__unknown_collection_refpoint_is_dropped(self):
        self.client.get("/")
        self.client.post("/notes/new", data={"title": "N", "ref_collection": ["999", "keinezahl"]})
        self.assertEqual(NOTES_STORE.list_notes(OWNER, dataset=TEST_DATASET)[0]["refpoints"], [])

    def test_b107__open_collection_refpoint_creates_a_panel(self):
        col = self._collection()
        self.client.get("/")
        note = self._reference(col["id"])
        resp = self.client.post(f"/notes/{note['id']}/refpoint/0/open-collection")
        self.assertEqual(resp.status_code, 200)
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["panels"][-1]["image_source"],
                             {"type": "collection", "id": col["id"]})

    def test_b108__deleted_collection_degrades_to_a_dead_badge(self):
        col = self._collection(title="Weg")
        note = self._reference(col["id"])
        COLLECTIONS_STORE.delete(col["id"], OWNER)  # bypasses the guard, as a hand-edit would
        html = self.client.get("/notes").data.decode()
        self.assertIn("gelöscht", html)
        self.assertNotIn(f"/notes/{note['id']}/refpoint/0/open-collection", html)
        self.assertEqual(
            self.client.post(f"/notes/{note['id']}/refpoint/0/open-collection").status_code, 404)


# ── i18n ──────────────────────────────────────────────────────────────────────

class TestFooter(unittest.TestCase):
    """Footer: version + deployment stamp, and the layout rule that keeps it below the fold."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        self._orig_stamp = app_module.DEPLOYMENT_STAMP_FILE
        self._orig_legal = app_module.LEGAL
        # The shipped template, not whatever legal.toml this checkout carries.
        app_module.LEGAL = legal.load_legal(LEGAL_EXAMPLE)

    def tearDown(self):
        app_module.DEPLOYMENT_STAMP_FILE = self._orig_stamp
        app_module.LEGAL = self._orig_legal

    def test_b120__footer_is_on_the_setup_pages_and_the_workspace(self):
        """Inherited via base.html — the whole entry flow carries it, not just the workspace."""
        self.assertIn(PRODUCT_FOOTER, self.client.get("/login").data.decode())
        with self.client.session_transaction() as sess:
            sess["user"] = "testuser"
        self.assertIn(PRODUCT_FOOTER, self.client.get("/role").data.decode())
        with self.client.session_transaction() as sess:
            sess["role"] = "validierer"
        self.assertIn(PRODUCT_FOOTER, self.client.get("/project").data.decode())
        _auth_session(self.client)
        self.assertIn(PRODUCT_FOOTER, self.client.get("/").data.decode())

    def test_b121__footer_starts_below_the_fold(self):
        """Structural guarantee: everything else sits in a container of >= one viewport height,
        and the footer comes after it. Without that the footer would be visible on short pages."""
        body = self.client.get("/login").data.decode()
        self.assertIn('class="page-fold"', body)
        self.assertLess(body.index("page-fold"), body.index("<footer"))

    def test_b121a__every_page_carries_the_minimum_width_guard(self):
        """The app compares panels side by side and has no mobile layout, so below a minimum width
        it is blocked rather than rendered broken. The guard lives in base.html (so login through
        workspace all have it) and is shown purely by a media query — no JS, no state."""
        for url in ("/login", "/"):
            if url == "/":
                _auth_session(self.client)
            body = self.client.get(url).data.decode()
            self.assertIn("viewport-guard", body)
            self.assertIn('role="alert"', body)
        css = (Path(app_module.__file__).parent / "static" / "css" / "style.css").read_text()
        # hidden by default, revealed only under the breakpoint
        self.assertRegex(css, r"\.viewport-guard\s*\{\s*display:\s*none")
        self.assertRegex(css, r"@media\s*\(max-width:\s*767\.98px\)")

    def test_b122__footer_names_the_version(self):
        body = self.client.get("/login").data.decode()
        self.assertIn(f"v{__version__}", body)
        self.assertRegex(__version__, r"^\d+\.\d+\.\d+$")

    def test_b123__deployment_stamp_falls_back_to_local_dev(self):
        app_module.DEPLOYMENT_STAMP_FILE = Path(tempfile.gettempdir()) / "nope-does-not-exist.txt"
        self.assertIn("local dev system", self.client.get("/login").data.decode())

    def test_b124__deployment_stamp_is_read_from_the_file_the_deploy_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "deployment_date.txt"
            stamp.write_text("2026-08-17 09:12:00\n", encoding="utf-8")
            app_module.DEPLOYMENT_STAMP_FILE = stamp
            body = self.client.get("/login").data.decode()
        self.assertIn("2026-08-17 09:12:00", body)
        self.assertNotIn("local dev system", body)

    def test_b125__audit_report_has_no_footer(self):
        """It is a standalone, print-oriented page — a web footer has no place in the PDF."""
        _auth_session(self.client)
        self.assertNotIn(PRODUCT_FOOTER, self.client.get("/report").data.decode())


    def test_b127__the_footer_links_the_legal_pages(self):
        body = self.client.get("/login").data.decode()
        self.assertIn('href="/imprint"', body)
        self.assertIn('href="/privacy"', body)

    def test_b128__legal_pages_are_reachable_without_a_session(self):
        """An imprint behind a login is worth nothing — and whoever is deciding whether to enter
        is exactly the reader of the privacy policy."""
        for path in ("/imprint", "/privacy"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)

    def test_b129__the_privacy_page_states_the_guest_retention_from_the_code(self):
        """The retention notice must not drift away from GUEST_TTL_HOURS."""
        body = self.client.get("/privacy").data.decode()
        self.assertIn(f"{app_module.GUEST_TTL_HOURS} Stunden", body)

    def test_b203__the_privacy_page_links_the_data_protection_officer(self):
        """The officer must be reachable from the page, in both languages, as a real link."""
        for lang in ("de", "en"):
            client = flask_app.test_client()
            client.get(f"/lang/{lang}")
            body = client.get("/privacy").data.decode()
            section = app_module.LEGAL[lang]["privacy"]
            self.assertIn(section["dpo_label"], body, lang)
            self.assertIn(f'href="{section["dpo_url"]}"', body, lang)
            # A bare URL inside prose would render as text, not as a link — the reason these
            # are separate keys (prose + label + href) rather than one sentence.
            self.assertNotIn("https://", section["controller_text"], lang)

    def test_b204__the_privacy_page_shows_its_draft_notice_exactly_while_something_is_unfilled(self):
        """Same rule as for the imprint: honest in both directions."""
        self._check_draft_notice("/privacy", "privacy")

    def test_b205__the_imprint_shows_its_draft_notice_exactly_while_something_is_unfilled(self):
        """The two must not drift apart in either direction: a notice over a page that is in
        fact complete is noise, and a page still full of placeholders without the notice claims
        to be final."""
        self._check_draft_notice("/imprint", "imprint")
        # ... and a filled-in section drops it.
        filled = {lang: {"imprint": {"lead": "Operator", "blocks": [{"heading": "H", "lines": ["L"]}]}}
                  for lang in ("de", "en")}
        app_module.LEGAL = filled
        self.assertNotIn(CATALOGS["de"]["legal.draft_notice"], self.client.get("/imprint").data.decode())

    def _check_draft_notice(self, path, key):
        for lang in ("de", "en"):
            client = flask_app.test_client()
            client.get(f"/lang/{lang}")
            body = client.get(path).data.decode()
            unfilled = legal.placeholders(app_module.LEGAL[lang][key])
            shown = CATALOGS[lang]["legal.draft_notice"] in body
            self.assertEqual(shown, bool(unfilled),
                             f"[{lang}] {path}: draft notice shown={shown}, unfilled: {unfilled or 'none'}")

    def test_b211__without_a_legal_file_the_pages_say_so_and_the_contact_links_vanish(self):
        """A fresh checkout has no legal.toml. The pages must still answer — and must not pretend
        to be anybody's imprint — and a contact link without a target is left out entirely."""
        app_module.LEGAL = {}
        for path in ("/imprint", "/privacy"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertIn(CATALOGS["de"]["legal.not_configured"], resp.data.decode(), path)
        login = self.client.get("/login").data.decode()
        self.assertNotIn(CATALOGS["de"]["login.request_link"], login)
        self.assertNotIn(f'>{CATALOGS["de"]["footer.contact"]}</a>', login)

    def test_b212__the_contact_links_follow_the_legal_file(self):
        """Footer contact and the login's "request access" point to the operator's page."""
        for lang in ("de", "en"):
            client = flask_app.test_client()
            client.get(f"/lang/{lang}")
            body = client.get("/login").data.decode()
            self.assertIn(f'href="{app_module.LEGAL[lang]["contact_url"]}"', body, lang)
            self.assertIn(CATALOGS[lang]["login.request_link"], body, lang)

    def test_b213__every_legal_file_in_the_tree_is_complete_in_both_languages(self):
        """The template and (where present) the instance's own file: both languages, both pages.
        A missing language would silently fall back to German on the English UI."""
        for path in (LEGAL_EXAMPLE, Path(XAI_DIR) / "legal.toml"):
            if not path.exists():
                continue
            data = legal.load_legal(path)
            for lang in LANGUAGES:
                self.assertIn(lang, data, f"{path.name}: [{lang}]")
                for key in ("imprint", "privacy", "contact_url"):
                    self.assertIn(key, data[lang], f"{path.name}: [{lang}] {key}")

    def test_b214__the_catalogues_carry_no_operator_details(self):
        """Operator texts belong in the legal file: a catalogue value naming an institution would
        end up on every third-party instance."""
        for lang in LANGUAGES:
            for key, value in CATALOGS[lang].items():
                for text in (value if isinstance(value, list) else [value]):
                    if isinstance(text, str):
                        self.assertNotRegex(text, r"(?i)tu[ -]dresden|tu-dresden\.de", f"{lang}: {key}")

    def test_b206__no_displayed_text_calls_the_application_a_prototype(self):
        """Renamed to "Demonstrator" on 2026-09-21 (the footer had said so all along).

        Scoped to the i18n catalogues on purpose: in the CODE, "prototype" is an XAI term —
        a CRAFT/CRP *concept prototype* is the representative image of a learned concept
        (see DATA.md). Those must keep the word, which is why this does not grep the tree.
        """
        for lang in ("de", "en"):
            offenders = [f"{key} = {text!r}"
                         for key, value in CATALOGS[lang].items()
                         for text in (value if isinstance(value, list) else [value])
                         if isinstance(text, str) and re.search(r"(?i)prototyp", text)]
            self.assertEqual(offenders, [], f"[{lang}] say Demonstrator instead")

    def test_b159__the_overview_page_is_reachable_without_a_session(self):
        """Whoever does not yet know what the tool is, is exactly the reader of this page."""
        resp = flask_app.test_client().get("/docs/overview")
        self.assertEqual(resp.status_code, 200)

    def test_b160__the_overview_figure_follows_the_ui_language(self):
        client = flask_app.test_client()
        self.assertIn("/static/docs/overview-de.svg", client.get("/docs/overview").get_data(as_text=True))
        client.get("/lang/en")
        self.assertIn("/static/docs/overview-en.svg", client.get("/docs/overview").get_data(as_text=True))

    def test_b161__every_language_has_its_overview_figure(self):
        """The figure is a checked-in build artefact; a missing file would only show in the
        browser as a broken image."""
        for lang in LANGUAGES:
            rel = f"docs/overview-{lang}.svg"
            self.assertTrue((Path(XAI_DIR) / "static" / rel).is_file(), rel)
            self.assertEqual(200, flask_app.test_client().get(f"/static/{rel}").status_code, rel)

    def _guide(self, page, lang="de"):
        client = flask_app.test_client()
        client.get(f"/lang/{lang}")
        resp = client.get(f"/docs/{page}")
        self.assertEqual(resp.status_code, 200, page)
        return client, resp.get_data(as_text=True)

    def test_b207__the_help_page_is_the_usage_guide_opening_with_the_overview(self):
        _, html = self._guide("overview")
        self.assertIn('id="wozu-xaiminer"', html)
        self.assertLess(html.index('id="wozu-xaiminer"'), html.index("/static/docs/overview-de.svg"))
        self.assertLess(html.index("/static/docs/overview-de.svg"),
                        html.index('id="teil-i--erste-schritte"'))

    def test_b208__every_in_page_link_of_a_guide_hits_a_heading(self):
        """The guides link by GitHub-style anchors; docs_render must generate the same ones,
        otherwise the table of contents is dead in the app while it works on the git host."""
        for page in docs_render.PAGES:
            _, html = self._guide(page)
            ids = set(re.findall(r'id="([^"]+)"', html))
            anchors = set(re.findall(r'href="(?:/docs/' + page + r')?#([^"]+)"', html))
            self.assertTrue(anchors, page)
            self.assertEqual(anchors - ids, set(), page)

    def test_b209__guides_carry_no_repository_links_and_all_images_are_served(self):
        for lang in LANGUAGES:
            for page in docs_render.PAGES:
                client, html = self._guide(page, lang)
                self.assertNotRegex(html, r'href="[^"]*\.md', f"{page}/{lang}")
                for src in re.findall(r'<img[^>]+src="([^"]+)"', html):
                    self.assertEqual(200, client.get(src).status_code, f"{page}/{lang}: {src}")

    def test_b210__links_between_guides_point_to_their_routes_and_unknown_pages_404(self):
        _, html = self._guide("overview")
        self.assertIn('href="/docs/setup#4-datensatz-vorbereiten"', html)
        self.assertEqual(404, flask_app.test_client().get("/docs/nope").status_code)


class TestNoThirdPartyContent(unittest.TestCase):
    """The privacy policy states that every part of the pages comes from this server. That is a
    promise about the templates, so it is checked in the templates."""

    def test_b130__no_page_loads_a_script_or_stylesheet_from_a_remote_host(self):
        remote = re.compile(r"""<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*["'](?:https?:)?//""",
                            re.I | re.S)
        offenders = []
        for path in glob.glob(os.path.join(XAI_DIR, "templates", "**", "*.html"), recursive=True):
            with open(path, encoding="utf-8") as fh:
                if remote.search(fh.read()):
                    offenders.append(os.path.relpath(path, XAI_DIR))
        self.assertEqual(offenders, [], f"remote asset reference in {offenders}")

    def test_b131__the_vendored_libraries_are_present(self):
        """A missing vendor file breaks the whole layout, and only in the browser — the tests
        would otherwise stay green while every page renders unstyled."""
        for rel in ("vendor/bootstrap-5.3.3/bootstrap.min.css",
                    "vendor/bootstrap-5.3.3/bootstrap.bundle.min.js",
                    "vendor/bootstrap-icons-1.11.3/bootstrap-icons.min.css",
                    "vendor/bootstrap-icons-1.11.3/fonts/bootstrap-icons.woff2",
                    "vendor/htmx-2.0.4/htmx.min.js"):
            self.assertTrue((Path(XAI_DIR) / "static" / rel).is_file(), rel)
            self.assertEqual(200, flask_app.test_client().get(f"/static/{rel}").status_code, rel)


class TestI18n(unittest.TestCase):

    def test_b47__catalogs_have_identical_key_sets(self):
        """Mandatory self-test: all languages have the same key set."""
        de = set(CATALOGS["de"])
        for lang, cat in CATALOGS.items():
            ks = set(cat)
            self.assertEqual(de, ks, f"Key-Differenz de↔{lang}: nur de={de - ks}, nur {lang}={ks - de}")

    def test_b48__data_label_keys_exist_for_all_tokens(self):
        """Data-label level: for every filter token a catalog key exists (de + en)."""
        expected = set()
        for key, spec in FILTER_CATEGORIES.items():
            expected.add(f"filter.cat.{key}.label")
            expected.update(f"filter.cat.{key}.opt.{v}" for v in spec["options"])
        expected.add("filter.classification.label")
        expected.update(f"filter.classification.opt.{v or 'alle'}"
                        for v in CLASSIFICATION_FILTER["options"])
        for key in DISTANCE_FILTERS:
            expected.add(f"filter.dist.{key}.label")
            expected.add(f"filter.dist.{key}.unit")
        for lang, cat in CATALOGS.items():
            self.assertFalse(expected - set(cat), f"{lang}: fehlende Daten-Label-Keys "
                             f"{expected - set(cat)}")

    def test_b50__every_role_has_its_label_and_description_in_both_catalogs(self):
        """role_select.html builds its keys dynamically ('role.' ~ key), so b49 cannot see them."""
        expected = set()
        for role in ("gutachter", "validierer", "entwickler"):
            expected.update({f"role.{role}", f"role.{role}_desc"})
        expected.add("role.unavailable")
        for lang, cat in CATALOGS.items():
            self.assertFalse(expected - set(cat), f"{lang}: fehlende Rollen-Keys {expected - set(cat)}")

    def test_b49__literal_t_keys_in_templates_and_app_exist(self):
        """Every literal t()/nt() key used in templates/app.py exists in the catalog
        (dynamic keys are covered by b48 for data labels and b50 for the roles)."""
        files = glob.glob(os.path.join(XAI_DIR, "templates", "**", "*.html"), recursive=True)
        files.append(os.path.join(XAI_DIR, "app.py"))
        t_pat = re.compile(r"(?<![\w.])t\(\s*['\"]([\w.]+)['\"]")
        nt_pat = re.compile(r"(?<![\w.])nt\(\s*[^,\n]+?,\s*['\"]([\w.]+)['\"]")
        de = CATALOGS["de"]
        problems = []
        for path in files:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            name = os.path.basename(path)
            for key in t_pat.findall(src):
                # A key ending in a dot is the literal half of a composed key
                # (t('role.' ~ key)); those are covered by b50, not resolvable here.
                if key.endswith("."):
                    continue
                if key not in de:
                    problems.append(f"{name}: t('{key}') fehlt im Katalog")
            for key in nt_pat.findall(src):
                for suf in ("one", "other"):
                    if f"{key}.{suf}" not in de:
                        problems.append(f"{name}: nt-Key '{key}.{suf}' fehlt")
        self.assertFalse(problems, "\n".join(problems))

    def test_b50a__lang_switch_persists_and_ignores_unknown(self):
        flask_app.config["TESTING"] = True
        client = flask_app.test_client()
        resp = client.get("/lang/en")
        self.assertEqual(resp.status_code, 302)
        with client.session_transaction() as sess:
            self.assertEqual(sess["lang"], "en")
        client.get("/lang/xx")  # unbekannt → ignoriert, bleibt en
        with client.session_transaction() as sess:
            self.assertEqual(sess.get("lang"), "en")

    def test_b80__no_untranslated_hardcoded_ui_text(self):
        """Guard against user-visible text that bypasses t()/nt(). Complements b49, which only
        checks the reverse (that used keys exist) and is blind to text that never goes through
        t() at all. Heuristic: strip Jinja, <script>/<style> and HTML tags, then flag residual
        text nodes and literal user-visible attributes with a run of >=2 letters. No allowlist:
        even the product name comes from a Jinja global now (version.__product__)."""
        word = re.compile(r"[A-Za-zÄÖÜäöüß]{2,}")
        attr = re.compile(r'\b(?:title|placeholder|aria-label|alt)\s*=\s*"([^"]*)"')
        skip_files = set()
        allow_phrases = ()

        def blank(m):
            return "\n" * m.group(0).count("\n")  # drop content, keep line numbers

        def strip(text):
            for pat in (r"<script\b.*?</script>", r"<style\b.*?</style>"):
                text = re.sub(pat, blank, text, flags=re.DOTALL | re.IGNORECASE)
            for pat in (r"\{#.*?#\}", r"\{\{.*?\}\}", r"\{%.*?%\}"):
                text = re.sub(pat, blank, text, flags=re.DOTALL)
            return re.sub(r"<[^>]*>", blank, text, flags=re.DOTALL)

        def has_text(s):
            s = re.sub(r"&#?\w+;", " ", s)  # HTML entities (&larr;, &nbsp;, …) are not words
            for phrase in allow_phrases:
                s = s.replace(phrase, " ")
            return bool(word.search(s))

        files = glob.glob(os.path.join(XAI_DIR, "templates", "**", "*.html"), recursive=True)
        problems = []
        for path in files:
            if os.path.basename(path) in skip_files:
                continue
            name = os.path.relpath(path, XAI_DIR)
            with open(path, encoding="utf-8") as fh:
                raw = fh.read()
            for i, line in enumerate(strip(raw).splitlines(), 1):
                if has_text(line):
                    problems.append(f"{name}:{i}: hardcoded text {line.strip()!r}")
            for i, line in enumerate(raw.splitlines(), 1):
                for val in attr.findall(line):
                    if "{{" not in val and "{%" not in val and has_text(val):
                        problems.append(f"{name}:{i}: hardcoded attr {val.strip()!r}")
        self.assertFalse(problems, "Untranslated hardcoded UI text (wrap in t()/nt() or "
                         "allowlist):\n" + "\n".join(problems))


# ── notes ──────────────────────────────────────────────────────────────

class TestNotes(unittest.TestCase):
    """Notes with n:m reference points: store CRUD + route contracts (list/create/edit/delete)."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        # Redirect the module-level store to a temp file (tests must not touch notes.json).
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = NOTES_STORE.path
        NOTES_STORE.path = Path(self._tmp.name) / "notes.json"
        _auth_session(self.client)

    def tearDown(self):
        NOTES_STORE.path = self._orig_path
        self._tmp.cleanup()

    def _create_note(self, title="Testnotiz", text="", **extra):
        return self.client.post("/notes/new", data={"title": title, "text": text, **extra})


    # ── forgetting the subject of a note (US: "a week later nobody knows what it was about") ──

    def _form_html(self, path="/notes/new"):
        self.client.get("/")  # a panel must exist for the workspace suggestion to show up
        return self.client.get(path).get_data(as_text=True)

    def test_b151__creating_preselects_the_panel_configuration(self):
        html = self._form_html()
        box = re.search(r'<input[^>]*name="ref_workspace"[^>]*>', html, re.DOTALL).group(0)
        self.assertIn("checked", box)

    def test_b152__editing_does_not_silently_add_another_snapshot(self):
        self.client.get("/")
        self._create_note(title="N", ref_workspace="1")
        html = self.client.get("/notes/1/edit").get_data(as_text=True)
        box = re.search(r'<input[^>]*name="ref_workspace"[^>]*>', html, re.DOTALL).group(0)
        self.assertNotIn("checked", box)

    def test_b153__the_form_carries_the_warning_dialog(self):
        """The warning is a modal in the form, revealed by its island — not a server round trip."""
        html = self._form_html()
        self.assertIn('id="note-noref-modal"', html)
        self.assertIn("d-none", html.split('id="note-noref-modal"')[0].rsplit("<div", 1)[-1])
        self.assertIn("data-noref-save", html)
        self.assertIn("data-noref-back", html)
        self.assertIn(CATALOGS["de"]["notes.noref.heading"], html)

    def test_b154__saving_without_a_refpoint_still_works(self):
        """The warning must stay a warning: the server never rejects a note without refpoints."""
        resp = self._create_note(title="Ohne Bezug")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(NOTES_STORE.get(1, OWNER)["refpoints"], [])


    def test_b155__the_form_separates_a_scrolling_body_from_the_action_row(self):
        """Save/Cancel must stay visible while the (long) form scrolls — see style.css."""
        html = self._form_html()
        self.assertIn('class="note-form"', html)
        body_at = html.index('class="note-form-body"')
        actions_at = html.index('class="note-form-actions')
        self.assertLess(body_at, actions_at)  # the row comes after the scrolling part
        # Both buttons belong to the pinned row, not to the scrolling body.
        row = html[actions_at:html.index("</form>", actions_at)]
        self.assertIn('type="submit"', row)
        self.assertIn("xaiCancelNote()", row)

    def test_b156__the_dock_height_variable_is_set_where_it_is_used(self):
        """base.html publishes --notes-dock-top, style.css consumes it. Renaming one silently
        breaks the other: the action row would slip below the viewport edge again."""
        base = (Path(app_module.__file__).parent / "templates" / "base.html").read_text(encoding="utf-8")
        css = (Path(app_module.__file__).parent / "static" / "css" / "style.css").read_text(encoding="utf-8")
        self.assertIn('setProperty("--notes-dock-top"', base)
        self.assertIn("var(--notes-dock-top", css)

    def test_b157__the_help_button_is_part_of_the_core_application(self):
        """The button belongs to the application, not to a side tool: it must still be there
        after a side tool is removed. Hence the check on the core workspace. Its help
        is the overview page, opened in a new tab so the workspace stays — no dialog."""
        body = self.client.get("/").get_data(as_text=True)
        self.assertRegex(body, r'<a [^>]*id="app-help-btn"[^>]*href="/docs/overview"[^>]*'
                               r'target="_blank"')
        self.assertNotIn('id="app-help"', body)
        self.assertNotIn("Hilfesystem ist noch nicht umgesetzt", body)

    def test_b51__store_crud_roundtrip_and_persistence(self):
        store = NotesStore(Path(self._tmp.name) / "store.json")
        note = store.create(title="Titel", text="abc",
                            refpoints=[{"type": "model", "model": "VGG16"}],
                            owner=OWNER, user="u", project="p", dataset="mock")
        self.assertEqual(note["id"], 1)
        self.assertFalse(note["include_in_report"])  # groundwork for selecting report entries
        store.update(note["id"], OWNER, title="T2", text="xyz")
        # A fresh instance reads the same file → persistence beyond the object lifetime.
        reread = NotesStore(store.path).get(note["id"], OWNER)
        self.assertEqual(reread["title"], "T2")
        self.assertEqual(reread["text"], "xyz")
        self.assertEqual(reread["refpoints"][0]["model"], "VGG16")
        self.assertTrue(store.delete(note["id"], OWNER))
        self.assertIsNone(store.get(note["id"], OWNER))
        self.assertFalse(store.delete(note["id"], OWNER))

    def test_b52__notes_routes_require_project(self):
        client = flask_app.test_client()  # no session
        self.assertEqual(client.get("/notes").status_code, 403)
        self.assertEqual(client.get("/notes/new").status_code, 403)
        self.assertEqual(client.post("/notes/new", data={"title": "x"}).status_code, 403)

    def test_b53__create_note_with_free_image_ids(self):
        valid_id = METADATA["images"][0]["id"]
        resp = self._create_note(title="Bildnotiz", image_ids=f"{valid_id}, gibtsnicht_999")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn("Bildnotiz", html)
        # Response is the dock panel (list) that replaces the inline form.
        self.assertIn('id="notes-list"', html)
        notes = NOTES_STORE.list_notes(OWNER, dataset=TEST_DATASET)
        self.assertEqual(len(notes), 1)
        refpoints = notes[0]["refpoints"]
        # Free-text ids have no display context → stored as original variant.
        self.assertEqual(refpoints,
                         [{"type": "image", "image_id": valid_id, "xai_method": "original"}])

    def test_b54__title_mandatory_text_optional(self):
        # No title (whitespace only) → 400, even with description text present.
        resp = self._create_note(title="   ", text="Beschreibung")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(NOTES_STORE.list_notes(OWNER), [])
        # Title without description text is fine.
        resp = self._create_note(title="Nur Titel")
        self.assertEqual(resp.status_code, 200)
        note = NOTES_STORE.list_notes(OWNER)[0]
        self.assertEqual(note["title"], "Nur Titel")
        self.assertEqual(note["text"], "")

    def test_b55__workspace_refpoint_snapshots_all_panels_and_current_image(self):
        self.client.get("/")  # creates panel 1
        _configure(self.client, 1, filter_umgebung=_FILTER_UMGEBUNG)
        self.client.post("/panel/1/duplicate")  # second panel (stays in gallery view)
        self.client.post("/panel/1/open/0")  # panel 1 → single view
        self._create_note(title="Panelnotiz", ref_workspace="1")
        rp = NOTES_STORE.list_notes(OWNER)[0]["refpoints"][0]
        self.assertEqual(rp["type"], "workspace")
        self.assertEqual(len(rp["panels"]), 2)  # the ENTIRE panel configuration
        by_id = {ps["panel_id"]: ps for ps in rp["panels"]}
        p1 = by_id[1]
        self.assertIn(p1["model"], METADATA["models"])
        self.assertEqual(p1["filters"]["umgebung"], [_FILTER_UMGEBUNG])
        # Concrete displayed image (id + displayed variant path), not just the base name.
        self.assertIsNotNone(p1["current_image"])
        self.assertIn("image_id", p1["current_image"])
        self.assertTrue(p1["current_image"]["rel"])
        # Gallery-view panel: no single displayed image.
        self.assertIsNone(next(ps for pid, ps in by_id.items() if pid != 1)["current_image"])
        # Reconfiguring a panel afterwards must not change the stored snapshot.
        _configure(self.client, 1)  # clears the panel's filters
        rp_after = NOTES_STORE.list_notes(OWNER)[0]["refpoints"][0]
        by_id_after = {ps["panel_id"]: ps for ps in rp_after["panels"]}
        self.assertEqual(by_id_after[1]["filters"]["umgebung"], [_FILTER_UMGEBUNG])

    def test_b56__edit_updates_text_and_removes_unkept_refpoints(self):
        valid_id = METADATA["images"][0]["id"]
        self._create_note(title="alt", image_ids=valid_id, ref_model="VGG16")
        nid = NOTES_STORE.list_notes(OWNER)[0]["id"]
        self.assertEqual(len(NOTES_STORE.get(nid, OWNER)["refpoints"]), 2)
        # Keep only refpoint 0 (image); ref_keep_1 missing → model refpoint is removed.
        resp = self.client.post(f"/notes/{nid}/edit",
                                data={"title": "neu", "text": "Text neu", "ref_keep_0": "1"})
        self.assertEqual(resp.status_code, 200)
        note = NOTES_STORE.get(nid, OWNER)
        self.assertEqual(note["title"], "neu")
        self.assertEqual(note["text"], "Text neu")
        self.assertEqual(note["refpoints"],
                         [{"type": "image", "image_id": valid_id, "xai_method": "original"}])

    def test_b57__delete_note_returns_refreshed_list(self):
        self._create_note(title="wegdamit")
        nid = NOTES_STORE.list_notes(OWNER)[0]["id"]
        resp = self.client.post(f"/notes/{nid}/delete")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(NOTES_STORE.list_notes(OWNER), [])
        self.assertNotIn(b"wegdamit", resp.data)

    def test_b58__list_is_scoped_to_active_dataset(self):
        NOTES_STORE.create(title="fremd", text="", refpoints=[],
                           owner=OWNER, user="u", project="p", dataset="other")
        self._create_note(title="eigen")
        resp = self.client.get("/notes")
        self.assertIn(b"eigen", resp.data)
        self.assertNotIn(b"fremd", resp.data)

    def test_b59__note_dialog_suggests_workspace_and_current_image(self):
        self.client.get("/")
        _configure(self.client, 1)
        self.client.post("/panel/1/open/0")  # single view → current image becomes a suggestion
        resp = self.client.get("/notes/new")
        html = resp.data.decode()
        self.assertIn('name="ref_workspace"', html)
        self.assertIn('name="ref_image_1"', html)
        first_image_id = METADATA["images"][0]["id"]
        self.assertIn(first_image_id, html)

    def test_b60__workspace_page_contains_notes_dock(self):
        resp = self.client.get("/")
        html = resp.data.decode()
        self.assertIn('id="notes-dock"', html)       # docking column
        self.assertIn('id="notes-content"', html)    # where list/form load
        self.assertIn("xaiToggleNotes()", html)      # nav toggle

    def test_b62__load_workspace_refpoint_replaces_panels(self):
        self.client.get("/")  # panel 1
        _configure(self.client, 1, filter_umgebung=_FILTER_UMGEBUNG)
        self.client.post("/panel/1/duplicate")  # panel 2
        self.client.post("/panel/1/open/0")  # panel 1 → single view
        self._create_note(title="Snap", ref_workspace="1")
        snap_image = NOTES_STORE.get(1, OWNER)["refpoints"][0]["panels"][0]["current_image"]["image_id"]
        # Mutate the workspace: drop everything, start over with a fresh default panel.
        self.client.post("/panel/1/delete")
        self.client.post("/panel/2/delete")
        self.client.get("/")  # creates panel 3
        resp = self.client.post("/notes/1/refpoint/0/load-workspace")
        self.assertEqual(resp.status_code, 200)
        with self.client.session_transaction() as sess:
            panels = sess["panels"]
            self.assertEqual([p["id"] for p in panels], [1, 2])  # stored ids kept
            self.assertEqual(panels[0]["filters"]["umgebung"], [_FILTER_UMGEBUNG])
            self.assertEqual(panels[0]["view"], "single")
            self.assertTrue(all(p["configured"] for p in panels))
            self.assertEqual(sess["next_panel_id"], 3)
        # The restored index points at the stored current image.
        base_ids = [i["id"] for i in METADATA["images"]
                    if _FILTER_UMGEBUNG in attribute_tokens(i["attributes"], "umgebung")]
        with self.client.session_transaction() as sess:
            self.assertEqual(base_ids[sess["panels"][0]["index"]], snap_image)

    def test_b63__multiple_workspace_refpoints_are_addressed_by_index(self):
        self.client.get("/")
        _configure(self.client, 1)  # no filters
        self._create_note(title="Zwei", ref_workspace="1")
        _configure(self.client, 1, filter_umgebung=_FILTER_UMGEBUNG)
        # Edit appends a second (different) workspace snapshot.
        self.client.post("/notes/1/edit",
                         data={"title": "Zwei", "ref_keep_0": "1", "ref_workspace": "1"})
        self.assertEqual([rp["type"] for rp in NOTES_STORE.get(1, OWNER)["refpoints"]],
                         ["workspace", "workspace"])
        self.client.post("/notes/1/refpoint/1/load-workspace")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["panels"][0]["filters"]["umgebung"], [_FILTER_UMGEBUNG])
        self.client.post("/notes/1/refpoint/0/load-workspace")
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["panels"][0]["filters"]["umgebung"], [])
        # Out-of-range index and wrong refpoint type → 404.
        self.assertEqual(self.client.post("/notes/1/refpoint/5/load-workspace").status_code, 404)
        self.assertEqual(
            self.client.post("/notes/1/refpoint/0/open-image", data={"target": "new"}).status_code,
            404)

    def test_b64__image_refpoint_target_chooser_lists_panels_and_new(self):
        self.client.get("/")
        _configure(self.client, 1)
        valid_id = METADATA["images"][2]["id"]
        self._create_note(title="Bild", image_ids=valid_id)
        resp = self.client.get("/notes/1/refpoint/0/open-image")
        html = resp.data.decode()
        self.assertIn('value="panel:1"', html)
        self.assertIn('value="new"', html)
        self.assertIn(valid_id, html)

    def test_b65__open_image_in_new_panel_single_view_unfiltered(self):
        self.client.get("/")
        _configure(self.client, 1)
        valid_id = METADATA["images"][2]["id"]
        self._create_note(title="Bild", image_ids=valid_id)
        resp = self.client.post("/notes/1/refpoint/0/open-image", data={"target": "new"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn('id="modal-container" hx-swap-oob="true"', resp.data.decode())
        with self.client.session_transaction() as sess:
            panels = sess["panels"]
            self.assertEqual(len(panels), 2)
            new = panels[-1]
            self.assertEqual(new["view"], "single")
            self.assertEqual(new["index"], 2)  # metadata order = unfiltered gallery order
            self.assertEqual(new["filters"], empty_filter_config())
            self.assertTrue(new["configured"])

    def test_b66__open_image_in_existing_panel_clears_filters(self):
        self.client.get("/")
        _configure(self.client, 1, filter_umgebung=_FILTER_UMGEBUNG)
        valid_id = METADATA["images"][2]["id"]
        self._create_note(title="Bild", image_ids=valid_id)
        resp = self.client.post("/notes/1/refpoint/0/open-image", data={"target": "panel:1"})
        self.assertEqual(resp.status_code, 200)
        with self.client.session_transaction() as sess:
            panels = sess["panels"]
            self.assertEqual(len(panels), 1)  # no new panel
            self.assertEqual(panels[0]["view"], "single")
            self.assertEqual(panels[0]["index"], 2)
            self.assertEqual(panels[0]["filters"], empty_filter_config())  # gallery = unfiltered
            self.assertEqual(panels[0]["image_source"], {"type": "filter"})

    def test_b67__open_image_with_stale_or_invalid_target_fails_cleanly(self):
        self.client.get("/")
        _configure(self.client, 1)
        # Image id that does not exist in the dataset (bypasses form validation via the store).
        NOTES_STORE.create(title="stale", text="",
                           refpoints=[{"type": "image", "image_id": "gibtsnicht_999"}],
                           owner=OWNER, user="u", project="p", dataset=TEST_DATASET)
        self.assertEqual(
            self.client.post("/notes/1/refpoint/0/open-image", data={"target": "new"}).status_code,
            404)
        valid_id = METADATA["images"][0]["id"]
        self._create_note(title="ok", image_ids=valid_id)
        # Unknown target panel → 404, nothing changes.
        resp = self.client.post("/notes/2/refpoint/0/open-image", data={"target": "panel:99"})
        self.assertEqual(resp.status_code, 404)
        with self.client.session_transaction() as sess:
            self.assertEqual(len(sess["panels"]), 1)

    def test_b68__image_refpoint_stores_and_restores_concrete_variant(self):
        model = next(m for m, meths in XAI_AVAILABILITY.items() if "Grad-CAM" in meths)
        level = next(iter(XAI_AVAILABILITY[model]["Grad-CAM"]))
        self.client.get("/")
        _configure(self.client, 1, model=model, level=level, xai_method="Grad-CAM")
        self.client.post("/panel/1/open/1")
        img_id = METADATA["images"][1]["id"]
        # Panel suggestion carries the panel's rendering → concrete variant is stored.
        self._create_note(title="Konkret", ref_image_1=img_id)
        rp = NOTES_STORE.get(1, OWNER)["refpoints"][0]
        self.assertEqual(rp, {"type": "image", "image_id": img_id, "xai_method": "Grad-CAM",
                              "model": model, "level": level})
        # Reconfigure the panel away from that rendering …
        _configure(self.client, 1, xai_method="original")
        # … then load the refpoint into it: the stored variant must be restored.
        resp = self.client.post("/notes/1/refpoint/0/open-image", data={"target": "panel:1"})
        self.assertEqual(resp.status_code, 200)
        with self.client.session_transaction() as sess:
            panel = sess["panels"][0]
            self.assertEqual(panel["model"], model)
            self.assertEqual(panel["level"], level)
            self.assertEqual(panel["xai_method"], "Grad-CAM")
            self.assertEqual(panel["view"], "single")
            self.assertEqual(panel["index"], 1)

    def test_b69__free_text_image_refpoint_loads_as_original(self):
        model = next(m for m, meths in XAI_AVAILABILITY.items() if "Grad-CAM" in meths)
        self.client.get("/")
        _configure(self.client, 1, model=model, xai_method="Grad-CAM")
        img_id = METADATA["images"][0]["id"]
        self._create_note(title="Basis", image_ids=img_id)
        self.client.post("/notes/1/refpoint/0/open-image", data={"target": "panel:1"})
        with self.client.session_transaction() as sess:
            panel = sess["panels"][0]
            # No stored model/level → panel keeps its own; variant "original" is applied.
            self.assertEqual(panel["model"], model)
            self.assertEqual(panel["xai_method"], "original")

    def test_b61__title_truncated_and_overview_shows_title_only(self):
        self._create_note(title="x" * 150, text="Langtext, der NICHT in der Liste stehen soll")
        note = NOTES_STORE.list_notes(OWNER)[0]
        self.assertEqual(len(note["title"]), 100)
        html = self.client.get("/notes").data.decode()
        self.assertIn("x" * 100, html)
        # Description text appears only as tooltip (title attribute), not as element text.
        self.assertNotIn(">Langtext", html.replace("\n", ""))
        self.assertIn('title="Langtext', html)


# ── requirements catalog ──────────────────────────────────────────────

class TestRequirementsCatalog(unittest.TestCase):
    """Loader for the bilingual audit requirements catalog."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def _write(self, text: str) -> Path:
        path = Path(self._tmp.name) / "req.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_b70__labels_are_index_based(self):
        self.assertEqual([label_for(i) for i in range(3)], ["A1", "A2", "A3"])

    def test_b71__loads_bilingual_entries_in_order(self):
        cat = load_requirements(self._write(
            '[[requirement]]\nde = "eins"\nen = "one"\n'
            '[[requirement]]\nde = "zwei"\nen = "two"\n'))
        self.assertEqual([r["label"] for r in cat], ["A1", "A2"])
        self.assertEqual(cat[0], {"label": "A1", "de": "eins", "en": "one"})
        self.assertEqual(text_for(cat, "de"), {"A1": "eins", "A2": "zwei"})
        self.assertEqual(text_for(cat, "en"), {"A1": "one", "A2": "two"})

    def test_b72__entry_missing_a_language_falls_back_to_the_other(self):
        """Since 2026-08-14 (users edit these files by hand): a missing language is filled from
        the other one and reported as a warning, instead of the entry silently vanishing."""
        catalog, warnings = parse_requirements(
            '[[requirement]]\nde = "eins"\nen = "one"\n'
            '[[requirement]]\nde = "zwei"\n'
            '[[requirement]]\nde = "drei"\nen = "three"\n')
        self.assertEqual([r["label"] for r in catalog], ["A1", "A2", "A3"])
        self.assertEqual(catalog[1]["en"], "zwei")
        self.assertEqual([w["code"] for w in warnings], ["missing_lang"])

    def test_b72c__explicit_labels_identify_a_requirement(self):
        """Labels come from the file, so reordering does not remap verdicts. Duplicates: the
        first entry wins (Q1a), and a warning names the offender."""
        catalog, warnings = parse_requirements(
            '[[requirement]]\nlabel = "A7"\nde = "sieben"\nen = "seven"\n'
            '[[requirement]]\nlabel = "A2"\nde = "zwei"\nen = "two"\n'
            '[[requirement]]\nlabel = "A7"\nde = "doppelt"\nen = "duplicate"\n'
            '[[requirement]]\nde = "ohne Label"\nen = "no label"\n')
        self.assertEqual([r["label"] for r in catalog], ["A7", "A2", "A4"])  # A4 = position 4
        self.assertEqual(catalog[0]["de"], "sieben")                        # first one kept
        self.assertEqual([w["code"] for w in warnings], ["duplicate_label"])

    def test_b72d__entry_without_any_text_is_skipped(self):
        catalog, warnings = parse_requirements('[[requirement]]\nlabel = "A1"\n')
        self.assertEqual(catalog, [])
        self.assertEqual([w["code"] for w in warnings], ["missing_text", "empty"])

    def test_b73__missing_file_yields_empty_catalog(self):
        self.assertEqual(load_requirements(Path(self._tmp.name) / "nope.toml"), [])

    def test_b74__shipped_catalog_is_wellformed(self):
        # The real requirements.toml the app loads at startup must be fully bilingual
        # and carry explicit labels (they identify a requirement, see 2026-08-14).
        self.assertTrue(REQUIREMENTS)
        for r in REQUIREMENTS:
            self.assertTrue(r["de"] and r["en"], r)
        lines = [ln.strip() for ln in REQUIREMENTS_MASTER_PATH.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(lines.count("[[requirement]]"),
                         sum(1 for ln in lines if ln.startswith("label = ")))


# ── per user+dataset requirements catalogs ──────────────────

CATALOG_TWO = ('[[requirement]]\nlabel = "A1"\nde = "eins"\nen = "one"\n'
               '[[requirement]]\nlabel = "A5"\nde = "fünf"\nen = "five"\n')


class TestRequirementsStore(unittest.TestCase):
    """Master as default, private copy per user+dataset."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        master = Path(self._tmp.name) / "master.toml"
        master.write_text('[[requirement]]\nlabel = "A1"\nde = "Master"\nen = "master"\n',
                          encoding="utf-8")
        self.store = RequirementsStore(master, self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_b75__master_applies_until_the_first_edit(self):
        self.assertFalse(self.store.is_custom("kim", "mock"))
        self.assertIn("Master", self.store.source("kim", "mock"))
        self.assertEqual([r["label"] for r in self.store.catalog("kim", "mock")[0]], ["A1"])

    def test_b76__saving_creates_a_private_copy_and_reset_removes_it(self):
        catalog, warnings, error = self.store.save("kim", "mock", CATALOG_TWO)
        self.assertIsNone(error)
        self.assertEqual([r["label"] for r in catalog], ["A1", "A5"])
        self.assertTrue(self.store.is_custom("kim", "mock"))
        self.assertTrue(self.store.reset("kim", "mock"))
        self.assertFalse(self.store.is_custom("kim", "mock"))
        self.assertIn("Master", self.store.source("kim", "mock"))
        self.assertFalse(self.store.reset("kim", "mock"))  # idempotent

    def test_b77__copies_are_separate_per_user_and_per_dataset(self):
        self.store.save("kim", "mock", CATALOG_TWO)
        self.assertTrue(self.store.is_custom("kim", "mock"))
        self.assertFalse(self.store.is_custom("other", "mock"))   # other user → master
        self.assertFalse(self.store.is_custom("kim", "RailPer"))   # other dataset → master
        self.assertIn("Master", self.store.source("other", "mock"))

    def test_b78__invalid_toml_is_rejected_without_writing(self):
        self.store.save("kim", "mock", CATALOG_TWO)
        catalog, warnings, error = self.store.save("kim", "mock", "[[requirement]\nde = ")
        self.assertIsNotNone(error)
        self.assertEqual(catalog, [])
        self.assertIn("A5", self.store.source("kim", "mock"))  # old copy untouched


class TestRequirementsEditing(unittest.TestCase):
    """The editing dialog and the structural cleanup it triggers."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_notes, self._orig_dir = NOTES_STORE.path, REQUIREMENTS_STORE.directory
        NOTES_STORE.path = Path(self._tmp.name) / "notes.json"
        REQUIREMENTS_STORE.directory = Path(self._tmp.name)
        _auth_session(self.client)

    def tearDown(self):
        NOTES_STORE.path = self._orig_notes
        REQUIREMENTS_STORE.directory = self._orig_dir
        self._tmp.cleanup()

    def _entry(self, title, status, owner=OWNER):
        return NOTES_STORE.create(title=title, text="", refpoints=[], owner=owner,
                                  user="testuser", project="p",
                                  dataset=TEST_DATASET, include_in_report=True,
                                  requirement_status=status)

    def _save(self, source, **form):
        return self.client.post("/requirements", data={"source": source, **form})

    def test_b79__dialog_offers_the_master_source_first(self):
        resp = self.client.get("/requirements")
        self.assertEqual(resp.status_code, 200)
        body = resp.data.decode()
        self.assertIn("[[requirement]]", body)
        self.assertIn("Standard", body)          # badge: master in effect
        self.assertNotIn("Eigene Fassung", body)

    def test_b80__requires_a_project(self):
        self.assertEqual(flask_app.test_client().get("/requirements").status_code, 403)

    def test_b81__invalid_toml_is_reported_and_nothing_is_stored(self):
        body = self._save("[[requirement]\nde = ").data.decode()
        self.assertIn("kein gültiges TOML", body)
        self.assertFalse(REQUIREMENTS_STORE.is_custom(OWNER, TEST_DATASET))

    def test_b82__saving_stores_a_copy_and_refreshes_the_form_out_of_band(self):
        body = self._save(CATALOG_TWO).data.decode()
        self.assertTrue(REQUIREMENTS_STORE.is_custom(OWNER, TEST_DATASET))
        self.assertIn("Eigene Fassung", body)                     # badge flipped
        squashed = " ".join(body.split())
        self.assertIn('id="req-section" hx-swap-oob="true"', squashed)
        self.assertIn('id="req-legend-wrap" hx-swap-oob="true"', squashed)
        self.assertIn("fünf", body)                               # new text in the table

    def test_b83__deleting_a_requirement_purges_it_from_own_entries_only(self):
        """Q2a: the verdict must not dangle — but other users have their own catalog, where
        the requirement still exists."""
        mine = self._entry("meins", {"A1": "met", "A5": "unmet"})
        theirs = self._entry("fremd", {"A1": "met", "A5": "unmet"}, owner="user:someone-else")
        self._save(CATALOG_TWO)                                   # A1 + A5 both present
        self.assertEqual(NOTES_STORE.get(mine["id"], OWNER)["requirement_status"],
                         {"A1": "met", "A5": "unmet"})

        body = self._save('[[requirement]]\nlabel = "A1"\nde = "eins"\nen = "one"\n').data.decode()
        self.assertEqual(NOTES_STORE.get(mine["id"], OWNER)["requirement_status"], {"A1": "met"})
        self.assertEqual(NOTES_STORE.get(theirs["id"], "user:someone-else")["requirement_status"],
                         {"A1": "met", "A5": "unmet"})
        self.assertIn("1 Notiz(en)", body)                        # purge is reported

    def test_b84a__verdicts_on_screen_survive_an_edit(self):
        """The dialog posts the open note form along, so ticked boxes are not lost — except for
        requirements that just disappeared."""
        self._save(CATALOG_TWO)
        body = self._save(CATALOG_TWO, req_met_A1="1", req_unmet_A5="1",
                          is_report_entry="1").data.decode()
        self.assertIn('name="req_met_A1" id="req_met_A1" data-req-pair="req_unmet_A1"'
                      ' aria-label="A1 erfüllt" checked', " ".join(body.split()))

    def test_b84b__reset_restores_the_master_and_purges_accordingly(self):
        note = self._entry("meins", {"A5": "met"})
        self._save(CATALOG_TWO)
        self.assertEqual(NOTES_STORE.get(note["id"], OWNER)["requirement_status"], {"A5": "met"})
        resp = self.client.post("/requirements/reset")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(REQUIREMENTS_STORE.is_custom(OWNER, TEST_DATASET))
        # The master has A1..A5 with different texts, but A5 exists → verdict survives.
        self.assertEqual(NOTES_STORE.get(note["id"], OWNER)["requirement_status"], {"A5": "met"})
        self.assertIn("Standard", resp.data.decode())

    def test_b84c__report_shows_only_the_own_entries(self):
        """Q3b: the catalog is per user, so foreign entries would be rendered against a catalog
        that never applied to them."""
        self._entry("meins", {"A1": "met"})
        self._entry("fremd", {"A1": "unmet"}, owner="user:someone-else")
        body = self.client.get("/report").data.decode()
        self.assertIn("meins", body)
        self.assertNotIn("fremd", body)


# ── report entries + auto-title ───────────────────────────────────────

class TestReportEntries(unittest.TestCase):
    """A note flagged as an audit-report entry (checkbox) also records met/unmet verdicts."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = NOTES_STORE.path
        NOTES_STORE.path = Path(self._tmp.name) / "notes.json"
        _auth_session(self.client)  # project type "test" → switch defaults off
        # The app loads a non-empty catalog at startup (see TestRequirementsCatalog).
        self.labels = [r["label"] for r in REQUIREMENTS]

    def tearDown(self):
        NOTES_STORE.path = self._orig_path
        self._tmp.cleanup()

    def _set_mode(self, project_type):
        with self.client.session_transaction() as sess:
            sess["project"] = {**sess["project"], "type": project_type}

    def test_b75__dialog_always_carries_requirements_and_switch(self):
        html = self.client.get("/notes/new").data.decode()
        self.assertIn('name="is_report_entry"', html)   # the report-entry switch
        self.assertIn('id="req-section"', html)
        self.assertIn('id="req-table"', html)            # requirements always in the DOM
        for label in self.labels:
            self.assertIn(f'name="req_met_{label}"', html)
            self.assertIn(f'name="req_unmet_{label}"', html)
        self.assertIn('id="req-info-modal"', html)       # (i) legend

    def test_b76__switch_default_follows_project_mode(self):
        # Test mode: switch off, requirements section hidden (d-none on the #req-section div).
        html = self.client.get("/notes/new").data.decode()
        self.assertNotRegex(html, r'id="is-report-entry"[^>]*\bchecked\b')
        self.assertIn('d-none" id="req-section"', html)
        # Exam mode: switch on, requirements shown.
        self._set_mode("pruefung")
        html = self.client.get("/notes/new").data.decode()
        self.assertRegex(html, r'id="is-report-entry"[^>]*\bchecked\b')
        self.assertNotIn('d-none" id="req-section"', html)

    def test_b77__create_report_entry_stores_flag_and_verdicts(self):
        a, b = self.labels[0], self.labels[1]
        resp = self.client.post("/notes/new", data={
            "title": "Prüfung 1", "text": "Befund", "is_report_entry": "1",
            f"req_met_{a}": "on", f"req_unmet_{b}": "on"})
        self.assertEqual(resp.status_code, 200)
        note = NOTES_STORE.list_notes(OWNER, dataset=TEST_DATASET)[0]
        self.assertTrue(note["include_in_report"])
        self.assertEqual(note["requirement_status"], {a: "met", b: "unmet"})

    def test_b78__both_boxes_checked_is_stored_as_no_assessment(self):
        a = self.labels[0]
        self.client.post("/notes/new", data={
            "title": "Widerspruch", "is_report_entry": "1",
            f"req_met_{a}": "on", f"req_unmet_{a}": "on"})
        # Server treats the (JS-prevented) contradiction as "no assessment".
        self.assertEqual(NOTES_STORE.list_notes(OWNER)[0]["requirement_status"], {})

    def test_b79__note_without_switch_is_not_a_report_entry(self):
        self.client.post("/notes/new", data={"title": "Normal"})  # switch not submitted
        note = NOTES_STORE.list_notes(OWNER)[0]
        self.assertFalse(note["include_in_report"])

    def test_b80__edit_can_flip_report_flag_and_update_verdicts(self):
        a, b = self.labels[0], self.labels[1]
        self.client.post("/notes/new", data={
            "title": "P", "is_report_entry": "1", f"req_met_{a}": "on"})
        nid = NOTES_STORE.list_notes(OWNER)[0]["id"]
        # Edit: keep it a report entry, clear A1, set A2 unmet.
        resp = self.client.post(f"/notes/{nid}/edit", data={
            "title": "P", "text": "", "is_report_entry": "1", f"req_unmet_{b}": "on"})
        self.assertEqual(resp.status_code, 200)
        note = NOTES_STORE.get(nid, OWNER)
        self.assertTrue(note["include_in_report"])
        self.assertEqual(note["requirement_status"], {b: "unmet"})
        # Edit again without the switch → drops out of the report.
        self.client.post(f"/notes/{nid}/edit", data={"title": "P"})
        self.assertFalse(NOTES_STORE.get(nid, OWNER)["include_in_report"])

    def test_b81__edit_dialog_prechecks_switch_and_verdicts(self):
        a = self.labels[0]
        self.client.post("/notes/new", data={
            "title": "P", "is_report_entry": "1", f"req_met_{a}": "on"})
        nid = NOTES_STORE.list_notes(OWNER)[0]["id"]
        html = self.client.get(f"/notes/{nid}/edit").data.decode()
        self.assertRegex(html, r'id="is-report-entry"[^>]*\bchecked\b')
        self.assertRegex(html, rf'name="req_met_{a}"[^>]*\bchecked\b')

    def test_b82__list_marks_report_entries_with_badge(self):
        self.client.post("/notes/new", data={"title": "PE", "is_report_entry": "1"})
        self.client.post("/notes/new", data={"title": "NN"})
        html = self.client.get("/notes").data.decode()
        self.assertEqual(html.count("bi-clipboard-check"), 1)  # only the report entry

    def test_b83__auto_title_for_untitled_report_entry(self):
        a, b, c, d = self.labels[0], self.labels[1], self.labels[2], self.labels[3]
        # A1,A3 unmet; A2,A4 met → unmet first, then met, each sorted by number.
        self.assertEqual(_auto_title({a: "unmet", c: "unmet", b: "met", d: "met"}),
                         f"{a}⚠ {c}⚠ {b}✓ {d}✓")
        # Via the route: empty title + assessment → generated title.
        self.client.post("/notes/new", data={
            "title": "  ", "is_report_entry": "1", f"req_unmet_{a}": "on", f"req_met_{b}": "on"})
        self.assertEqual(NOTES_STORE.list_notes(OWNER)[0]["title"], f"{a}⚠ {b}✓")

    def test_b84__untitled_requires_title_without_assessment(self):
        # Report entry, empty title, no assessment → 400.
        self.assertEqual(self.client.post("/notes/new", data={
            "title": "", "is_report_entry": "1"}).status_code, 400)
        # Plain note, empty title → 400 (unchanged behaviour).
        self.assertEqual(self.client.post("/notes/new", data={"title": ""}).status_code, 400)
        self.assertEqual(NOTES_STORE.list_notes(OWNER), [])


# ── audit report ──────────────────────────────────────────────────────

class TestAuditReport(unittest.TestCase):
    """The audit report aggregates all report entries of the active dataset."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = NOTES_STORE.path
        NOTES_STORE.path = Path(self._tmp.name) / "notes.json"
        _auth_session(self.client)
        self.labels = [r["label"] for r in REQUIREMENTS]

    def tearDown(self):
        NOTES_STORE.path = self._orig_path
        self._tmp.cleanup()

    def _entry(self, title, status, owner=OWNER):
        """Report entries belong to the session owner by default: the report is scoped to one
        person since the requirements catalog is per owner+dataset (2026-08-14)."""
        NOTES_STORE.create(title=title, text="", refpoints=[], owner=owner, user="testuser",
                           project="p", dataset=TEST_DATASET, include_in_report=True,
                           requirement_status=status)

    def test_b85__report_requires_project(self):
        client = flask_app.test_client()  # no session
        resp = client.get("/report")
        self.assertEqual(resp.status_code, 302)  # → login

    def test_b86__empty_report_renders_with_placeholder(self):
        resp = self.client.get("/report")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode()
        self.assertIn("Prüfbericht", html)
        self.assertIn("Keine Prüfberichtseinträge", html)

    def test_b87__report_aggregates_and_lists_entries(self):
        a, b = self.labels[0], self.labels[1]
        self._entry("Lauf A", {a: "met", b: "unmet"})
        self._entry("Lauf B", {a: "met"})
        resp = self.client.get("/report")
        html = resp.data.decode()
        self.assertIn("Lauf A", html)
        self.assertIn("Lauf B", html)
        # Overview counts A1: met in both entries, no unmet.
        self.assertRegex(html, rf'{a}</td>\s*<td>[^<]*</td>\s*<td class="num">2</td>'
                               rf'\s*<td class="num">0</td>')
        # Per-entry verdict words present.
        self.assertIn("nicht erfüllt", html)

    def test_b88__report_ignores_plain_notes(self):
        self._entry("Bericht", {})
        NOTES_STORE.create(title="Normale Notiz", text="", refpoints=[], owner=OWNER,
                           user="u", project="p", dataset=TEST_DATASET)  # plain note
        html = self.client.get("/report").data.decode()
        self.assertIn("Bericht", html)
        self.assertNotIn("Normale Notiz", html)
        # Exactly one report entry counted in the entries heading.
        self.assertIn("(1)", html)

    def test_b89__legacy_protocol_entry_still_counts(self):
        # Notes from the first audit-report iteration used note_type instead of include_in_report.
        NOTES_STORE.create(title="Alt", text="", refpoints=[], owner=OWNER, user="testuser",
                           project="p", dataset=TEST_DATASET, requirement_status={})
        # Simulate the legacy field on the stored note.
        data = json.loads(NOTES_STORE.path.read_text(encoding="utf-8"))
        data["notes"][0]["note_type"] = "protocol_entry"
        data["notes"][0]["include_in_report"] = False
        NOTES_STORE.path.write_text(json.dumps(data), encoding="utf-8")
        html = self.client.get("/report").data.decode()
        self.assertIn("Alt", html)
        self.assertIn("(1)", html)

    def test_b90__report_scoped_to_active_dataset(self):
        NOTES_STORE.create(title="Fremd", text="", refpoints=[], owner=OWNER, user="u", project="p",
                           dataset="other", include_in_report=True, requirement_status={})
        self._entry("Eigen", {})
        html = self.client.get("/report").data.decode()
        self.assertIn("Eigen", html)
        self.assertNotIn("Fremd", html)

    def test_b91__report_pdf_returns_pdf_attachment(self):
        self._entry("Lauf A", {self.labels[0]: "met"})
        resp = self.client.get("/report.pdf")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/pdf")
        self.assertTrue(resp.data.startswith(b"%PDF"))
        self.assertIn("attachment", resp.headers.get("Content-Disposition", ""))
        self.assertIn(".pdf", resp.headers.get("Content-Disposition", ""))

    def test_b92__report_pdf_requires_project(self):
        client = flask_app.test_client()  # no session
        self.assertEqual(client.get("/report.pdf").status_code, 302)


class TestBuildMetadata(unittest.TestCase):
    """Reader normalisation in build_metadata (dotted osdar names, CRP manifest)."""

    def test_b109__basename_key_keeps_dotted_osdar_timestamps(self):
        from tools.build_metadata import basename_key
        # Real image extension is stripped, the float-timestamp dot is not (regression guard:
        # Path(...).stem would drop ".700000016" as if it were a suffix → CRP tile mismatch).
        dotted = "osdar23_11_main_station_11.1_rgb_center_276_1631531579.700000016"
        self.assertEqual(basename_key(dotted + ".png"), dotted)
        self.assertEqual(basename_key(dotted), dotted)  # no extension → unchanged
        self.assertEqual(basename_key("RAWPED_set11_V000_I00001.jpg"), "RAWPED_set11_V000_I00001")

    def test_b110__read_concepts_ranks_and_matches_image_ids(self):
        from tools.build_metadata import read_concepts
        with tempfile.TemporaryDirectory() as tmp:
            level = Path(tmp)
            (level / "concepts.csv").write_text(
                "image_name,rank,concept_id,relevance\n"
                "img.a.001,2,10,0.05\n"
                "img.a.001,1,20,0.18\n", encoding="utf-8")
            got = read_concepts(level)
            self.assertIn("img.a.001", got)  # dotted key survives (no ext stripped)
            self.assertEqual([e["rank"] for e in got["img.a.001"]], [1, 2])  # rank-sorted
            self.assertEqual(got["img.a.001"][0]["concept_id"], 20)  # rank 1 = highest relevance


class TestSceneAttributes(unittest.TestCase):
    """Synthetic datasets ship scene_attributes.csv – it replaces the mocked filter values."""

    def test_b120__scene_row_gives_real_objects_and_weather(self):
        from tools.build_metadata import scene_attributes

        row = {"building_present": "1", "wetter": "sonne", "tageszeit": "tag"}
        attrs, prov = scene_attributes("seed1", row, has_person=True)
        self.assertEqual(sorted(attrs["objekte"]), ["gebaeude", "mensch"])
        self.assertEqual(attrs["wetter"], ["sonnig"])  # "sonne" is the pre-2026-08 spelling
        self.assertEqual(attrs["tageszeit"], ["tag"])
        self.assertEqual(prov["objekte"], "scene")
        self.assertEqual(prov["wetter"], "scene")
        self.assertEqual(prov["umgebung"], "mock")  # the scene says nothing about it

    def test_b121__building_flag_absent_means_no_building_token(self):
        from tools.build_metadata import scene_attributes

        attrs, _ = scene_attributes("seed2", {"building_present": "0", "wetter": "wolken",
                                              "tageszeit": "tag"}, has_person=False)
        self.assertEqual(attrs["objekte"], [])
        self.assertEqual(attrs["wetter"], ["bewoelkt"])  # aliased from the old spelling

    def test_b122__missing_weather_falls_back_to_a_mocked_value(self):
        from tools.build_metadata import scene_attributes

        attrs, prov = scene_attributes("seed3", {"building_present": "1"}, has_person=False)
        self.assertIn(attrs["wetter"][0], FILTER_CATEGORIES["wetter"]["options"])
        self.assertEqual(prov["wetter"], "mock")
        self.assertEqual(prov["objekte"], "scene")  # the flag itself is still real

    def test_b123__unknown_token_is_replaced_instead_of_reaching_the_filters(self):
        from tools.build_metadata import scene_attributes

        attrs, prov = scene_attributes("seed4", {"building_present": "1", "wetter": "hagel",
                                                 "tageszeit": "tag"}, has_person=False)
        self.assertIn(attrs["wetter"][0], FILTER_CATEGORIES["wetter"]["options"])
        self.assertEqual(prov["wetter"], "mock")

    def test_b124__filter_matches_the_real_building_token(self):
        from tools.build_metadata import scene_attributes

        attrs, _ = scene_attributes("seed5", {"building_present": "1", "wetter": "sonne",
                                              "tageszeit": "tag"}, has_person=True)
        cfg = empty_filter_config()
        cfg["objekte"] = ["gebaeude"]
        self.assertTrue(image_passes_filter(attrs, cfg))
        cfg["objekte"] = ["schutzwand"]
        self.assertFalse(image_passes_filter(attrs, cfg))

    def test_b176__environment_column_replaces_the_mocked_value(self):
        from tools.build_metadata import scene_attributes

        row = {"building_present": "1", "umgebung": "ueberland", "wetter": "sonne",
               "tageszeit": "tag"}
        attrs, prov = scene_attributes("seed6", row, has_person=True)
        self.assertEqual(attrs["umgebung"], ["ueberland"])
        self.assertEqual(prov["umgebung"], "scene")

    def test_b177__scene_distances_are_real_and_an_empty_cell_stays_unknown(self):
        from tools.build_metadata import scene_attributes

        row = {"building_present": "0", "wetter": "sonne", "tageszeit": "tag",
               "dist_lateral_m": "2.6", "dist_longitudinal_m": "8.71"}
        attrs, prov = scene_attributes("seed7", row, has_person=True)
        self.assertEqual(attrs["dist_lateral_m"], 2.6)
        self.assertEqual(attrs["dist_longitudinal_m"], 8.71)
        self.assertEqual(prov["dist_lateral_m"], "scene")

        # No person in the scene means there is no distance to it. That is a fact, not a gap,
        # so the value stays empty instead of getting a stand-in.
        attrs, prov = scene_attributes("seed8", dict(row, dist_lateral_m="",
                                                     dist_longitudinal_m=""), has_person=False)
        self.assertIsNone(attrs["dist_lateral_m"])
        self.assertEqual(prov["dist_longitudinal_m"], "scene")
        # Untouched slider: no restriction, the image stays. Narrowed: it drops out (b179).
        cfg = empty_filter_config()
        self.assertTrue(image_passes_filter(attrs, cfg))
        cfg["dist_lateral_m"] = [0, 1]
        self.assertFalse(image_passes_filter(attrs, cfg))

    def test_b178__a_csv_without_the_new_columns_keeps_mocking_them(self):
        from tools.build_metadata import scene_attributes

        attrs, prov = scene_attributes("seed9", {"building_present": "1", "wetter": "sonne",
                                                 "tageszeit": "tag"}, has_person=True)
        self.assertEqual(prov["umgebung"], "mock")
        self.assertEqual(prov["dist_lateral_m"], "mock")
        self.assertIn(attrs["umgebung"][0], FILTER_CATEGORIES["umgebung"]["options"])
        self.assertIsInstance(attrs["dist_lateral_m"], float)


class TestManualLabels(unittest.TestCase):
    """`originals/manual_labels.json` (hand-label export) beats scene and mock."""

    CATEGORIES = {
        "umgebung": {"label": "Umgebung",
                     "options": [{"token": "stadt", "label": "Stadt"},
                                 {"token": "tunnel", "label": "Tunnel"}]},
        "objekte": {"label": "Objekte",
                    "options": [{"token": "mensch", "label": "Mensch"}]},
    }

    def _write(self, tmp: Path, payload: dict):
        with open(tmp / MANUAL_LABELS_JSON, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return tmp

    def _payload(self, **overrides) -> dict:
        payload = {
            "schema": MANUAL_LABELS_SCHEMA,
            "dataset": "T",
            "categories": self.CATEGORIES,
            "images": {"img1.jpg": {"umgebung": ["stadt", "tunnel"], "objekte": ["mensch"],
                                    "unclear": False}},
        }
        payload.update(overrides)
        return payload

    def test_b125__labels_and_categories_are_read_in_file_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._write(Path(tmp), self._payload()) / MANUAL_LABELS_JSON
            labels, categories = read_export_labels(path)
            self.assertEqual(list(labels), ["img1"])  # the image extension is stripped
            self.assertEqual([c["key"] for c in categories], ["umgebung", "objekte"])
            self.assertEqual([o["token"] for o in categories[0]["options"]], ["stadt", "tunnel"])

    def test_b126__a_foreign_schema_is_ignored_rather_than_half_applied(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = self._payload(schema="something-else/9")
            path = self._write(Path(tmp), payload) / MANUAL_LABELS_JSON
            self.assertEqual(read_export_labels(path), ({}, []))

    def test_b127__absent_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(resolve_manual_labels(Path(tmp), "nosuchdataset"), ({}, [], None))

    def test_b128__more_categories_than_the_viewer_shows_are_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            cats = {f"k{i}": {"label": f"K{i}", "options": [{"token": f"t{i}", "label": "T"}]}
                    for i in range(MAX_FILTER_CATEGORIES + 2)}
            path = self._write(Path(tmp), self._payload(categories=cats)) / MANUAL_LABELS_JSON
            _, categories = read_export_labels(path)
            self.assertEqual(len(categories), MAX_FILTER_CATEGORIES)

    def test_b129__every_category_is_real_the_distances_stay_mocked(self):
        categories = [{"key": k, "label": v["label"], "options": v["options"]}
                      for k, v in self.CATEGORIES.items()]
        row = {"umgebung": ["stadt", "tunnel"], "objekte": ["mensch"]}
        attrs, prov = manual_attributes("img1", row, categories)
        self.assertEqual(attrs["umgebung"], ["stadt", "tunnel"])  # multi-valued, order kept
        self.assertEqual(prov["umgebung"], "manual")
        self.assertEqual(prov["objekte"], "manual")
        self.assertEqual(prov["dist_lateral_m"], "mock")  # nobody labelled distances

    def test_b130__a_token_outside_its_own_category_is_dropped(self):
        categories = [{"key": k, "label": v["label"], "options": v["options"]}
                      for k, v in self.CATEGORIES.items()]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            attrs, _ = manual_attributes("img1", {"umgebung": ["stadt", "mond"]}, categories)
        self.assertEqual(attrs["umgebung"], ["stadt"])
        self.assertIn("mond", err.getvalue())

    def test_b131__legacy_single_valued_attributes_still_filter_correctly(self):
        """A metadata.json written before the multi-value switch must not filter wrongly."""
        legacy = {"umgebung": "tunnel", "objekte": ["mensch"]}
        self.assertEqual(attribute_tokens(legacy, "umgebung"), ["tunnel"])
        cfg = empty_filter_config()
        cfg["umgebung"] = ["tunnel"]
        self.assertTrue(image_passes_filter(legacy, cfg))
        cfg["umgebung"] = ["stadt"]
        self.assertFalse(image_passes_filter(legacy, cfg))


    # ── the store as a source (no export/copy step on the machine that labelled) ─────────────

    def _store_payload(self) -> dict:
        """What LabelingStore writes: categories as the editable text, labels beside it."""
        return {
            "dataset": "T",
            "categories_text": "# Umgebung\nStadt\nTunnel\n\n# Objekte\nMensch\n",
            "labels": {"img1": {"umgebung": ["stadt"], "objekte": ["mensch"]}},
            "unclear": [], "notes": {},
        }

    def test_b147__the_labelling_store_is_a_source_of_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "labeling_T.json"
            path.write_text(json.dumps(self._store_payload()), encoding="utf-8")
            labels, categories, _ = resolve_manual_labels(Path(tmp), "T", explicit=path)
            self.assertEqual(labels, {"img1": {"umgebung": ["stadt"], "objekte": ["mensch"]}})
            self.assertEqual([c["key"] for c in categories], ["umgebung", "objekte"])
            # slugify() of the tool: the tokens match what the export would have carried
            self.assertEqual([o["token"] for o in categories[0]["options"]], ["stadt", "tunnel"])
            self.assertEqual(categories[0]["label"], "Umgebung")

    def test_b148__the_shape_is_recognised_by_content_not_by_file_name(self):
        """An export renamed to labeling_*.json (or the reverse) must still be read correctly."""
        with tempfile.TemporaryDirectory() as tmp:
            misnamed = Path(tmp) / "labeling_T.json"      # holds an EXPORT
            misnamed.write_text(json.dumps(self._payload()), encoding="utf-8")
            _, categories, _ = resolve_manual_labels(Path(tmp), "T", explicit=misnamed)
            self.assertEqual([c["key"] for c in categories], ["umgebung", "objekte"])

    def test_b149__an_explicit_path_wins_over_the_copy_in_the_dataset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "originals").mkdir()
            self._write(root / "originals", self._payload())          # source 2
            explicit = root / "elsewhere.json"                        # source 1
            explicit.write_text(json.dumps(self._store_payload()), encoding="utf-8")
            _, categories, source = resolve_manual_labels(root, "T", explicit=explicit)
            self.assertEqual(source, explicit)
            self.assertEqual([o["token"] for o in categories[0]["options"]], ["stadt", "tunnel"])

    def test_b150__a_broken_category_text_does_not_produce_half_a_catalogue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "labeling_T.json"
            broken = {**self._store_payload(), "categories_text": "Stadt\nTunnel\n"}  # no heading
            path.write_text(json.dumps(broken), encoding="utf-8")
            with contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(read_store_labels(path), ({}, []))
            self.assertIn("Kategorientext", err.getvalue())


class TestDatasetOwnCategories(unittest.TestCase):
    """A dataset may bring its own filter categories in `metadata.json` (DATA.md)."""

    OWN = [
        {"key": "sicht", "label": "Sichtverhältnisse",
         "options": [{"token": "klar", "label": "Klar"}, {"token": "diesig", "label": "Diesig"}]},
        {"key": "objekte", "label": "Objekte",
         "options": [{"token": "mensch", "label": "Mensch"}]},
    ]

    def test_b132__the_metadata_block_beats_the_built_in_defaults(self):
        categories = filter_categories({"filter_categories": self.OWN})
        self.assertEqual(list(categories), ["sicht", "objekte"])  # file order kept
        self.assertEqual(categories["sicht"]["options"], ["klar", "diesig"])
        self.assertEqual(categories["sicht"]["label"], "Sichtverhältnisse")
        self.assertEqual(categories["sicht"]["labels"]["diesig"], "Diesig")

    def test_b133__without_the_block_the_defaults_apply(self):
        categories = filter_categories({})
        self.assertEqual(list(categories), list(FILTER_CATEGORIES))
        self.assertEqual(categories["wetter"]["options"], FILTER_CATEGORIES["wetter"]["options"])
        self.assertEqual(categories["wetter"]["labels"], {})  # the i18n catalogue knows them

    def test_b134__a_category_the_dataset_does_not_have_cannot_restrict_anything(self):
        categories = filter_categories({"filter_categories": self.OWN})
        cfg = empty_filter_config(categories)
        self.assertNotIn("umgebung", cfg)
        cfg["umgebung"] = ["tunnel"]  # left over from another dataset
        self.assertTrue(image_passes_filter({"sicht": ["klar"]}, cfg, categories=categories))

    def test_b135__the_dialog_shows_the_datasets_own_labels(self):
        """The i18n catalogue wins where it knows a key; the rest falls back to the file."""
        meta = dict(load_metadata(TEST_DATASET), filter_categories=self.OWN)
        original = app_module.load_metadata
        app_module.load_metadata = lambda name: meta
        try:
            client = app_module.app.test_client()
            _auth_session(client)
            client.get("/")
            html = client.get("/panel/1/config").get_data(as_text=True)
        finally:
            app_module.load_metadata = original
        self.assertIn("Sichtverhältnisse", html)  # unknown to i18n → the labeller's wording
        self.assertIn('value="diesig"', html)
        self.assertIn("Objekte", html)  # known to i18n → translated as before
        self.assertNotIn('value="bahnuebergang"', html)  # not a category of this dataset


class TestFeaturedImages(unittest.TestCase):
    """Images pulled to the front of every image list (featured_images.toml, demo preparation)."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        _auth_session(self.client)
        self._featured = app_module.FEATURED_IMAGES

    def tearDown(self):
        app_module.FEATURED_IMAGES = self._featured
        load_metadata.cache_clear()  # drop whatever order a test installed

    def _with_featured(self, names):
        """Installs a featured list for the mock dataset and returns the reloaded metadata."""
        app_module.FEATURED_IMAGES = {TEST_DATASET: names}
        load_metadata.cache_clear()
        return load_metadata(TEST_DATASET)

    def test_b169__featured_images_come_first_the_rest_keeps_its_order(self):
        ids = [img["id"] for img in METADATA["images"]]
        wanted = [ids[3], ids[1]]  # deliberately out of order and out of the front
        order = [img["id"] for img in self._with_featured(wanted)["images"]]
        self.assertEqual(order[:2], wanted)
        self.assertEqual(order[2:], [i for i in ids if i not in wanted])

    def test_b170__a_featured_name_may_carry_its_extension(self):
        first = METADATA["images"][2]
        order = [img["id"] for img in self._with_featured([first["filename"]])["images"]]
        self.assertEqual(order[0], first["id"])

    def test_b171__unknown_and_duplicate_names_are_skipped_not_fatal(self):
        ids = [img["id"] for img in METADATA["images"]]
        order = [img["id"] for img in
                 self._with_featured(["no_such_image", ids[2], ids[2]])["images"]]
        self.assertEqual(order[0], ids[2])
        self.assertEqual(len(order), len(ids))  # no image lost, none duplicated

    def test_b172__the_gallery_shows_the_featured_image_first(self):
        """End-to-end: the order reaches the rendered panel, not just the metadata."""
        featured = METADATA["images"][4]
        self._with_featured([featured["id"]])
        self.client.get("/")
        _configure(self.client, 1, xai_method="original")
        html = self.client.post("/panel/1/gallery").get_data(as_text=True)
        shown = re.findall(r"originals/imgs/([\w.-]+)", html)
        self.assertTrue(shown, "gallery shows no original images")
        self.assertEqual(shown[0], featured["filename"])

    def test_b173__the_shipped_featured_file_is_readable_and_well_formed(self):
        """A typo in the file people edit shortly before a demo must fail here, not on stage."""
        self.assertTrue(app_module.FEATURED_IMAGES_PATH.exists())
        featured = app_module._load_featured_images(app_module.FEATURED_IMAGES_PATH)
        self.assertIn("RailPer", featured)  # the dataset the demo runs on
        for dataset, names in featured.items():
            self.assertIsInstance(names, list, dataset)
            for name in names:
                self.assertIsInstance(name, str, dataset)

    def test_b174__a_missing_file_leaves_every_order_untouched(self):
        self.assertEqual(app_module._load_featured_images(Path("/nowhere/featured.toml")), {})


class TestDatasetPathGuard(unittest.TestCase):
    """Path handling for /data and /thumb: traversal always blocked, symlinks only on request."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # Resolved on purpose: in the app `base` always comes from _safe_dataset_base(), which
        # resolves it. A temporary directory may well sit behind a symlink (macOS: /tmp →
        # /private/tmp, or a symlinked $TMPDIR), and comparing a resolved path against an
        # unresolved base would fail for reasons that have nothing to do with the guard.
        tmp_root = Path(self._tmp.name).resolve()
        self.base = tmp_root / "dataset"
        (self.base / "xai").mkdir(parents=True)
        self.outside = tmp_root / "elsewhere"
        (self.outside / "high").mkdir(parents=True)
        (self.outside / "high" / "img.jpg").write_bytes(b"jpeg")
        (self.base / "xai" / "Grad-CAM").symlink_to(self.outside)
        self._flag = app_module.FOLLOW_DATASET_SYMLINKS

    def tearDown(self):
        app_module.FOLLOW_DATASET_SYMLINKS = self._flag
        self._tmp.cleanup()

    def _path(self, relpath, follow):
        app_module.FOLLOW_DATASET_SYMLINKS = follow
        return app_module._dataset_path(self.base, relpath)

    def test_b136__strict_mode_refuses_a_symlink_leaving_the_dataset(self):
        self.assertIsNone(self._path("xai/Grad-CAM/high/img.jpg", follow=False))

    def test_b137__relaxed_mode_serves_through_the_symlink(self):
        target = self._path("xai/Grad-CAM/high/img.jpg", follow=True)
        self.assertIsNotNone(target)
        self.assertTrue(target.is_file())

    def test_b138__traversal_is_blocked_in_both_modes(self):
        """The relaxed mode relaxes symlinks, never "..": normpath collapses it first."""
        for follow in (False, True):
            self.assertIsNone(self._path("../secret.txt", follow=follow), follow)
            self.assertIsNone(self._path("xai/../../secret.txt", follow=follow), follow)

    def test_b139__a_plain_path_inside_the_dataset_works_in_both_modes(self):
        (self.base / "xai" / "plain.jpg").write_bytes(b"jpeg")
        for follow in (False, True):
            self.assertEqual(self._path("xai/plain.jpg", follow=follow),
                             self.base / "xai" / "plain.jpg")

    def test_b140__the_pre_warm_walks_into_symlinked_directories(self):
        """rglob() skips them silently before 3.13 — an empty pre-warm would go unnoticed."""
        (self.base / "originals" / "imgs").mkdir(parents=True)
        (self.base / "originals" / "imgs" / "orig.jpg").write_bytes(b"jpeg")
        found = {p.name for p in iter_source_images(self.base)}
        self.assertEqual(found, {"orig.jpg", "img.jpg"})


class TestSingleViewPrefetch(unittest.TestCase):
    """The single view prefetches the NEXT image of its own sequence (US: stepping felt slow)."""

    def setUp(self):
        self.client = app_module.app.test_client()
        # One test here creates a collection: redirect the store to a temp file, or the run
        # would write into the real collections.json of the machine it runs on.
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_cols = COLLECTIONS_STORE.path
        COLLECTIONS_STORE.path = Path(self._tmp.name) / "collections.json"
        _auth_session(self.client)
        self.client.get("/")

    def tearDown(self):
        COLLECTIONS_STORE.path = self._orig_cols
        self._tmp.cleanup()

    def _preloaded(self, html: str):
        match = re.search(r'<img class="panel-preload" src="([^"]+)"', html)
        return match.group(1) if match else None

    def _open_single(self, index=0, **fields):
        _configure(self.client, 1, model=_TEST_MODEL, xai_method="original", **fields)
        return self.client.post(f"/panel/1/open/{index}").get_data(as_text=True)

    def test_b141__the_next_image_is_prefetched_hidden(self):
        html = self._open_single(0)
        preload = self._preloaded(html)
        self.assertIsNotNone(preload)
        self.assertIn(METADATA["images"][1]["filename"], preload)
        self.assertIn('aria-hidden="true"', html)
        self.assertIn('fetchpriority="low"', html)

    def test_b142__stepping_on_prefetches_the_one_after(self):
        self._open_single(0)
        html = self.client.post("/panel/1/step/next").get_data(as_text=True)
        self.assertIn(METADATA["images"][2]["filename"], self._preloaded(html))

    def test_b143__the_last_image_prefetches_nothing(self):
        html = self._open_single(len(METADATA["images"]) - 1)
        self.assertIsNone(self._preloaded(html))

    def test_b144__the_gallery_view_prefetches_nothing(self):
        _configure(self.client, 1, model=_TEST_MODEL, xai_method="original")
        self.assertIsNone(self._preloaded(self.client.get("/").get_data(as_text=True)))

    def test_b145__it_follows_the_filter_not_the_metadata_order(self):
        """"Next" must mean the next image of the FILTERED set, not of all images."""
        html = self._open_single(0, filter_umgebung=_FILTER_UMGEBUNG)
        filtered = [img for img in METADATA["images"]
                    if _FILTER_UMGEBUNG in attribute_tokens(img["attributes"], "umgebung")]
        self.assertGreater(len(filtered), 1, "fixture must have >1 image in that facet")
        self.assertIn(filtered[1]["filename"], self._preloaded(html))

    def test_b146__it_follows_a_collection_source(self):
        ids = [img["id"] for img in METADATA["images"][3:5]]
        col = COLLECTIONS_STORE.create(title="C", image_ids=ids, owner=OWNER,
                                       user="testuser", project="Testprojekt", dataset=TEST_DATASET)
        try:
            html = self._open_single(0, image_source=f"collection:{col['id']}")
            self.assertIn(METADATA["images"][4]["filename"], self._preloaded(html))
        finally:
            COLLECTIONS_STORE.delete(col["id"], OWNER)


class TestThumbnailWritePath(unittest.TestCase):
    """The /thumb write path must be built from the *validated* source, not the raw relpath.

    Regression guard for the security review 2026-09-09 (S2): a relpath that passes the read
    check via ".." re-descent used to steer the thumbnail write (extra "thumbs/" prefix) to a
    mkdir()+write OUTSIDE the dataset. Uses a private temp DATASETS_ROOT so a regression can
    never touch the real datasets tree.
    """

    def setUp(self):
        from PIL import Image
        self.client = app_module.app.test_client()
        _auth_session(self.client)  # /data and /thumb require a session (S3)
        self._tmp = tempfile.TemporaryDirectory()
        # Root basename "datasets" so the attacker can re-descend via it (matches the real setup).
        self.root = Path(self._tmp.name).resolve() / "datasets"
        self.base = self.root / "ds"
        img_dir = self.base / "originals" / "imgs"
        img_dir.mkdir(parents=True)
        Image.new("RGB", (40, 30), "white").save(img_dir / "x.png")
        # metadata.json makes "ds" an *offered* dataset, so _safe_dataset_base() accepts it.
        (self.base / "metadata.json").write_text("{}", encoding="utf-8")
        self._orig_root = app_module.DATASETS_ROOT
        app_module.DATASETS_ROOT = self.root

    def tearDown(self):
        app_module.DATASETS_ROOT = self._orig_root
        self._tmp.cleanup()

    def test_b141__thumbnail_write_cannot_escape_the_dataset(self):
        # relpath resolves back inside base (passes the read guard) but, with the old code, the
        # thumb path would land one level up in <root>/datasets/ds/... .
        attack = "%2e%2e/%2e%2e/datasets/ds/originals/imgs/x.png"
        resp = self.client.get(f"/thumb/ds/{attack}")
        self.assertEqual(resp.status_code, 200)
        escaped = self.root / "datasets" / "ds" / "originals" / "imgs" / "x.webp"
        self.assertFalse(escaped.exists(), "thumbnail escaped the dataset directory")
        # It lands where it belongs: inside base/thumbs/.
        self.assertTrue((self.base / "thumbs" / "originals" / "imgs" / "x.webp").exists())

    def test_b142__plain_thumbnail_request_still_works(self):
        resp = self.client.get("/thumb/ds/originals/imgs/x.png")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue((self.base / "thumbs" / "originals" / "imgs" / "x.webp").exists())

    def test_b146__data_requires_a_session(self):
        # S3: without a project in the session the image routes must not serve anything.
        anon = app_module.app.test_client()
        self.assertEqual(anon.get("/data/ds/originals/imgs/x.png").status_code, 403)
        self.assertEqual(anon.get("/thumb/ds/originals/imgs/x.png").status_code, 403)

    def test_b147__non_image_files_are_not_served(self):
        # S3: metadata.json (and CSVs, label files) must stay private even inside an offered dataset.
        self.assertEqual(self.client.get("/data/ds/metadata.json").status_code, 404)

    def test_b148__unoffered_dataset_and_root_are_rejected(self):
        # A directory without metadata.json is not an offered dataset; "." must not reach the root.
        (self.root / "hidden" / "originals").mkdir(parents=True)
        (self.root / "hidden" / "originals" / "y.png").write_bytes(b"x")
        self.assertEqual(self.client.get("/data/hidden/originals/y.png").status_code, 403)
        self.assertEqual(self.client.get("/data/./ds/metadata.json").status_code, 403)

    def test_b149__served_image_carries_nosniff(self):
        resp = self.client.get("/thumb/ds/originals/imgs/x.png")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")


class TestPanelLimit(unittest.TestCase):
    """A hard cap on the number of panels (security review 2026-09-09, S1): unbounded panels
    turn a cheap POST /panel/new into an ever-larger workspace render on every worker."""

    def setUp(self):
        self.client = app_module.app.test_client()
        _auth_session(self.client)
        self.client.get("/")

    def _panel_count(self):
        with self.client.session_transaction() as sess:
            return len(sess.get("panels", []))

    def test_b143__panel_new_stops_at_the_cap(self):
        # Panel 1 exists after the first workspace render; add until the cap, then one more.
        for _ in range(app_module.MAX_PANELS + 5):
            self.client.post("/panel/new")
        self.assertEqual(self._panel_count(), app_module.MAX_PANELS)

    def test_b144__duplicate_cannot_exceed_the_cap(self):
        while self._panel_count() < app_module.MAX_PANELS:
            self.client.post("/panel/new")
        with self.client.session_transaction() as sess:
            first_pid = sess["panels"][0]["id"]
        resp = self.client.post(f"/panel/{first_pid}/duplicate")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._panel_count(), app_module.MAX_PANELS)

    def test_b145__new_panel_button_is_disabled_at_the_cap(self):
        for _ in range(app_module.MAX_PANELS):
            html = self.client.post("/panel/new").data.decode()
        # The last response is rendered at the cap → the button carries the disabled attribute.
        self.assertRegex(html, r'hx-post="/panel/new"[^>]*\bdisabled\b')


class TestStoreLocking(unittest.TestCase):
    """S10: concurrent read-modify-write on a JSON store must not lose writes or reuse next_id.

    Threads (not processes) are enough to exercise it: store_lock.locked() takes flock on a fresh
    fd each time, which serialises across fds within a process too, and _load/_save do file I/O
    that releases the GIL — so without the lock this interleaves and loses writes. Skipped where
    flock is unavailable (Windows dev), because there the lock is a documented no-op.
    """

    @unittest.skipIf(app_module is None or __import__("store_lock").fcntl is None,
                     "flock not available on this platform")
    def test_b155__concurrent_creates_keep_every_note_with_unique_ids(self):
        import threading
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = NotesStore(Path(tmp.name) / "notes.json")
        workers, per_worker = 8, 15

        def make():
            for _ in range(per_worker):
                store.create(title="t", text="x", refpoints=[], owner="user:a",
                             user="a", project="p", dataset="mock")

        threads = [threading.Thread(target=make) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        notes = store.list_notes("user:a")
        self.assertEqual(len(notes), workers * per_worker)  # no write was lost
        ids = [n["id"] for n in notes]
        self.assertEqual(len(set(ids)), len(ids))  # next_id was never reused


class TestSecurityHardeningS5(unittest.TestCase):
    """Assorted hardening from the security review 2026-09-09 (S5)."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_b150__lang_does_not_follow_a_foreign_referer(self):
        # Open-redirect guard: a foreign Referer must not become the redirect target.
        resp = self.client.get("/lang/en", headers={"Referer": "https://evil.test/phish"})
        self.assertEqual(resp.status_code, 302)
        self.assertNotIn("evil.test", resp.headers.get("Location", ""))

    def test_b151__lang_follows_a_same_origin_referer(self):
        resp = self.client.get("/lang/en", headers={"Referer": "http://localhost/notes"})
        self.assertTrue(resp.headers.get("Location", "").endswith("/notes"))

    def test_b152__responses_carry_baseline_security_headers(self):
        resp = self.client.get("/login")
        self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(resp.headers.get("X-Frame-Options"), "DENY")
        self.assertEqual(resp.headers.get("Referrer-Policy"), "same-origin")

    def test_b153__null_byte_in_image_path_is_not_a_500(self):
        _auth_session(self.client)
        resp = self.client.get("/data/mock/originals/%00.png")
        self.assertNotEqual(resp.status_code, 500)
        self.assertIn(resp.status_code, (400, 403, 404))

    def test_b154__setup_error_page_hides_the_absolute_path(self):
        _auth_session(self.client)
        with self.client.session_transaction() as sess:
            sess["project"] = {**sess["project"], "dataset": "ghost-dataset"}
        # _active_dataset() returns the (now missing) dataset; load_metadata raises
        # FileNotFoundError with the absolute path, which the error page must not echo.
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 500)
        body = resp.data.decode()
        self.assertIn("metadata.json", body)
        self.assertNotIn(str(app_module.DATASETS_ROOT), body)


class TestUsageTracking(unittest.TestCase):
    """Opt-in tracking of the click track (tracking.py + the island in base.html).

    The invariants worth a test are the ones that are easy to break by accident: the feature
    must be *absent* unless configured, must record nothing outside the main application, and
    must never store a password.
    """

    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Both are module-level (read at import); the app reads them per request, so patching
        # here is what a configured instance looks like.
        self._enabled, self._store = app_module.TRACKING_ENABLED, app_module.TRACKING_STORE
        self.addCleanup(self._restore)
        app_module.TRACKING_ENABLED = True
        app_module.TRACKING_STORE = TrackingStore(Path(self.tmp.name))

    def _restore(self):
        app_module.TRACKING_ENABLED = self._enabled
        app_module.TRACKING_STORE = self._store

    def _arm(self):
        """Switches tracking on the way the shortcut does, and returns the session id."""
        _auth_session(self.client)
        self.client.post("/tracking/toggle")
        with self.client.session_transaction() as sess:
            return sess.get("tracking_id")

    def _events(self, sid):
        return app_module.TRACKING_STORE.read(sid)

    # ── availability ──────────────────────────────────────────────────────────

    def test_b180__without_configuration_no_tracking_code_and_no_routes(self):
        """The public demo must ship nothing of this: it is what makes the privacy statement
        ("no analytics or tracking tools") true there."""
        app_module.TRACKING_ENABLED = False
        _auth_session(self.client)
        body = self.client.get("/").data.decode()
        self.assertNotIn("__xaiTracking", body)
        self.assertNotIn("tracking-marker", body)
        self.assertEqual(self.client.post("/tracking/toggle").status_code, 404)
        self.assertEqual(self.client.post("/track", json={"events": []}).status_code, 404)

    def test_b181__configured_but_not_armed_ships_the_island_without_the_marker(self):
        _auth_session(self.client)
        body = self.client.get("/").data.decode()
        self.assertIn("__xaiTracking", body)
        self.assertIn('id="tracking-marker"', body)
        self.assertNotIn('data-tracking="on"', body)   # nothing is being recorded yet

    def test_b182__the_toggle_turns_the_marker_on_and_off_again(self):
        _auth_session(self.client)
        first = self.client.post("/tracking/toggle")
        self.assertIn('data-tracking="on"', first.data.decode())
        self.assertIn("(R)", self.client.get("/").data.decode())
        second = self.client.post("/tracking/toggle")
        self.assertNotIn('data-tracking="on"', second.data.decode())
        self.assertNotIn('data-tracking="on"', self.client.get("/").data.decode())

    def test_b183__a_second_arming_starts_a_new_track(self):
        first = self._arm()
        self.client.post("/tracking/toggle")
        second = self._arm()
        self.assertNotEqual(first, second)
        self.assertEqual(len(app_module.TRACKING_STORE.sessions()), 2)

    # ── scope ─────────────────────────────────────────────────────────────────

    def test_b184__the_login_area_carries_the_shortcut_but_no_marker(self):
        """Q4: the mode is armed before the participant logs in — the login area itself is out
        of scope (passwords), so nothing there is recorded and nothing is shown."""
        body = self.client.get("/login").data.decode()
        self.assertIn("__xaiTracking", body)          # the shortcut must work here
        self.assertIn("inScope: false", body)         # …but the recorder does not run
        self.client.post("/tracking/toggle")
        self.assertNotIn('data-tracking="on"', self.client.get("/login").data.decode())

    def test_b198__logging_out_does_not_end_the_recording(self):
        """Q4: the mode is armed once and the browser handed over; a participant logging in or
        out must not silently end the session that is being recorded."""
        sid = self._arm()
        self.client.post("/logout")
        with self.client.session_transaction() as sess:
            self.assertNotIn("user", sess)          # the logout itself still works
            self.assertEqual(sess.get("tracking_id"), sid)
        _auth_session(self.client)                  # the participant logs in again
        self.client.post("/track", json={"events": [{"kind": "click", "el_id": "after-login"}]})
        self.assertIn("after-login", [e.get("el_id") for e in self._events(sid)])

    def test_b185__events_posted_without_a_project_are_not_recorded(self):
        with self.client.session_transaction() as sess:
            sess["user"] = "testuser"
        self.client.post("/tracking/toggle")
        with self.client.session_transaction() as sess:
            sid = sess["tracking_id"]
        resp = self.client.post("/track", json={"events": [{"kind": "click", "el_id": "x"}]})
        self.assertEqual(resp.status_code, 204)
        self.assertEqual([e for e in self._events(sid) if e["kind"] == "click"], [])

    def test_b186__events_posted_while_switched_off_are_not_recorded(self):
        sid = self._arm()
        self.client.post("/tracking/toggle")          # off again
        self.client.post("/track", json={"events": [{"kind": "click", "el_id": "x"}]})
        self.assertEqual([e for e in self._events(sid) if e["kind"] == "click"], [])

    # ── what is recorded ──────────────────────────────────────────────────────

    def test_b187__a_click_is_stored_with_its_session_context(self):
        sid = self._arm()
        self.client.post("/track", json={"events": [
            {"kind": "click", "el_id": "cfg-apply", "label": "Übernehmen", "panel": "1",
             "dialog": "config-modal", "hx": "post /panel/1/config"},
        ]})
        click = [e for e in self._events(sid) if e["kind"] == "click"][0]
        self.assertEqual(click["el_id"], "cfg-apply")
        self.assertEqual(click["label"], "Übernehmen")
        self.assertEqual(click["panel"], "1")
        self.assertEqual(click["dataset"], TEST_DATASET)
        self.assertEqual(click["owner"], OWNER)
        self.assertEqual(click["sid"], sid)
        self.assertIn("srv_ts", click)

    def test_b188__cancelling_a_dialog_is_recorded_although_it_makes_no_request(self):
        """The reason this feature exists: the request log cannot see a cancel."""
        sid = self._arm()
        self.client.post("/track", json={"events": [
            {"kind": "click", "el_id": "cfg-cancel", "label": "Abbrechen", "dialog": "config-modal"},
        ]})
        labels = [e.get("label") for e in self._events(sid)]
        self.assertIn("Abbrechen", labels)

    def test_b189__a_password_value_never_reaches_the_file(self):
        sid = self._arm()
        self.client.post("/track", json={"events": [
            {"kind": "change", "input_type": "password", "name": "password", "value": "hunter2"},
        ]})
        self.assertNotIn("hunter2", json.dumps(self._events(sid)))

    def test_b190__unknown_kinds_and_unknown_fields_are_dropped(self):
        """The file is read by our own analysis later; a client must not define its own schema."""
        sid = self._arm()
        self.client.post("/track", json={"events": [
            {"kind": "exfiltrate", "el_id": "a"},
            {"kind": "click", "el_id": "b", "cookie": "secret", "nested": {"a": 1}},
        ]})
        events = [e for e in self._events(sid) if e["kind"] == "click"]
        self.assertEqual(len(events), 1)
        self.assertNotIn("cookie", events[0])
        self.assertNotIn("nested", events[0])
        self.assertEqual([e for e in self._events(sid) if e["kind"] == "exfiltrate"], [])

    def test_b191__a_broken_body_is_not_an_error(self):
        """Tracking must never turn into a visible application problem."""
        sid = self._arm()
        for body in ({"events": "nope"}, {"nothing": 1}, {"events": [None, 7, "x"]}):
            self.assertEqual(self.client.post("/track", json=body).status_code, 204)
        self.assertEqual([e for e in self._events(sid) if e["kind"] == "click"], [])

    # ── the store itself ──────────────────────────────────────────────────────

    def test_b192__long_values_are_capped_per_field(self):
        event = tracking.sanitize({"kind": "change", "value": "x" * 5000, "label": "y" * 5000})
        self.assertEqual(len(event["value"]), tracking.EVENT_FIELDS["value"])
        self.assertEqual(len(event["label"]), tracking.EVENT_FIELDS["label"])

    def test_b197__an_emptied_field_is_recorded_as_empty_not_as_absent(self):
        """Clearing a note title is an action; "no value" and "value gone" must stay tellable
        apart. Every other field keeps being dropped when empty, to keep the lines small."""
        cleared = tracking.sanitize({"kind": "change", "name": "title", "value": "", "label": ""})
        self.assertEqual(cleared["value"], "")
        self.assertNotIn("label", cleared)

    def test_b193__a_batch_is_capped_in_length(self):
        raw = [{"kind": "click", "el_id": str(i)} for i in range(tracking.MAX_EVENTS_PER_BATCH + 50)]
        self.assertEqual(len(tracking.sanitize_batch(raw)), tracking.MAX_EVENTS_PER_BATCH)

    def test_b194__a_session_id_that_is_not_ours_addresses_no_file(self):
        store = TrackingStore(Path(self.tmp.name))
        for bogus in ("../../etc/passwd", "", "nope", "20260921-143005-XXXXXXXX"):
            self.assertIsNone(store.path_for(bogus))
            self.assertEqual(store.append(bogus, [{"kind": "click"}]), 0)

    def test_b195__the_size_cap_stops_the_file_and_says_so(self):
        store = TrackingStore(Path(self.tmp.name), max_bytes=2000)
        sid = tracking.new_session_id()
        for _ in range(20):
            store.append(sid, tracking.sanitize_batch([{"kind": "click", "label": "x" * 200}]))
        events = store.read(sid)
        self.assertTrue(any(e.get("value") == "cap_reached" for e in events))
        self.assertLess(store.path_for(sid).stat().st_size, 2 * 2000)

    # ── the analysis (tools/analyze_tracking.py) ──────────────────────────────

    def _recorded(self, *events):
        """Writes a track the way a browser would have and returns it read back."""
        sid = tracking.new_session_id()
        store = TrackingStore(Path(self.tmp.name))
        store.append(sid, tracking.sanitize_batch(list(events)), {"sid": sid})
        return store.read(sid)

    def test_b199__a_dwell_time_is_the_gap_to_the_next_real_action(self):
        """The request an action triggers is a consequence, not a step the person took — it
        must not swallow the time, otherwise every click looks like 2 milliseconds."""
        events = self._recorded(
            {"kind": "click", "ts": "2026-09-21T10:00:00.000Z", "label": "Konfigurieren"},
            {"kind": "request", "ts": "2026-09-21T10:00:00.002Z", "hx": "get /panel/1/config"},
            {"kind": "click", "ts": "2026-09-21T10:00:09.000Z", "label": "Anwenden"},
        )
        rows = analyze_tracking.with_durations(events)
        self.assertAlmostEqual(rows[0][1], 9.0)     # the click keeps the thinking time
        self.assertIsNone(rows[1][1])               # the request gets none of its own
        self.assertIsNone(rows[2][1])               # nothing follows the last action

    def test_b200__a_dialog_visit_is_not_split_by_the_requests_inside_it(self):
        """One trip through the configuration dialog is ONE visit. The requests it fires carry
        no dialog of their own and would otherwise cut it into pieces (seen on real data)."""
        events = self._recorded(
            {"kind": "click", "ts": "2026-09-21T10:00:00.000Z", "label": "Konfigurieren"},
            {"kind": "request", "ts": "2026-09-21T10:00:00.002Z", "hx": "get /panel/1/config"},
            {"kind": "change", "ts": "2026-09-21T10:00:04.000Z", "dialog": "config-modal",
             "label": "Anzeige", "value": "CRAFT"},
            {"kind": "request", "ts": "2026-09-21T10:00:04.100Z", "hx": "post /panel/1/counts"},
            {"kind": "submit", "ts": "2026-09-21T10:00:10.000Z", "dialog": "config-modal"},
            {"kind": "request", "ts": "2026-09-21T10:00:10.002Z", "hx": "post /panel/1/config"},
            {"kind": "click", "ts": "2026-09-21T10:00:20.000Z", "label": "Panel duplizieren"},
        )
        visits = analyze_tracking.dialog_durations(analyze_tracking.with_durations(events))
        self.assertEqual(len(visits), 1)
        self.assertEqual(visits[0]["dialog"], "config-modal")
        # 4s orienting after the opening click + 6s in the dialog + 10s until the next action.
        self.assertAlmostEqual(visits[0]["seconds"], 20.0)

    def test_b201__a_long_break_is_reported_as_idle_and_kept_out_of_the_total(self):
        """One coffee break must not turn into "20 minutes of deep engagement"."""
        events = self._recorded(
            {"kind": "click", "ts": "2026-09-21T10:00:00.000Z", "label": "A"},
            {"kind": "click", "ts": "2026-09-21T10:20:00.000Z", "label": "B"},
        )
        summary = analyze_tracking.session_summary(events)
        self.assertEqual(summary["active"], 0.0)
        self.assertAlmostEqual(summary["idle"], 1200.0)
        self.assertAlmostEqual(summary["wall"], 1200.0)

    def test_b202__the_analysis_reads_a_track_the_app_actually_wrote(self):
        """End to end: what /track stores must be what the tool can open."""
        sid = self._arm()
        self.client.post("/track", json={"events": [
            {"kind": "click", "ts": "2026-09-21T10:00:00.000Z", "label": "Neue Notiz",
             "dialog": "notes-dock"},
            {"kind": "change", "ts": "2026-09-21T10:00:07.000Z", "dialog": "notes-dock",
             "el_id": "note-title", "label": "Titel", "value": "Kante bei Grad-CAM"},
        ]})
        store = TrackingStore(Path(self.tmp.name))
        self.assertIn(sid, store.sessions())
        summary = analyze_tracking.session_summary(store.read(sid))
        self.assertEqual([v["dialog"] for v in summary["dialogs"]], ["notes-dock"])
        self.assertIn(("Neue Notiz", 1), summary["top_clicks"])

    def test_b196__the_privacy_statement_follows_the_configuration(self):
        """A configured instance must not keep claiming that nothing is recorded."""
        app_module.TRACKING_ENABLED = False
        plain = self.client.get("/privacy").data.decode()
        app_module.TRACKING_ENABLED = True
        configured = self.client.get("/privacy").data.decode()
        self.assertIn(CATALOGS["de"]["privacy.external_text"], plain)
        self.assertNotIn(CATALOGS["de"]["privacy.tracking"], plain)
        self.assertIn(CATALOGS["de"]["privacy.tracking"], configured)
        self.assertNotIn(CATALOGS["de"]["privacy.external_text"], configured)


if __name__ == "__main__":
    unittest.main()

"""
XAI-Viewer – demonstrator for usage concept v2 (panel concept).

Main workspace with any number of panels. Each panel has its own
configuration (AI model, training level, display/XAI method, filter) and shows
the filtered image set as a gallery. Panel state is stored in the Flask session.

Data comes from an external dataset directory (selected project-wide):
`<datasets_root>/<dataset>/metadata.json` + images under `originals/` and `xai/`.
Canonical names = real names (VGG16/ResNet50/ConvNeXt-T, Grad-CAM, low/mid/high).
"""

import io
import json
import os
import secrets
import tomllib
from datetime import date, datetime, timedelta
from functools import lru_cache
from time import monotonic
from pathlib import Path
from urllib.parse import urlparse

from flask import (Flask, Response, redirect, render_template, request, send_file,
                   session, url_for)
from werkzeug.security import check_password_hash

from auth import (AccountStore, RateLimiter, account_owner, accounts_path, is_guest,
                  new_guest_owner)
from filter_options import (
    CLASSIFICATION_FILTER,
    CONFIDENCE_FILTER,
    DISTANCE_FILTERS,
    attribute_tokens,
    empty_filter_config,
    filter_categories,
    image_passes_filter,
)
from i18n import LANGUAGES, init_app, get_lang, nt, t
from notes import TITLE_MAX, NotesStore
import demo_seed
import docs_render
import legal
from collections_store import CollectionsStore
from requirements_catalog import load_requirements, text_for
from requirements_store import RequirementsStore
import tracking
from tracking import TrackingStore
from version import __product__, __version__
from thumbnails import THUMB_TAG, thumb_rel
from thumbnails import generate as generate_thumbnail

BASE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

app = Flask(__name__)
# Session cookies are client-side and signed with this key (they carry the whole panel state).
# The deployment injects a real secret via the environment (deployment/wsgi.py); the constant
# fallback keeps local development sessions stable across restarts.
app.secret_key = os.environ.get("XAI_SECRET_KEY") or "xai-viewer-dev-secret"
init_app(app)  # register t()/nt()/get_lang/LANGUAGES as Jinja globals

# Footer data. The deployment writes its timestamp next to the app tree (deployment/deploy.py,
# finalize); without that file we are running from a working copy. Read per render (one stat
# call) rather than cached, so a deploy that does not restart the service still shows the truth.
DEPLOYMENT_STAMP_FILE = BASE_DIR.parent / "deployment_date.txt"


def deployment_stamp() -> str:
    try:
        return DEPLOYMENT_STAMP_FILE.read_text(encoding="utf-8").strip() or t("footer.local_dev")
    except OSError:
        return t("footer.local_dev")


app.jinja_env.globals.update(product=__product__, app_version=__version__,
                            deployment_stamp=deployment_stamp)

# ── access protection ─────────────────────────────────────────────────────────
# The app has no real authentication (its "login" takes any name and no password). A
# public deployment is therefore gated by one HTTP-Basic prompt in front of *everything* —
# including the image routes. Hoster-independent on purpose: an Uberspace web backend
# routes straight to gunicorn, there is no .htaccess to hook into. Credentials come from the
# environment (deployment/wsgi.py bridges them out of config.toml); without them the gate stays
# off, so local development and the tests are unaffected.
AUTH_USER = os.environ.get("XAI_AUTH_USER", "")
AUTH_PASSWORD_HASH = os.environ.get("XAI_AUTH_PASSWORD_HASH", "")

if AUTH_USER and not AUTH_PASSWORD_HASH:
    # Half-configured is the dangerous state: the gate is OFF, but whoever filled in a user name
    # believes the instance is protected. The usual cause is the config key — the deployment
    # config must carry `AUTH_PASSWORD_HASH` (a werkzeug hash), not a plain `AUTH_PASSWORD`.
    print(f"WARN: XAI_AUTH_USER is set ({AUTH_USER!r}) but XAI_AUTH_PASSWORD_HASH is empty — "
          "the Basic-Auth gate is therefore OFF and this instance is open. Check the key name "
          "in the deployment config: it is AUTH_PASSWORD_HASH, and it holds a hash "
          "(werkzeug.security.generate_password_hash), not a plain password.")


@app.before_request
def require_basic_auth():
    if not (AUTH_USER and AUTH_PASSWORD_HASH):
        return None
    auth = request.authorization
    if (auth and auth.username and auth.password
            and secrets.compare_digest(auth.username, AUTH_USER)
            and check_password_hash(AUTH_PASSWORD_HASH, auth.password)):
        return None
    return Response("Authentication required.", 401,
                    {"WWW-Authenticate": f'Basic realm="{__product__}"'})


# Panel display modes. "images" = gallery / single image (rendered per xai_method);
# "modelcard" = model overview (confusion matrix + accuracy) – no image set at all.
MODE_IMAGES = "images"
MODE_MODELCARD = "modelcard"

# Preselected model of a new panel: the first of these the dataset has, else its first model.
DEFAULT_MODEL_PREFERENCE = ("ConvNeXt-T", "ResNet50", "VGG16")

# Sentinel value of the "display" dropdown that selects the modelcard mode. The dropdown carries
# the XAI method *and* the mode; panel_config_apply() maps this value onto panel["mode"], so
# xai_method stays a pure rendering attribute and keeps its value across a mode switch.
MODELCARD_OPTION = "__modelcard__"

# ── config + datasets ─────────────────────────────────────────────────────────

def _load_config() -> dict:
    cfg_path = BASE_DIR / "config.toml"
    if cfg_path.exists():
        with open(cfg_path, "rb") as f:
            return tomllib.load(f)
    return {}


CONFIG = _load_config()
# Precedence: environment (set by the deployment, which has no local config.toml) → config.toml
# → default beside the repo. The deployment keeps the datasets *outside* the deployed tree so
# they survive every upload.
_root = os.environ.get("XAI_DATASETS_ROOT") or CONFIG.get("paths", {}).get("datasets_root")
DATASETS_ROOT = Path(_root) if _root else (BASE_DIR.parent.parent / "datasets")
# Same precedence as DATASETS_ROOT: the deployment has no local config.toml, so it sets the
# default via the environment. Getting this wrong is not harmless — a default that is not present
# makes every entry point answer with the "setup required" page (HTTP 500).
DEFAULT_DATASET = (os.environ.get("XAI_DEFAULT_DATASET")
                   or CONFIG.get("dataset", {}).get("default", "RailPer"))

# Images that must sit at the front of every image list (demo/trade-fair preparation): looking
# for a specific picture in 17.500 thumbnails costs time one does not have in front of visitors.
# Deliberately its OWN file and not a section of config.toml — deploy.py excludes every
# config.toml from the upload, so a list kept there would never reach the server. The file itself
# carries the user-facing explanation.
FEATURED_IMAGES_PATH = BASE_DIR / "featured_images.toml"


def _load_featured_images(path: Path) -> dict:
    """``{dataset: [name, ...]}`` from featured_images.toml. Missing or unreadable file -> ``{}``:
    a convenience for demos must never be what keeps the app from starting."""
    if not path.exists():
        return {}
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        print(f"WARN: {path.name} cannot be read ({e}) — no images are pulled to the front.")
        return {}
    featured = {}
    for dataset, section in raw.items():
        names = section.get("images", []) if isinstance(section, dict) else []
        featured[dataset] = [str(n).strip() for n in names if str(n).strip()]
    return featured


FEATURED_IMAGES = _load_featured_images(FEATURED_IMAGES_PATH)

# Imprint, operator part of the privacy policy and contact link: they belong to whoever runs the
# instance, not to the application, so they come from a file per deployment (see legal.py and
# legal.example.toml). Same precedence as everywhere else: environment → config.toml → default.
_legal_file = os.environ.get("XAI_LEGAL_FILE") or CONFIG.get("paths", {}).get("legal_file")
LEGAL_PATH = Path(_legal_file) if _legal_file else BASE_DIR / "legal.toml"
LEGAL = legal.load_legal(LEGAL_PATH)
if not LEGAL:
    print(f"WARN: no legal texts ({LEGAL_PATH.name} not found) — /imprint and /privacy say that "
          "nothing is configured. Template: legal.example.toml.")

# Development only: serve images that a symlink inside the dataset points at (a dev machine may
# link renderings in from a mounted drive instead of copying GBs). Deliberately readable ONLY
# from the local `xai_viewer/config.toml` and NOT from the environment: the deployment has no
# such file (deploy.py excludes every config.toml) and configures the app purely via env vars,
# so the relaxed mode cannot reach production even by accident. See _dataset_path().
FOLLOW_DATASET_SYMLINKS = bool(CONFIG.get("dev", {}).get("follow_dataset_symlinks", False))
if FOLLOW_DATASET_SYMLINKS:
    print("WARN: follow_dataset_symlinks is ON — dataset images are served through symlinks "
          "(development setting, see config.toml.example).")

# Notes are persisted server-side (session cookie is limited to ~4 KB).
_notes_file = CONFIG.get("paths", {}).get("notes_file")
NOTES_STORE = NotesStore(Path(_notes_file) if _notes_file else BASE_DIR / "notes.json")

# Image collections: manually curated image sets, persisted like notes.
_collections_file = CONFIG.get("paths", {}).get("collections_file")
COLLECTIONS_STORE = CollectionsStore(
    Path(_collections_file) if _collections_file else BASE_DIR / "collections.json")

# Audit requirements catalog: the bilingual criteria that protocol entries assess.
# The master file is read-only and acts as the default; each user+dataset can have a private
# copy, edited in the app (see requirements_store.py). REQUIREMENTS is the master, loaded once
# at startup — the *active* catalog for the current session comes from _requirements().
_requirements_file = CONFIG.get("paths", {}).get("requirements_file")
REQUIREMENTS_MASTER_PATH = (Path(_requirements_file) if _requirements_file
                            else BASE_DIR / "requirements.toml")
REQUIREMENTS = load_requirements(REQUIREMENTS_MASTER_PATH)
REQUIREMENTS_STORE = RequirementsStore(REQUIREMENTS_MASTER_PATH, BASE_DIR)

# ── demo mode + identity ──────────────────────────────────────────────────────
# The public demo (2026-08-30) runs the same code as an internal instance, with one switch.
# What it changes is documented per use site; in one sentence: anonymous visitors become
# *throwaway sandboxes* instead of sharing one pool, and everything that could damage real
# working data or fill the disk is limited or switched off.
#
# Off (local development, internal instance): the historical behaviour. A typed name is the
# owner key, so a developer keeps finding their own notes after the session cookie is gone.
#
# Same precedence as everywhere else (environment → config.toml → default) — and the
# environment must be able to switch the mode OFF as well, not only on: `XAI_DEMO_MODE=0`
# in front of a config.toml that enables it is the obvious way to try the normal behaviour,
# and it would be a nasty surprise if that were silently ignored.
_demo_env = os.environ.get("XAI_DEMO_MODE", "").strip().lower()
DEMO_MODE = (_demo_env in ("1", "true", "yes", "on") if _demo_env
             else bool(CONFIG.get("demo", {}).get("enabled", False)))

if DEMO_MODE:
    # Which mode is in effect is not visible on the page at a glance; say it once at startup.
    print(f"INFO: {__product__} runs in DEMO MODE (guest sandboxes, side tools off, "
          "size and amount limits).")

# Pre-created accounts of *known* users (see auth.py + tools/manage_accounts.py). Empty file or no
# file → everyone is a guest, which is what a fresh checkout and the tests want.
ACCOUNTS = AccountStore(accounts_path(BASE_DIR, CONFIG))

# Password guessing is the only real attack surface on a demo with ~20 operator-assigned
# passwords. Per client address, so one visitor cannot lock out the others.
LOGIN_LIMITER = RateLimiter(max_attempts=10, window_seconds=60)

if DEMO_MODE and AUTH_USER:
    print("WARN: demo mode is ON together with Basic-Auth credentials — the HTTP prompt in "
          "front of everything keeps anonymous visitors out. Configure one or the other.")
if DEMO_MODE and not os.environ.get("XAI_SECRET_KEY"):
    print("WARN: demo mode is ON with the built-in development secret key. Session cookies are "
          "signed with a value that is public in the repository — set XAI_SECRET_KEY.")

# ── usage tracking ────────────────────────────────────────────────────────────
# Records the *click track* of a usability session (see tracking.py for what and why). Three
# properties, none of them incidental:
#
#  1. **Off unless switched on in the configuration.** Without it the app ships no tracking code
#     at all — the templates render neither the island nor the shortcut, and the routes answer
#     404. That is what keeps the privacy statement ("no analytics or tracking tools are used")
#     true for the public demo, which is configured without it.
#  2. **Then still off until someone presses the shortcut**, per session. Switching it on is a
#     deliberate act by whoever runs the session, not a state the app drifts into.
#  3. **Only in the main application.** Nothing is recorded before a project is open: the login
#     area is out of scope (it carries passwords), and the interesting part starts with panel 1's
#     configuration dialog anyway.
_tracking_env = os.environ.get("XAI_TRACKING", "").strip().lower()
TRACKING_ENABLED = (_tracking_env in ("1", "true", "yes", "on") if _tracking_env
                    else bool(CONFIG.get("tracking", {}).get("enabled", False)))
# Kept out of the deployed tree by default is NOT possible here (the app only knows its own
# directory), so the default sits beside the JSON stores and .gitignore covers it. A deployment
# that wants the records to survive `deploy.py --purge` points XAI_TRACKING_DIR elsewhere.
_tracking_dir = (os.environ.get("XAI_TRACKING_DIR")
                 or CONFIG.get("paths", {}).get("tracking_dir"))
TRACKING_STORE = TrackingStore(Path(_tracking_dir) if _tracking_dir else BASE_DIR / "tracking")

# Ctrl+Alt+Shift+T. Matched on ``event.code`` (the physical key), never on ``event.key``: with
# Alt held, macOS reports the composed character ("†") and a German layout reports AltGr
# characters, so the key name is not the letter on the cap. Ctrl+Shift+R — the obvious choice —
# is the browsers' hard reload on Windows and Linux and therefore unusable.
TRACKING_SHORTCUT_CODE = "KeyT"

if TRACKING_ENABLED:
    print("INFO: usage tracking is AVAILABLE (Ctrl+Alt+Shift+T switches it on per session, "
          f"records into {TRACKING_STORE.directory}). Tell participants and check the privacy "
          "statement before running a session.")
if TRACKING_ENABLED and DEMO_MODE:
    print("WARN: usage tracking is available together with demo mode — anonymous visitors could "
          "switch it on and their notes would be recorded, which the privacy statement does not "
          "cover. Configure one or the other.")

# Ceilings. They exist so that a visitor cannot fill the disk or make every other visitor's
# requests slow (the JSON stores are rewritten whole on each write). Deliberately far above what
# an honest demo session produces: hitting one of these means someone is hammering the app.
DEMO_MAX_NOTES = 100          # per owner and dataset
DEMO_MAX_COLLECTIONS = 50     # per owner and dataset
DEMO_MAX_CATALOG_CHARS = 20_000
# Panels live in the session, not on disk, but each one makes every workspace render walk the
# whole dataset again (one stat() + up to four exists() per image, per panel). Without a cap an
# anonymous client can POST /panel/new in a loop and turn a cheap request into a huge render on
# every worker. Far above what a real side-by-side comparison needs; enforced in every route
# that creates a panel (new + duplicate), not demo-gated — the cost is the same either way.
MAX_PANELS = 12

# Whole-request size cap. Without one, a POST body is read into memory unbounded — the review
# tool's clipboard upload does exactly that. In demo mode the tool is off, so the cap can be
# small enough that no single request is worth anything to an attacker.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 if DEMO_MODE else 32 * 1024 * 1024

# Session cookie. HttpOnly is Flask's default; SameSite=Lax additionally means a cross-site
# form post carries no session, which blunts CSRF before the token check even runs.
# Secure is opt-in: it would silently break local HTTP testing, and only the deployment knows
# it is behind HTTPS (XAI_COOKIE_SECURE=1).
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("XAI_COOKIE_SECURE", "").strip().lower()
                          in ("1", "true", "yes"),
)
# Sessions stay non-permanent on purpose: the cookie dies with the browser session, which is
# exactly the promised guest lifetime ("when your session ends, you start over"). The
# server-side sweep (see _sweep_expired_guests) collects what the closed browser left behind.

# Side tools work on shared, real working data without an owner concept. In a public demo they
# are therefore not merely uninteresting but harmful (e.g. a visitor could overwrite the hand
# labels of a whole dataset). They stay registered and are refused at the
# door, which is also what makes the block testable.
DEMO_BLOCKED_PREFIXES = (
)


@app.before_request
def block_side_tools_in_demo():
    if DEMO_MODE and request.path.startswith(DEMO_BLOCKED_PREFIXES):
        # 404 stays the status (the route must not look available), but a bare "Not found."
        # sends whoever hits it hunting for a typo instead of the mode they are running in.
        return Response(t("demo.tool_blocked"), 404, mimetype="text/plain; charset=utf-8")
    return None


# Cross-site write protection. Every mutating route is a plain POST, so without this a link on
# a foreign page could delete someone's notes in their logged-in session.
#
# Two layers, no token:
#  1. ``SESSION_COOKIE_SAMESITE="Lax"`` above — a browser does not attach the session cookie to
#     a cross-site POST at all, so the request arrives unauthenticated and changes nothing.
#  2. The check below — for the cases SameSite does not cover (an old browser, or a same-site
#     but different-origin page): the write is refused unless it declares our own origin.
#
# A request without ``Origin``/``Referer`` passes. That is deliberate and not a hole: a browser
# always sends one of the two on a cross-site POST and cannot be told to omit it, and a client
# that *can* omit it (curl, a script) has no victim's cookie to abuse. It keeps server-side
# tools and the test suite working without threading a token through every form.
#
# ``XAI_ALLOWED_ORIGINS`` (comma-separated) exists for a deployment whose proxy rewrites the
# Host header: if every action starts answering 403 after a deploy, that is what happened.
ALLOWED_ORIGINS = {o.strip().lower().rstrip("/")
                   for o in os.environ.get("XAI_ALLOWED_ORIGINS", "").split(",") if o.strip()}
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")


@app.before_request
def reject_cross_site_writes():
    if request.method in SAFE_METHODS:
        return None
    stated = request.headers.get("Origin") or request.headers.get("Referer")
    if not stated:
        return None
    host = urlparse(stated).netloc.lower()
    if host == request.host.lower() or stated.lower().rstrip("/") in ALLOWED_ORIGINS:
        return None
    return Response("Cross-site request rejected.", 403)


# ── guest sandbox expiry ──────────────────────────────────────────────────────
# A guest sandbox is throwaway by design ("when your session ends, you start over"), but its
# data outlives the closed browser on the server. Without a sweep the stores would grow for
# ever — and they are rewritten whole on every write, so growth costs every visitor latency.
#
# The clock is the sandbox's own newest timestamp, not the session: the server never learns
# that a browser was closed. A guest who comes back inside the window finds their work.
# Accounts are never touched — their persistence is the point.
GUEST_TTL_HOURS = int(os.environ.get("XAI_GUEST_TTL_HOURS", "48"))
# The sweep walks both stores, so it runs on a request at most this often (lazily — no cron,
# which the hosting does not readily offer).
SWEEP_INTERVAL_SECONDS = 900
_last_sweep = 0.0


def _sweep_expired_guests(now: datetime = None) -> int:
    """Deletes every guest sandbox whose newest entry is older than the TTL. Returns the number
    of sandboxes removed. Idempotent and safe to call at any time."""
    cutoff = ((now or datetime.now()) - timedelta(hours=GUEST_TTL_HOURS)).isoformat(
        timespec="seconds")
    removed = 0
    for owner in NOTES_STORE.owners() | COLLECTIONS_STORE.owners():
        if not is_guest(owner):
            continue
        # Both stamps have the same format, so a string comparison is the date comparison.
        activity = max(NOTES_STORE.last_activity(owner), COLLECTIONS_STORE.last_activity(owner))
        if activity and activity >= cutoff:
            continue
        NOTES_STORE.delete_owner(owner)
        COLLECTIONS_STORE.delete_owner(owner)
        REQUIREMENTS_STORE.delete_owner(owner)
        removed += 1
    return removed


@app.before_request
def sweep_expired_guests_occasionally():
    global _last_sweep
    if not DEMO_MODE:
        return None
    now = monotonic()
    if now - _last_sweep < SWEEP_INTERVAL_SECONDS:
        return None
    _last_sweep = now
    try:
        _sweep_expired_guests()
    except OSError:
        pass  # a sweep that fails must never take a request down with it
    return None


@app.route("/robots.txt")
def robots_txt():
    """Keep the demo out of search engines: it is meant to be handed out, not found."""
    return Response("User-agent: *\nDisallow: /\n", mimetype="text/plain")


@app.after_request
def security_headers(response):
    """Baseline hardening on every response (security review 2026-09-09, S5).

    A full Content-Security-Policy is deliberately *not* set here: the app renders small inline
    JS islands on purpose (build-free, HTMX re-evaluates them on swap), so a useful CSP needs a
    per-response nonce on every one of them — a separate, larger change. These three headers cost
    nothing and need no nonce. ``setdefault`` so a route may still set a stricter value itself.
    """
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    # Belt and braces next to robots.txt — a crawler that ignores the file still sees this.
    if DEMO_MODE:
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    return response


# ── usage tracking: routes ────────────────────────────────────────────────────
# Configuration and reasoning above (search "usage tracking"); the recording itself lives in
# tracking.py and in the island in base.html.

def _tracking_in_scope() -> bool:
    """Whether the current page may record at all — the main application, i.e. a project is open."""
    return "project" in session


def _tracking_on() -> bool:
    """Whether this session is currently recording *and* the page is in scope."""
    return bool(TRACKING_ENABLED and session.get("tracking") and _tracking_in_scope())


@app.context_processor
def tracking_context():
    """``tracking_available`` decides whether the island ships at all, ``tracking_on`` whether it
    records. Both are needed in base.html, which every page extends."""
    return {
        "tracking_available": TRACKING_ENABLED,
        "tracking_on": _tracking_on(),
        "tracking_in_scope": _tracking_in_scope(),
        "tracking_shortcut_code": TRACKING_SHORTCUT_CODE,
    }


@app.route("/tracking/toggle", methods=["POST"])
def tracking_toggle():
    """Flips the tracking mode for this session and answers with the navbar marker.

    The marker is rendered server-side and swapped in by the island, so the page does not have
    to reload — a reload mid-session would itself distort what we are measuring.
    """
    if not TRACKING_ENABLED:
        return "", 404
    if session.get("tracking"):
        # Record the switch-off in the track being closed, before the id is gone.
        _tracking_write([{"kind": "tracking", "value": "off"}])
        session.pop("tracking", None)
        session.pop("tracking_id", None)
    else:
        session["tracking"] = True
        session["tracking_id"] = tracking.new_session_id()
        _tracking_write([{"kind": "tracking", "value": "on"}])
    # The body is the marker for the navbar slot — empty outside the main application, where
    # there is nothing to show. The new state therefore travels in a header instead: on the
    # login page an empty body means "no marker here", not "not recording".
    response = Response(render_template("partials/_tracking_marker.html"))
    response.headers["X-Tracking"] = "on" if session.get("tracking") else "off"
    return response


def _tracking_write(events: list) -> int:
    """Appends sanitized events to this session's track, stamped with who and where.

    The stamp is per event rather than once per file: a track is read as a stream, and a session
    that switches project mid-way would otherwise be ambiguous.
    """
    sid = session.get("tracking_id")
    if not sid or not session.get("tracking"):
        return 0
    project = session.get("project") or {}
    context = {
        "sid": sid,
        "owner": _owner(),
        "user": session.get("user") or "",
        "role": session.get("role") or "",
        "project": project.get("name") or "",
        "dataset": project.get("dataset") or "",
        "lang": get_lang(),
    }
    try:
        return TRACKING_STORE.append(sid, tracking.sanitize_batch(events), context)
    except OSError:
        # A failed recording must never take the interaction it was recording down with it.
        app.logger.warning("tracking: could not write session %s", sid)
        return 0


@app.route("/track", methods=["POST"])
def track():
    """Receives a batch of events from the island. Always 204 — the client cannot react anyway,
    and an error status would only turn a tracking problem into a visible application problem."""
    if not TRACKING_ENABLED:
        return "", 404
    if not _tracking_on():
        return "", 204  # switched off, or a page outside the main application
    _tracking_write((request.get_json(silent=True) or {}).get("events"))
    return "", 204


# Example content a fresh demo sandbox starts with (see demo_seed.py). Read once at startup:
# it is deployed content, not user data, so it does not change while the server runs.
DEMO_SEED_NOTES = demo_seed.load(BASE_DIR / "demo_seed" / "notes.json")


def _seed_demo_notes(dataset: str, project: str):
    """Hands a demo visitor the prepared notes for the dataset they just opened.

    Only in demo mode: on an internal instance the notes list belongs to the person working in
    it, and example content would be clutter. Failures are swallowed — an empty workspace is a
    much smaller problem than not being able to open a project at all."""
    if not DEMO_MODE or not DEMO_SEED_NOTES:
        return
    try:
        demo_seed.apply_notes(NOTES_STORE, DEMO_SEED_NOTES, _owner(), dataset, project)
    except (OSError, KeyError, TypeError, ValueError):
        pass


def _owner() -> str:
    """The key the current session's data belongs to — see auth.py for why this is not the name.

    Sessions predating this change (and the test helpers) carry only ``user``; they are read as
    the account owner of that name, which is exactly what they meant before guests existed.
    """
    owner = session.get("owner")
    if owner:
        return owner
    return account_owner(session.get("user", "?"))


def _notes(dataset: str = None) -> list:
    """The current owner's notes. Every route goes through here, so the filter cannot be
    forgotten — the stores require the owner argument for exactly that reason (notes.py)."""
    return NOTES_STORE.list_notes(_owner(), dataset=dataset)


def _note(nid: int):
    """The current owner's note with this id, or None (someone else's is indistinguishable)."""
    return NOTES_STORE.get(nid, _owner())


def _collections(dataset: str = None) -> list:
    return COLLECTIONS_STORE.list_collections(_owner(), dataset)


def _collection(cid):
    return COLLECTIONS_STORE.get(cid, _owner()) if cid is not None else None


def _collection_titles(dataset: str = None) -> set:
    return COLLECTIONS_STORE.titles(_owner(), dataset)


# Display tokens: only "original" (→ i18n common.original) and the classes (dataset-side
# class_labels) need translation – models, XAI methods, and levels are displayed as-is
# (real names, A10–A12).

# XAI filename: <basename>_<appendix>.<ext>. Appendix differs from the directory token.
METHOD_APPENDIX = {"Grad-CAM": "gradcam"}

# Methods whose per-image asset lives in a sub-folder of the level dir (not directly in it).
# CRAFT's attribution map is xai/<Model>/CRAFT/<level>/concept_attribution_maps/<id>_craft.jpg.
METHOD_SUBDIR = {"CRAFT": "concept_attribution_maps"}


def _dataset_dir(name: str) -> Path:
    return DATASETS_ROOT / name


def available_datasets() -> list:
    """Dataset names under DATASETS_ROOT that carry a metadata.json (= ready to be opened).

    Scanned on every call rather than cached: a dataset generated while the server runs
    shows up in the project dialog without a restart.
    """
    if not DATASETS_ROOT.is_dir():
        return []
    return sorted(d.name for d in DATASETS_ROOT.iterdir()
                  if d.is_dir() and (d / "metadata.json").exists())


def _featured_first(images: list, names: list, dataset: str) -> list:
    """Stable partition of the image list: the images named in ``names`` first, in exactly that
    order, every other image behind them in its unchanged order.

    A name is matched against the image id (the file's basename); the extension may be written
    along ("img_7.jpg" == "img_7"). Unknown names are skipped with a warning instead of raising —
    a typo shortly before a demo should be visible in the log, not take the app down."""
    if not names:
        return images
    by_id = {img["id"]: img for img in images}
    front, taken = [], set()
    for name in names:
        img = by_id.get(name) or by_id.get(Path(name).stem)
        if img is None:
            app.logger.warning("featured image %r is not part of dataset %r - skipped", name, dataset)
            continue
        if img["id"] in taken:  # named twice: the first mention decides its position
            continue
        taken.add(img["id"])
        front.append(img)
    return front + [img for img in images if img["id"] not in taken]


@lru_cache(maxsize=None)
def load_metadata(name: str) -> dict:
    path = _dataset_dir(name) / "metadata.json"
    if not path.exists():
        raise FileNotFoundError(str(path))
    with open(path, encoding="utf-8") as f:
        meta = json.load(f)
    # The single place where the image order is decided: every image list in the app (gallery,
    # single view, confusion-matrix cells, side tools) walks meta["images"], so pulling
    # the featured ones to the front here is enough — no filter and no view needs to know.
    featured = FEATURED_IMAGES.get(name)
    if featured:
        meta["images"] = _featured_first(meta["images"], featured, name)
    return meta


@app.errorhandler(FileNotFoundError)
def handle_missing_metadata(e):
    # Show only the file name, never the absolute server path: this page is reachable by any
    # visitor when a dataset is incompletely deployed, and the full path would leak the
    # deployment layout and the hosting account name (S5). The full path goes to the log.
    app.logger.warning("dataset file missing: %s", e)
    missing = Path(str(e)).name or "metadata.json"
    dataset = _active_dataset() if "project" in session else DEFAULT_DATASET
    return render_template("setup_error.html", missing=missing, dataset=dataset), 500


def _active_dataset() -> str:
    return session.get("project", {}).get("dataset", DEFAULT_DATASET)


def _categories() -> dict:
    """Filter categories of the ACTIVE dataset – its own where `metadata.json` carries them,
    otherwise the defaults from filter_options (see there). A session is bound to exactly one
    dataset, so this is unambiguous per request; `load_metadata` is cached, so it is cheap."""
    try:
        return filter_categories(load_metadata(_active_dataset()))
    except FileNotFoundError:
        return filter_categories({})


def _label_maps(meta: dict):
    """(model_labels, level_labels, xai_labels) – real names mapped 1:1, only 'original' is translated."""
    model_labels = {m: m for m in meta["models"]}
    level_labels = {lv: lv for lv in meta["levels"]}
    methods = sorted({mth for mdl in meta["xai_availability"].values() for mth in mdl})
    xai_labels = {"original": t("common.original")}
    xai_labels.update({mth: mth for mth in methods})
    return model_labels, level_labels, xai_labels


def _flat_availability(meta: dict) -> dict:
    """{model: [methods]} – flat list for the JS in the config dialog."""
    return {model: sorted(methods) for model, methods in meta["xai_availability"].items()}


# ── panel state (session) ─────────────────────────────────────────────────────

def _default_panel(pid: int, meta: dict) -> dict:
    models = meta["models"]
    levels = meta["levels"]
    return {
        "id": pid,
        "model": next((m for m in DEFAULT_MODEL_PREFERENCE if m in models),
                      models[0] if models else DEFAULT_MODEL_PREFERENCE[-1]),
        "level": "high" if "high" in levels else (levels[-1] if levels else "high"),
        "xai_method": "original",
        # Display mode, see MODE_* above. Kept separate from xai_method on purpose.
        "mode": MODE_IMAGES,
        "filters": empty_filter_config(filter_categories(meta)),
        # Modelcard mode restricts the confusion matrix to at most ONE category option
        # (None = all images). Deliberately separate from "filters": the image filters stay
        # untouched across a mode switch, and "exactly one option" is enforced structurally.
        "modelcard_filter": None,
        # Image source: where the (ordered) base image set comes from. {"type": "filter"} = own
        # filter (default); {"type": "panel", "id": N} = adopt the filter result of panel N.
        # Deliberately structured as a discriminator so that {"type": "selection", ...} (manually
        # curated sets) can be added later without refactoring.
        "image_source": {"type": "filter"},
        "visible": True,
        "view": "gallery",   # "gallery" | "single"
        "index": 0,          # position in the filtered image list (single-image view)
        # CRP single-view selection: which concept (by rank) and which image type to show. Only
        # consulted for CRP panels; reset to rank-1 / heatmap when a new image is OPENED (gallery
        # click), but kept across ◀/▶ stepping so the same rank/view can be compared image to image.
        "crp_rank": 1,       # selected concept rank (1 = most relevant)
        "crp_view": "heatmap",  # "heatmap" | "grid" (example grid)
        # CRAFT single-view selection: None = per-image attribution map (default), else the id of a
        # GLOBAL concept prototype. Reset to None (map) on open (click); kept across stepping.
        "craft_concept": None,
        "linked": False,     # coupled gallery scrolling with other linked panels
        "configured": False, # load gallery only after first configuration confirmation
    }


def _clamp_index(index: int, total: int) -> int:
    if total <= 0:
        return 0
    return max(0, min(index, total - 1))


def _coupled_targets(panels: list, panel: dict) -> list:
    """Target panels for an action: if the panel is linked, all visible linked panels
    (coupled); otherwise just this one. Modelcard panels are skipped silently – they have no
    image set to open or step through (Q3a)."""
    if panel.get("linked"):
        return [p for p in panels if p.get("visible") and p.get("linked")
                and p.get("mode") != MODE_MODELCARD]
    return [] if panel.get("mode") == MODE_MODELCARD else [panel]


def _get_panels() -> list:
    return session.setdefault("panels", [])


def _save_panels(panels: list):
    session["panels"] = panels
    session.modified = True


def _find_panel(panels: list, pid: int):
    return next((p for p in panels if p["id"] == pid), None)


def _next_panel_id() -> int:
    pid = session.get("next_panel_id", 1)
    session["next_panel_id"] = pid + 1
    return pid


def _image_source(panel: dict) -> dict:
    """Image source of a panel (backwards-compatible: missing/unknown = own filter)."""
    src = panel.get("image_source")
    if isinstance(src, dict) and src.get("type") in ("filter", "panel", "collection"):
        return src
    return {"type": "filter"}


def _panel_followers(panels: list, pid: int) -> list:
    """IDs of the panels that DECLARE panel <pid> as their image source.

    Declaration, not effect: a reference survives while the source is temporarily unusable
    (e.g. switched to modelcard mode) and revives afterwards. The chaining guard in
    _image_source_from_form() relies on this – an effective view would let a panel adopt a
    foreign source while a follower still points at it, forming exactly the chain we exclude.
    For "who really adopts my image set" use _effective_source_panel()."""
    return [p["id"] for p in panels
            if _image_source(p).get("type") == "panel" and _image_source(p).get("id") == pid]


def _effective_source_panel(panels: list, panel: dict):
    """The panel whose filter result THIS panel actually adopts, or None (→ own filter).

    A usable source exists, is not the panel itself, is filter-based (no chaining – Q5a) and
    shows images: a modelcard panel has no image set, so followers fall back to their own
    filter (gracefully robust, same as for a deleted source)."""
    src = _image_source(panel)
    if src["type"] != "panel":
        return None
    source = _find_panel(panels, src.get("id"))
    if source is None or source["id"] == panel["id"]:
        return None
    if _image_source(source)["type"] != "filter" or source.get("mode") == MODE_MODELCARD:
        return None
    return source


# ── helpers ───────────────────────────────────────────────────────────────────

# Candidate suffixes for XAI assets. The export does not guarantee the original's
# extension (LRP writes .jpg even for .png originals), so we probe instead of assuming.
_XAI_SUFFIX_CANDIDATES = (".jpg", ".jpeg", ".png")


def _xai_rel_path(img: dict, model: str, method: str, level: str, ds_dir: Path):
    """Dataset-relative path of the XAI asset, or None if no matching file exists.

    The original's extension is tried first (fast path, matches Grad-CAM), then the
    remaining candidates – the LRP export writes .jpg even for .png originals."""
    appendix = METHOD_APPENDIX.get(method, method.lower())
    base = f"xai/{model}/{method}/{level}"
    subdir = METHOD_SUBDIR.get(method)  # CRAFT: per-image map lives in a sub-folder
    if subdir:
        base = f"{base}/{subdir}"
    stem = f"{base}/{img['id']}_{appendix}"
    orig_ext = Path(img["filename"]).suffix
    exts = [orig_ext] + [e for e in _XAI_SUFFIX_CANDIDATES if e != orig_ext.lower()]
    for ext in exts:
        rel = f"{stem}{ext}"
        if (ds_dir / rel).exists():
            return rel
    return None


def _crp_heatmap_rel(model: str, level: str, img_id: str, concept_id: int, rank: int) -> str:
    """Per-image CRP heatmap (where a concept activates in THIS image). Flat concept_heatmaps/
    folder, filename encodes concept id + rank, always .jpg (see DATA.md)."""
    return (f"xai/{model}/CRP/{level}/concept_heatmaps/"
            f"{img_id}_concept{concept_id}_rank{rank}_crp.jpg")


def _crp_grid_rel(model: str, level: str, concept_id: int) -> str:
    """Global CRP concept prototype (what the concept looks like), stored once per concept in
    concepts/ and shared across all images of a model/level (see DATA.md)."""
    return f"xai/{model}/CRP/{level}/concepts/concept{concept_id}_3x3.jpg"


def _crp_rel_path(img: dict, model: str, level: str, meta: dict, ds_dir: Path):
    """Dataset-relative path of the CRP tile: the rank-1 concept's heatmap, or None.

    CRP has no single overlay; the top-relevance concept (rank 1 in metadata's xai_concepts)
    represents the image in the gallery (Phase 1)."""
    concepts = (meta.get("xai_concepts", {}).get(model, {})
                .get("CRP", {}).get(level, {}).get(img["id"]))
    if not concepts:
        return None
    top = concepts[0]  # entries are rank-sorted; [0] == rank 1
    rel = _crp_heatmap_rel(model, level, img["id"], top["concept_id"], top["rank"])
    return rel if (ds_dir / rel).exists() else None


def _crp_concepts_display(img_id: str, model: str, level: str, meta: dict, dataset: str, ds_dir: Path):
    """Rank-ordered concept list for the CRP explorer (single view, Phase 2): each concept with
    its per-image heatmap + its (global) example-grid URLs. Empty if the image has no CRP concepts.
    Only the image's own concepts are listed – computed for the opened image, not every tile."""
    entries = (meta.get("xai_concepts", {}).get(model, {})
               .get("CRP", {}).get(level, {}).get(img_id, []))
    out = []
    for e in entries:
        cid, rank = e["concept_id"], e["rank"]
        item = {"rank": rank, "concept_id": cid, "relevance": e["relevance"]}
        for key, rel in (("heatmap_url", _crp_heatmap_rel(model, level, img_id, cid, rank)),
                         ("grid_url", _crp_grid_rel(model, level, cid))):
            path = ds_dir / rel
            if path.exists():
                try:
                    mt = int(path.stat().st_mtime)
                except OSError:
                    mt = 0
                item[key] = url_for("dataset_image", dataset=dataset, relpath=rel, v=mt)
            else:
                item[key] = None
        out.append(item)
    return out


def _craft_chips(img_id: str, model: str, level: str, meta: dict, selected_id):
    """Ordered CRAFT concept list for the single-view chips. When per-image activation is available
    (xai_concepts), concepts are ordered by the image's activation; otherwise by concept_id. Each
    chip carries colour + global_importance (a global, 0..1 concept property) + activation_score (the
    raw per-image magnitude, no fixed scale) + activation_share (that magnitude as a share of the
    total concept activation in THIS image, 0..1 summing to 1 across concepts — a legible per-image
    distribution) + selected flag + file. Empty if the model/level has no CRAFT concepts."""
    glob = {c["concept_id"]: c for c in
            meta.get("xai_global_concepts", {}).get(model, {}).get("CRAFT", {}).get(level, [])}
    if not glob:
        return []
    ranked = (meta.get("xai_concepts", {}).get(model, {})
              .get("CRAFT", {}).get(level, {}).get(img_id))
    order = ([(e["concept_id"], e.get("activation_score")) for e in ranked] if ranked
             else [(cid, None) for cid in sorted(glob)])
    # Reference for the share: the total activation across this image's concepts. Guard against a
    # non-positive or missing total so we never divide by zero (then activation_share stays None).
    acts = [a for _, a in order if a is not None]
    total_act = sum(acts) if acts else None
    chips = []
    for cid, act in order:
        g = glob.get(cid)
        if not g:
            continue
        chips.append({
            "concept_id": cid,
            "color": g.get("color"),
            "global_importance": g.get("global_importance"),
            "activation_score": act,
            "activation_share": (act / total_act) if (act is not None and total_act) else None,
            "file": g.get("file"),
            "selected": cid == selected_id,
        })
    return chips


def _filtered_base_images(panel: dict, meta: dict) -> list:
    """Ordered base image set from the panel's OWN filter (categorical + distance +
    classification). Classification is prediction-derived (model + level of this panel);
    returns raw meta image dicts in metadata order (identity, no rendering yet)."""
    preds = meta["predictions"].get(panel["model"], {}).get(panel["level"], {})
    classification = panel["filters"].get(CLASSIFICATION_FILTER["key"], "")
    categories = filter_categories(meta)

    result = []
    for img in meta["images"]:
        if not image_passes_filter(img["attributes"], panel["filters"], categories=categories):
            continue
        pred = preds.get(img["id"], {})
        predicted = pred.get("predicted_class")
        correct = predicted == img["true_class"]
        # Prediction filter: images without a prediction count as neither correct nor incorrect.
        if not _classification_matches(classification, predicted, correct):
            continue
        if not _confidence_matches(_predicted_confidence(pred), panel["filters"]):
            continue
        result.append(img)
    return result


def _collection_base_images(cid, meta: dict) -> list:
    """Images of a stored collection, in the collection's saved order.

    A snapshot of ids (Q5a): filters are NOT applied. Ids no longer present in the dataset are
    dropped silently; a missing/deleted collection yields an empty set (the panel then shows
    "no match" – deletion handling is a later management feature)."""
    collection = _collection(cid) if cid is not None else None
    if collection is None:
        return []
    by_id = {img["id"]: img for img in meta["images"]}
    return [by_id[iid] for iid in collection["image_ids"] if iid in by_id]


def _resolve_base_images(panel: dict, panels: list, meta: dict) -> list:
    """Ordered base image list of a panel according to its image source.

    type=filter: own filter. type=panel: filter result of the source panel (order included).
    type=collection: the stored id snapshot (no filtering). Unusable panel source → fallback to
    own filter, see _effective_source_panel()."""
    src = _image_source(panel)
    if src["type"] == "collection":
        return _collection_base_images(src.get("id"), meta)
    return _filtered_base_images(_effective_source_panel(panels, panel) or panel, meta)


def _render_image(panel: dict, img: dict, dataset: str, meta: dict) -> dict:
    """Maps a base image to the display of THIS panel (its own model/level/xai_method):
    display URL, prediction, confidences. This way followers show the same base images with
    their own rendering (e.g. Grad-CAM of a different model)."""
    model, level, method = panel["model"], panel["level"], panel["xai_method"]
    preds = meta["predictions"].get(model, {}).get(level, {})
    class_labels = meta["class_labels"]
    ds_dir = _dataset_dir(dataset)

    pred = preds.get(img["id"], {})
    predicted = pred.get("predicted_class")
    correct = predicted == img["true_class"]
    conf_map = pred.get("confidences", {})
    # Confidences for ALL classes, sorted descending; predicted class flagged.
    confidences = sorted(
        ({"label": class_labels.get(c, c), "value": conf_map.get(c, 0.0),
          "predicted": c == predicted} for c in meta["classes"]),
        key=lambda d: d["value"], reverse=True,
    )

    xai_missing = False
    if method == "original":
        rel = f"originals/imgs/{img['filename']}"
    else:
        if method == "CRP":
            rel = _crp_rel_path(img, model, level, meta, ds_dir)
        else:
            rel = _xai_rel_path(img, model, method, level, ds_dir)
        if rel is None:
            rel = f"originals/imgs/{img['filename']}"
            xai_missing = True

    # Cache-buster carries the source mtime, so a re-exported image yields a new thumbnail
    # URL and bypasses the browser cache (THUMB_TAG alone only covers size/quality changes).
    try:
        src_mtime = int((ds_dir / rel).stat().st_mtime)
    except OSError:
        src_mtime = 0
    thumb_v = f"{THUMB_TAG}-{src_mtime}"

    return {
        "id": img["id"],
        "filename": img["filename"],
        "rel": rel,  # dataset-relative path of the DISPLAYED variant (XAI rendering or original)
        "true_class": class_labels.get(img["true_class"], img["true_class"]),
        "url": url_for("thumbnail", dataset=dataset, relpath=rel, v=thumb_v),
        "full_url": url_for("dataset_image", dataset=dataset, relpath=rel, v=src_mtime),
        "xai_missing": xai_missing,
        "predicted_class": class_labels.get(predicted, predicted or "?"),
        "confidence": conf_map.get(predicted),
        "confidences": confidences,
        "correct": correct,
    }


def _panel_images(panel: dict, panels: list, dataset: str, meta: dict) -> list:
    """Display list of a panel: resolved base image set × its own rendering."""
    base = _resolve_base_images(panel, panels, meta)
    return [_render_image(panel, img, dataset, meta) for img in base]


def _localized_filter_meta():
    """Filter structure with translated labels (session language), in exactly the dict form the
    templates expect (spec.label / spec.options.items() …). This keeps the filter templates
    unchanged at the data-label level; only the labels now come from the i18n catalog.

    Where the dataset brings its own categories, the catalogue still wins for keys it knows –
    the built-in set stays translated, a foreign one falls back to the labeller's wording.
    Returns (categories, classification, confidence, distance)."""
    categories = {
        key: {
            "label": t(f"filter.cat.{key}.label", default=spec["label"] or key),
            "options": {v: t(f"filter.cat.{key}.opt.{v}",
                             default=spec["labels"].get(v, v)) for v in spec["options"]},
        }
        for key, spec in _categories().items()
    }
    classification = {
        "key": CLASSIFICATION_FILTER["key"],
        "label": t("filter.classification.label"),
        "options": {v: t(f"filter.classification.opt.{v or 'alle'}")
                    for v in CLASSIFICATION_FILTER["options"]},
    }
    confidence = {
        "key": CONFIDENCE_FILTER["key"],
        "active_key": CONFIDENCE_FILTER["active_key"],
        "label": t("filter.confidence.label"),
        "enable_label": t("filter.confidence.enable"),
        "min_label": t("filter.confidence.min_label"),
        "max_label": t("filter.confidence.max_label"),
        "hint": t("filter.confidence.hint"),
        "min": CONFIDENCE_FILTER["min"],
        "max": CONFIDENCE_FILTER["max"],
        "step": CONFIDENCE_FILTER["step"],
    }
    distance = {
        key: {"label": t(f"filter.dist.{key}.label"), "unit": t(f"filter.dist.{key}.unit"),
              "min": spec["min"], "max": spec["max"]}
        for key, spec in DISTANCE_FILTERS.items()
    }
    return categories, classification, confidence, distance


def _filter_summary(filters: dict) -> list:
    """Compact description of the active filters for the panel header (translated)."""
    parts = []
    for key, spec in _categories().items():
        selected = filters.get(key, [])
        if selected:
            labels = [t(f"filter.cat.{key}.opt.{v}", default=spec["labels"].get(v, v))
                      for v in selected]
            parts.append(f"{t(f'filter.cat.{key}.label', default=spec['label'] or key)}: "
                         f"{', '.join(labels)}")
    for key, spec in DISTANCE_FILTERS.items():
        lo, hi = filters.get(key, [spec["min"], spec["max"]])
        if [lo, hi] != [spec["min"], spec["max"]]:
            parts.append(f"{t(f'filter.dist.{key}.label')}: {lo}–{hi} {t(f'filter.dist.{key}.unit')}")
    klass = filters.get(CLASSIFICATION_FILTER["key"], "")
    if klass:
        parts.append(f"{t('filter.classification.label')}: {t(f'filter.classification.opt.{klass}')}")
    if filters.get(CONFIDENCE_FILTER["active_key"]):
        lo, hi = filters.get(CONFIDENCE_FILTER["key"],
                             [CONFIDENCE_FILTER["min"], CONFIDENCE_FILTER["max"]])
        parts.append(f"{t('filter.confidence.label')}: {lo:g}–{hi:g} %")
    return parts


def _classification_matches(classification: str, predicted, correct: bool) -> bool:
    if classification == "korrekt":
        return predicted is not None and correct
    if classification == "inkorrekt":
        return predicted is not None and not correct
    return True


def _predicted_confidence(pred: dict):
    """Confidence of the PREDICTED class in percent, or None without a prediction.

    "How sure was the model about its own answer" – with two classes this is never below 50 %
    (RailPer/VGG16-high: 58.4 … 100 %), so the useful range of the filter is 50–100.
    """
    predicted = pred.get("predicted_class")
    if predicted is None:
        return None
    value = pred.get("confidences", {}).get(predicted)
    return None if value is None else value * 100


def _confidence_matches(confidence, filters: dict) -> bool:
    """Prediction-derived filter (like the classification one), bounds inclusive.

    Inactive → no restriction. While active, images without a prediction drop out: an unknown
    confidence cannot be inside a range (same stance as the classification filter, which counts
    them as neither correct nor incorrect)."""
    if not filters.get(CONFIDENCE_FILTER["active_key"]):
        return True
    if confidence is None:
        return False
    lo, hi = filters.get(CONFIDENCE_FILTER["key"],
                         [CONFIDENCE_FILTER["min"], CONFIDENCE_FILTER["max"]])
    return lo <= confidence <= hi


def _faceted_counts(meta: dict, model: str, level: str, filters: dict) -> dict:
    """Image count per filter option – faceted: each facet excludes its *own* selection
    but respects all other active filters (+ distance + classification). O(images × cat.),
    computed only when the dialog is opened/changed – gallery rendering is not affected."""
    preds = meta["predictions"].get(model, {}).get(level, {})
    classification = filters.get(CLASSIFICATION_FILTER["key"], "")
    dataset_categories = filter_categories(meta)
    categories = {key: {} for key in dataset_categories}
    cls = {"": 0, "korrekt": 0, "inkorrekt": 0}

    for img in meta["images"]:
        attrs = img["attributes"]
        pred = preds.get(img["id"], {})
        predicted = pred.get("predicted_class")
        correct = predicted == img["true_class"]
        # The confidence range restricts every facet (it has no counters of its own, so it is
        # never the "own" facet): counters that ignore an active restriction would mislead.
        in_confidence = _confidence_matches(_predicted_confidence(pred), filters)

        # Classification facet: all categories + distance (own selection = exclude classification)
        if in_confidence and image_passes_filter(attrs, filters, categories=dataset_categories):
            cls[""] += 1
            if predicted is not None:
                cls["korrekt" if correct else "inkorrekt"] += 1

        # Category facets: classification + all other categories (own category excluded)
        if in_confidence and _classification_matches(classification, predicted, correct):
            for key in dataset_categories:
                if not image_passes_filter(attrs, filters, skip_key=key,
                                           categories=dataset_categories):
                    continue
                for v in attribute_tokens(attrs, key):
                    categories[key][v] = categories[key].get(v, 0) + 1

    return {"categories": categories, "classification": cls}


# ── modelcard / model overview ───────────────────────────────────────

# Fields of the metadata block above the confusion matrix, in display order. Keyed per model
# AND training level: epochs, training date and nominal accuracy differ between low/mid/high,
# and the stable fields (parameters, algorithm, datasets) simply repeat per level.
MODELCARD_META_FIELDS = ("params", "epochs", "trained_at", "algorithm",
                         "train_dataset", "test_dataset", "nominal_accuracy")
MODELCARD_META_NA = "N/A"


def _fmt_meta_value(key, value):
    """Display string for one metadata field. Raw numbers live in metadata.json; the compact
    forms (millions, percent) are produced here so the JSON stays language-neutral and reusable."""
    if value is None or value == "":
        return MODELCARD_META_NA
    if key == "params":
        n = float(value)
        if n >= 1e6:
            return f"{n / 1e6:.1f} M"
        if n >= 1e3:
            return f"{n / 1e3:.0f} k"
        return str(int(n))
    if key == "nominal_accuracy":
        return f"{float(value) * 100:.1f} %"
    return str(value)


def _model_metadata(meta: dict, model: str, level: str) -> list:
    """Label/value pairs for the metadata block, in MODELCARD_META_FIELDS order.

    Reads the optional metadata.json key ``model_meta[model][level]`` (from
    model_meta.csv); absent fields render as "N/A". Raw values are formatted per field via
    _fmt_meta_value so the stored JSON keeps neutral numbers.
    """
    values = meta.get("model_meta", {}).get(model, {}).get(level, {})
    return [{"label": t(f"modelcard.meta.{key}"),
             "value": _fmt_meta_value(key, values.get(key))}
            for key in MODELCARD_META_FIELDS]


def _modelcard_selection(panel: dict):
    """Validated modelcard selection of a panel: (key, value) or None (= all images)."""
    sel = panel.get("modelcard_filter")
    if not isinstance(sel, dict):
        return None
    key, value = sel.get("key"), sel.get("value")
    categories = _categories()
    if key in categories and value in categories[key]["options"]:
        return key, value
    return None


def _in_selection(attrs: dict, selection) -> bool:
    """Whether an image carries the selected category option (None = no restriction)."""
    if selection is None:
        return True
    key, value = selection
    return value in attribute_tokens(attrs, key)


def _accuracy(correct: int, total: int):
    return correct / total if total else None


def _modelcard_stats(meta: dict, model: str, level: str, selection) -> dict:
    """Confusion matrix + accuracies of one model/level – a single pass over all images.

    - ``matrix[true][predicted]``: counts restricted to `selection` (None = all images).
    - ``option_accuracy``: per filter option the accuracy of the images carrying that option,
      INDEPENDENT of the current selection (numbers stay stable while clicking around), plus
      the overall accuracy under ``all``. None where nothing was predicted.

    Images without a prediction are ignored – they are neither correct nor incorrect (same
    semantics as the classification filter). Cost is O(images x categories), like
    _faceted_counts(): cheap enough to compute live, no precomputation needed.

    NOTE: model *metadata* (architecture, training data, parameters) comes separately from the
    optional ``model_meta`` key, see _model_metadata().
    """
    preds = meta["predictions"].get(model, {}).get(level, {})
    classes = list(meta["class_labels"])
    matrix = {tc: {pc: 0 for pc in classes} for tc in classes}
    overall = [0, 0]  # [correct, total]
    per_option = {key: {v: [0, 0] for v in spec["options"]}
                  for key, spec in filter_categories(meta).items()}
    sel_correct = sel_total = 0

    for img in meta["images"]:
        predicted = preds.get(img["id"], {}).get("predicted_class")
        if predicted is None:
            continue
        true_class = img["true_class"]
        correct = predicted == true_class
        attrs = img["attributes"]

        if (_in_selection(attrs, selection)
                and true_class in matrix and predicted in matrix[true_class]):
            matrix[true_class][predicted] += 1
            sel_total += 1
            sel_correct += correct

        overall[0] += correct
        overall[1] += 1
        for key in per_option:
            for v in attribute_tokens(attrs, key):
                if v in per_option[key]:
                    per_option[key][v][0] += correct
                    per_option[key][v][1] += 1

    # Row/column sums for the axis labels ("Person (122 ≙ 49%)"); percentages of the whole
    # (selected) subset, so a row sum and a column sum each add up to sel_total.
    row_totals = {tc: sum(matrix[tc].values()) for tc in classes}
    col_totals = {pc: sum(matrix[tc][pc] for tc in classes) for pc in classes}

    return {
        "classes": classes,
        "class_labels": meta["class_labels"],
        "matrix": matrix,
        "row_totals": row_totals,
        "col_totals": col_totals,
        "total": sel_total,
        "correct": sel_correct,
        "accuracy": _accuracy(sel_correct, sel_total),
        "option_accuracy": {
            "all": _accuracy(*overall),
            "categories": {key: {v: _accuracy(*c) for v, c in opts.items()}
                           for key, opts in per_option.items()},
        },
    }


def _matrix_cell_images(meta: dict, model: str, level: str, selection, cells: set) -> list:
    """Image ids that fall into one of the given confusion-matrix cells, in metadata order.

    ``cells`` is a set of (true_class, predicted_class) tuples. Restricted to the same subset as
    the matrix itself (``selection`` = the modelcard scope; None = all images). Images without a
    prediction are ignored – they are in no cell (same semantics as _modelcard_stats)."""
    preds = meta["predictions"].get(model, {}).get(level, {})
    ids = []
    for img in meta["images"]:
        predicted = preds.get(img["id"], {}).get("predicted_class")
        if predicted is None:
            continue
        if not _in_selection(img["attributes"], selection):
            continue
        if (img["true_class"], predicted) in cells:
            ids.append(img["id"])
    return ids


def _parse_matrix_cells(form, classes: list) -> set:
    """Parses the ticked matrix cells ('<true>:<predicted>') from the form, keeping only valid
    class pairs."""
    cells = set()
    for raw in form.getlist("cell"):
        true_class, _, predicted = raw.partition(":")
        if true_class in classes and predicted in classes:
            cells.add((true_class, predicted))
    return cells


def _auto_collection_title(model: str, level: str, n_cells: int, existing: set) -> str:
    """Auto title 'ConfMatrix-{model}-{level}-{n}fields-{m}', m counting up to avoid collisions."""
    base = f"ConfMatrix-{model}-{level}-{n_cells}fields"
    m = 1
    while f"{base}-{m}" in existing:
        m += 1
    return f"{base}-{m}"


@app.template_filter("accuracy_pct")
def _accuracy_pct(value) -> str:
    """Accuracy as a percentage; en dash when there is no prediction data."""
    return "–" if value is None else f"{round(value * 100)}%"


@app.template_filter("share_pct")
def _share_pct(count, total) -> str:
    """`count` as a percentage of `total` (matrix cell / axis share); en dash when total is 0."""
    return "–" if not total else f"{round(count / total * 100)}%"


def _modelcard_scope_label(panel: dict) -> str:
    """Readable scope of a modelcard evaluation: "All images" or "Environment: Station"."""
    selection = _modelcard_selection(panel)
    if selection is None:
        return t("config.modelcard_all")
    key, value = selection
    return f"{t(f'filter.cat.{key}.label')}: {t(f'filter.cat.{key}.opt.{value}')}"


def _modal_context(panel: dict, panels: list, meta: dict) -> dict:
    model_labels, level_labels, xai_labels = _label_maps(meta)
    categories, classification, confidence, distance = _localized_filter_meta()
    # Selectable image sources: other filter-based panels that actually show images
    # (no chaining, no self-selection – Q5a; a modelcard panel has no image set to offer).
    source_panels = [
        {"id": p["id"],
         "descr": f"{p['model']} / {p['level']} / {xai_labels.get(p['xai_method'], p['xai_method'])}"}
        for p in panels
        if p["id"] != panel["id"] and _image_source(p)["type"] == "filter"
        and p.get("mode") != MODE_MODELCARD
    ]
    # Selection-independent accuracies for the modelcard radio labels (see _modelcard_stats).
    option_accuracy = _modelcard_stats(meta, panel["model"], panel["level"], None)["option_accuracy"]
    # Selectable collections (snapshots): available as a base image source regardless of chaining.
    collections = [{"id": c["id"], "title": c["title"], "count": len(c["image_ids"])}
                   for c in _collections(_active_dataset())]
    return {
        "panel": panel,
        "modelcard_option": MODELCARD_OPTION,
        "modelcard_selection": _modelcard_selection(panel),
        "modelcard_accuracy": option_accuracy,
        "model_labels": model_labels,
        "level_labels": level_labels,
        "xai_labels": xai_labels,
        "xai_availability": _flat_availability(meta),
        "filter_categories": categories,
        "distance_filters": distance,
        "classification_filter": classification,
        "confidence_filter": confidence,
        "filter_counts": _faceted_counts(meta, panel["model"], panel["level"], panel["filters"]),
        # Image source: current selection, selectable sources, and whether this panel is itself a source
        # (in that case it may not adopt a foreign source → select disabled).
        "image_source": _image_source(panel),
        "source_panels": source_panels,
        "collections": collections,
        "source_has_followers": bool(_panel_followers(panels, panel["id"])),
    }


def _build_page_data(panels: list, dataset: str) -> dict:
    """Embedded workspace state data for the frontend (e.g. arrow-key gate).
    Modelcard panels never count as single-image panels – they cannot be stepped (Q3a)."""
    singles = [p for p in panels
               if p["visible"] and p.get("view") == "single"
               and p.get("mode") != MODE_MODELCARD]
    return {
        "page_type": "workspace",
        "dataset": dataset,
        "panel_count": len(panels),
        "visible_panel_ids": [p["id"] for p in panels if p["visible"]],
        "single_panel_ids": [p["id"] for p in singles],
        "linked_single_panel_ids": [p["id"] for p in singles if p.get("linked")],
    }


def _single_view_image(panel: dict, image: dict, dataset: str, meta: dict) -> dict:
    """The single-view variant of an image: which file the panel actually shows for it.

    For most XAI methods that is the rendered image itself. CRP and CRAFT are special because the
    panel carries a *selection* beside the image (concept + type resp. prototype), which survives
    stepping — so the variant has to be derived per image, not once per view. Used for the shown
    image AND for the prefetch of the next one, so both agree on the file.
    """
    # CRP single view keeps the normal single-image layout: it shows ONE image, chosen by two
    # header dropdowns — concept (by rank) and image type (heatmap | example grid). The rank-
    # ordered concept list drives the dropdown; the selected concept/type overrides full_url.
    if panel.get("xai_method") == "CRP":
        concepts = _crp_concepts_display(image["id"], panel["model"], panel["level"],
                                         meta, dataset, _dataset_dir(dataset))
        if concepts:
            rank = panel.get("crp_rank", 1)
            crp_view = panel.get("crp_view", "heatmap")
            sel = next((c for c in concepts if c["rank"] == rank), concepts[0])
            url = sel["grid_url"] if crp_view == "grid" else sel["heatmap_url"]
            return {**image,
                    "crp_concepts": concepts,
                    "crp_rank": sel["rank"],
                    "crp_view": crp_view,
                    # Fall back across type/concept if a specific file is missing.
                    "full_url": url or sel["heatmap_url"] or sel["grid_url"] or image["full_url"]}
    # CRAFT single view: chips switch between the per-image attribution map (default, full_url
    # already set) and a GLOBAL concept prototype. Chips carry the concept colour + importance
    # and, where available, are ordered by the image's per-concept activation.
    if panel.get("xai_method") == "CRAFT":
        chips = _craft_chips(image["id"], panel["model"], panel["level"], meta,
                             panel.get("craft_concept"))
        if chips:
            sel = next((c for c in chips if c["selected"]), None)
            image = {**image, "craft_chips": chips,
                     "craft_concept": sel["concept_id"] if sel else None}
            if sel and sel.get("file"):
                rel = f"xai/{panel['model']}/CRAFT/{panel['level']}/concepts/{sel['file']}"
                try:
                    mt = int((_dataset_dir(dataset) / rel).stat().st_mtime)
                except OSError:
                    mt = 0
                image = {**image, "full_url": url_for("dataset_image", dataset=dataset,
                                                      relpath=rel, v=mt)}
    return image


def _render_workspace(oob: bool = False):
    """Renders the whole workspace. ``oob=True`` marks the block for an out-of-band swap so a
    response targeted elsewhere (collections dialog) can refresh the panels along the way."""
    dataset = _active_dataset()
    meta = load_metadata(dataset)
    model_labels, level_labels, xai_labels = _label_maps(meta)

    panels = _get_panels()
    panel_views = []
    for p in panels:
        is_card = p.get("mode") == MODE_MODELCARD
        rendered = p["visible"] and p.get("configured", True)
        images = _panel_images(p, panels, dataset, meta) if rendered and not is_card else []
        # Modelcard panels have no image set → no gallery/single view, no filter summary,
        # no image-source relationship in either direction (Q3a).
        modelcard = (_modelcard_stats(meta, p["model"], p["level"], _modelcard_selection(p))
                     if rendered and is_card else None)
        view = p.get("view", "gallery")
        total = len(images)
        index = _clamp_index(p.get("index", 0), total)
        current = (_single_view_image(p, images[index], dataset, meta)
                   if (view == "single" and total) else None)
        # Warm the browser cache for the NEXT image while this one is looked at: stepping on then
        # needs no download. Exactly one ahead —
        # that fits the one-at-a-time workflow and keeps the extra traffic to a single image.
        # It follows the panel's own sequence, so filter, collection or adopted source all apply,
        # and it goes through _single_view_image() so a CRP/CRAFT panel prefetches the variant it
        # will actually show (the concept selection survives stepping).
        preload_url = None
        if current and index + 1 < total:
            preload_url = _single_view_image(p, images[index + 1], dataset, meta)["full_url"]
        # Image source relationship (for subtle labelling): only when the source is actually
        # adopted (otherwise fallback to own filter → no label, see _effective_source_panel).
        source = _effective_source_panel(panels, p)
        source_pid = source["id"] if source else None
        # Collection source (snapshot): title for the badge; None if not collection-sourced or gone.
        source_col = (_collection(_image_source(p).get("id"))
                      if _image_source(p)["type"] == "collection" else None)
        # Displayed variant label. Singular while a *single* image is on screen ("Original-Bild"
        # instead of "Original-Bilder") — the plural is a statement about the gallery. Only the
        # translated "original" carries a number; XAI method names are shown verbatim. ``current``
        # is exactly "single view with an image", the state the templates render.
        xai_label = (t("single.original") if (current and p["xai_method"] == "original")
                     else xai_labels.get(p["xai_method"], p["xai_method"]))
        panel_views.append({
            **p,
            "view": view,
            "index": index,
            "total": total,
            "current": current,
            "xai_label": xai_label,
            "preload_url": preload_url,
            "images": images,
            "image_count": total,
            "is_modelcard": is_card,
            "modelcard": modelcard,
            "modelcard_meta": _model_metadata(meta, p["model"], p["level"]) if rendered and is_card else [],
            "modelcard_scope": _modelcard_scope_label(p) if is_card else "",
            # Followers do not use their own filters → suppress own filter summary
            # (the source is shown instead in the image-source badge).
            "filter_summary": [] if (source_pid or source_col or is_card) else _filter_summary(p["filters"]),
            "source_panel_id": source_pid,
            "source_collection_title": source_col["title"] if source_col else None,
            "follower_ids": [] if is_card else _panel_followers(panels, p["id"]),
        })
    visible = [p for p in panels if p["visible"]]
    all_linked = bool(visible) and all(p.get("linked") for p in visible)
    page_data = _build_page_data(panels, dataset)
    return render_template(
        "partials/_workspace.html",
        panels=panel_views,
        layout_scroll=session.get("layout_scroll", False),
        all_linked=all_linked,
        adopt_base_enabled=len(_adopt_base_panels(panels)) >= 2,
        model_labels=model_labels,
        level_labels=level_labels,
        page_data=page_data,
        panel_limit_reached=len(panels) >= MAX_PANELS,
        max_panels=MAX_PANELS,
        oob=oob,
    )


# ── routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def workspace():
    if "user" not in session:
        return redirect(url_for("login"))
    if "role" not in session:
        return redirect(url_for("role_select"))
    if "project" not in session:
        return redirect(url_for("project_select"))

    meta = load_metadata(_active_dataset())
    if not _get_panels():
        pid = _next_panel_id()
        _save_panels([_default_panel(pid, meta)])

    modal_html = ""
    if session.pop("open_panel1_config", False):
        panels = _get_panels()
        if panels:
            modal_html = render_template("partials/_config_modal.html",
                                         **_modal_context(panels[0], panels, meta))

    return render_template("workspace.html",
                           workspace_html=_render_workspace(),
                           modal_html=modal_html)


IMAGE_MAX_AGE = 30 * 24 * 3600  # 30 days; content is static (cache-buster via ?v=)

# What /data and /thumb may hand out. The dataset directory also holds metadata.json, CSVs and
# label files — none of which the image routes should serve. Restricting to image suffixes keeps
# those (all predictions, confidences, class lists) private and prevents a stray .svg/.html in a
# dataset from being served as active content in the app's own origin (with nosniff below).
SERVE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}


def _serve_image(path: Path):
    """send_file for a dataset image, with nosniff so the declared type is not second-guessed."""
    resp = send_file(path, max_age=IMAGE_MAX_AGE)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    return resp


def _safe_dataset_base(dataset: str):
    """Resolved directory of an *offered* dataset within DATASETS_ROOT, else None.

    The membership check against available_datasets() is the authorisation, not just a nicety:
    it rejects datasets that the UI never offers (present under DATASETS_ROOT but without a
    metadata.json) and, because "." is never an offered name, also blocks dataset="." from
    addressing DATASETS_ROOT itself.
    """
    if dataset not in available_datasets():
        return None
    root = DATASETS_ROOT.resolve()
    base = (root / dataset).resolve()
    if base != root and not str(base).startswith(str(root) + os.sep):
        return None
    return base


def _dataset_path(base: Path, relpath: str):
    """Absolute path for a dataset-relative request path – or None if it escapes the dataset.

    ``base`` must already be resolved (as _safe_dataset_base() returns it) – otherwise a symlink
    anywhere *above* the dataset (a symlinked mount point, /tmp on macOS) makes every path look
    like an escape.

    Normally the check runs on the *resolved* path, so not even a symlink inside the dataset can
    lead out of it. FOLLOW_DATASET_SYMLINKS relaxes it to a lexical check: a development machine
    may stitch a dataset together from symlinks into a mounted drive (renderings that are too
    large to copy), and those would otherwise answer 403. Traversal via the URL stays blocked
    either way – `normpath` collapses ".." before the comparison.
    """
    if "\x00" in relpath:
        # resolve() raises ValueError on an embedded null byte → an unhandled 500. Treat it as
        # what it is: not a valid path inside the dataset (S5).
        return None
    candidate = base / relpath
    target = Path(os.path.normpath(candidate)) if FOLLOW_DATASET_SYMLINKS else candidate.resolve()
    return target if str(target).startswith(str(base) + os.sep) else None


@app.route("/data/<dataset>/<path:relpath>")
def dataset_image(dataset, relpath):
    """Serves a full-resolution image from the dataset (with path-traversal protection)."""
    if "project" not in session:
        return "", 403
    base = _safe_dataset_base(dataset)
    if base is None:
        return "", 403
    target = _dataset_path(base, relpath)
    if target is None:
        return "", 403
    if target.suffix.lower() not in SERVE_IMAGE_SUFFIXES:
        return "", 404
    if not target.is_file():
        return "", 404
    return _serve_image(target)


@app.route("/thumb/<dataset>/<path:relpath>")
def thumbnail(dataset, relpath):
    """Serves a gallery thumbnail; generates it lazily under <dataset>/thumbs/ if needed."""
    if "project" not in session:
        return "", 403
    base = _safe_dataset_base(dataset)
    if base is None:
        return "", 403
    src = _dataset_path(base, relpath)
    if src is None or not src.is_file():
        return "", 404
    if src.suffix.lower() not in SERVE_IMAGE_SUFFIXES:
        return "", 404
    # Build the *write* path from the validated source, never from the raw relpath: a relpath
    # that passes the read check via ".." re-descent would otherwise steer the thumbnail (which
    # carries an extra "thumbs/" prefix) to a mkdir()+write outside the dataset. src is already
    # guaranteed to sit inside base, so relative_to() cannot escape.
    thumb = base / thumb_rel(str(src.relative_to(base)))
    # Regenerate when missing OR stale (source newer than the cached thumbnail): XAI assets
    # get re-exported in place, so an existence check alone would keep serving the old crop.
    if not thumb.exists() or src.stat().st_mtime > thumb.stat().st_mtime:
        try:
            generate_thumbnail(src, thumb)
        except OSError:
            return _serve_image(src)  # fallback: original
    return _serve_image(thumb)


# ── legal pages ───────────────────────────────────────────────────────────────

# Reachable without a session on purpose: an imprint that only exists behind a login is worth
# nothing, and a visitor deciding whether to enter is exactly who needs the privacy policy.

@app.context_processor
def legal_context():
    """``legal``: the operator texts in the session language — the footer's contact link and the
    login's "request an account" link need them on every page, not only on the legal ones."""
    return {"legal": legal.for_lang(LEGAL, get_lang())}


@app.route("/imprint")
def imprint():
    section = legal.for_lang(LEGAL, get_lang()).get("imprint", {})
    return render_template("imprint.html", imprint=section, draft=bool(legal.placeholders(section)))


@app.route("/privacy")
def privacy():
    section = legal.for_lang(LEGAL, get_lang()).get("privacy", {})
    return render_template("privacy.html", privacy=section, draft=bool(legal.placeholders(section)),
                           guest_ttl_hours=GUEST_TTL_HOURS)


# ── documentation ─────────────────────────────────────────────────────────────

# Public like the legal pages: a visitor who does not yet know what the tool is, is exactly the
# reader of these pages. /docs/overview (the help button) is the usage guide, which opens with the
# overview figure (pre-rendered per language by tools/make_overview_figure.py). The guides are the
# Markdown files in docs/, rendered by docs_render.py.

@app.route("/docs/<page>")
def docs_page(page):
    if page not in docs_render.PAGES:
        return "", 404
    pattern = url_for("docs_page", page="PAGE").replace("PAGE", "{page}")
    title, html = docs_render.render(page, get_lang(), url_for("static", filename=""), pattern)
    return render_template("docs_page.html", doc_title=title, doc_html=html)


# ── setup flow ────────────────────────────────────────────────────────────────

def _login_page(status: int = 200, **kw):
    """The login page with the context both entrances need (guest TTL, which form to blame)."""
    kw.setdefault("form", "guest")
    return render_template("login.html", demo=DEMO_MODE, guest_ttl_hours=GUEST_TTL_HOURS, **kw), status


@app.route("/login", methods=["GET", "POST"])
def login():
    """Entry point for both kinds of visitor (see auth.py).

    The page offers two separated entrances, and the hidden ``mode`` field says which one was
    used: the guest form has no password at all, the account form requires one. Requests
    without ``mode`` (older clients, tests) keep the previous single-form contract, where an
    empty password meant "guest".

    Which owner key a guest gets depends on the mode: a throwaway sandbox in the demo, the typed
    name on an internal instance. A name that belongs to an account always requires its password
    — otherwise a guest could walk into an account's data simply by typing its name.
    """
    if request.method == "POST":
        mode = request.form.get("mode", "")
        username = request.form.get("username", "").strip()
        # The guest form carries no password field; ignore one even if it is smuggled in, so the
        # section a visitor used decides how the request is read.
        password = "" if mode == "guest" else request.form.get("password", "")
        form = "account" if mode == "account" else "guest"
        if not username:
            return _login_page(form=form)
        if password or ACCOUNTS.exists(username):
            # The limit guards password *guessing*, so only the password path is counted: a
            # guest login has no secret to guess, and counting it would let ordinary demo
            # traffic lock the entrance for everyone sharing an address behind a proxy.
            if not LOGIN_LIMITER.allow(request.remote_addr or "?"):
                return _login_page(429, form=form, error=t("login.rate_limited"),
                                   username=username)
            if not ACCOUNTS.verify(username, password):
                # A guest who picked an account's name gets told what to do instead of a
                # "wrong password" for a field their form does not even have.
                error = t("login.guest_name_taken") if mode == "guest" else t("login.failed")
                return _login_page(401, form=form, error=error, username=username)
            LOGIN_LIMITER.reset(request.remote_addr or "?")
            LOGIN_LIMITER.prune()
            session["owner"] = account_owner(username)
            session["user"] = username.strip().lower()
        else:
            session["owner"] = new_guest_owner() if DEMO_MODE else account_owner(username)
            session["user"] = username
        return redirect(url_for("role_select"))
    return _login_page()


# The roles of the usage concept that the application actually implements. The others appear
# on the selection page as disabled cards. What is built — comparing models and their
# explanations, the requirements catalogue, the audit trail — turned out to fit *validation*
# better than an expert opinion, so that is the live role (2026-09-02).
ACTIVE_ROLES = ("validierer",)

# Project type ("test" = free exploration, "pruefung" = formal audit). The choice confused the
# first testers, so the selection is hidden for now and every new project silently becomes a
# free-exploration one (2026-09-09). The mechanism itself is untouched — flipping this flag back
# to True restores the radio buttons on the project page and the type badge in the header.
ALLOW_PROJECT_TYPE_SELECTION = False
DEFAULT_PROJECT_TYPE = "test"

app.jinja_env.globals.update(allow_project_type_selection=ALLOW_PROJECT_TYPE_SELECTION)


@app.route("/role", methods=["GET", "POST"])
def role_select():
    if "user" not in session:
        return redirect(url_for("login"))
    if request.method == "POST":
        role = request.form.get("role", "")
        if role in ACTIVE_ROLES:
            session["role"] = role
            return redirect(url_for("project_select"))
    return render_template("role_select.html", active_roles=ACTIVE_ROLES)


@app.route("/project", methods=["GET", "POST"])
def project_select():
    if "user" not in session:
        return redirect(url_for("login"))
    if "role" not in session:
        return redirect(url_for("role_select"))
    if request.method == "POST":
        name = (request.form.get("name", "").strip()
                or f"{t('project.default_name_prefix')} {date.today()}")
        # With the selection hidden the form carries no type at all; do not trust a posted one
        # either, so the flag alone decides what a new project becomes.
        project_type = (request.form.get("type", DEFAULT_PROJECT_TYPE)
                        if ALLOW_PROJECT_TYPE_SELECTION else DEFAULT_PROJECT_TYPE)
        # Whitelist against the scanned datasets: the raw form value would otherwise reach
        # _dataset_dir() as a path fragment. An empty/unknown value falls back to the config.
        requested = request.form.get("dataset", "").strip()
        dataset = requested if requested in available_datasets() else DEFAULT_DATASET
        session["project"] = {
            "name": name,
            "type": project_type,
            "dataset": dataset,
            "created_at": date.today().isoformat(),
        }
        _seed_demo_notes(dataset, name)
        session.pop("panels", None)
        session.pop("next_panel_id", None)
        session["open_panel1_config"] = True
        return redirect(url_for("workspace"))
    return render_template("project_select.html",
                           default_name=f"{t('project.default_name_prefix')} {date.today()}",
                           datasets=available_datasets(),
                           default_dataset=DEFAULT_DATASET)


@app.route("/lang/<code>")
def set_lang(code):
    """Switch the UI language (session) and redirect back to the calling page. Idempotent GET link;
    server-side rendering then automatically uses the new language."""
    if code in LANGUAGES:
        session["lang"] = code
        session.modified = True
    # Bounce back to the calling page — but only if it is same-origin. Otherwise this GET turns
    # the trusted domain into an open redirector: a foreign page linking to /lang/de would send
    # the visitor right back to itself. A relative referrer has an empty netloc and is fine.
    ref = request.referrer or ""
    if ref and urlparse(ref).netloc.lower() != request.host.lower():
        ref = ""
    return redirect(ref or url_for("workspace"))


@app.route("/logout", methods=["POST"])
def logout():
    # An armed tracking mode outlives the logout on purpose: whoever runs a usability session
    # arms it once and then hands the browser over, and a participant switching accounts must
    # not silently end the recording. It is switched off by the shortcut, not by leaving.
    armed = {key: session[key] for key in ("tracking", "tracking_id") if key in session}
    session.clear()
    session.update(armed)
    return redirect(url_for("login"))


@app.route("/panel/new", methods=["POST"])
def panel_new():
    meta = load_metadata(_active_dataset())
    panels = _get_panels()
    if len(panels) >= MAX_PANELS:
        # At the cap: refuse silently (the button is already disabled in the UI, this guards
        # against a direct POST) and just re-render the unchanged workspace, no modal.
        return _render_workspace()
    pid = _next_panel_id()
    new_panel = _default_panel(pid, meta)
    panels.append(new_panel)
    _save_panels(panels)
    modal_html = render_template("partials/_config_modal.html",
                                 **_modal_context(new_panel, panels, meta))
    return _render_workspace() + f'<div id="modal-container" hx-swap-oob="true">{modal_html}</div>'


@app.route("/panel/<int:pid>/duplicate", methods=["POST"])
def panel_duplicate(pid):
    panels = _get_panels()
    src = _find_panel(panels, pid)
    if src and len(panels) < MAX_PANELS:
        new_pid = _next_panel_id()
        clone = json.loads(json.dumps(src))  # deep copy
        clone["id"] = new_pid
        panels.insert(panels.index(src) + 1, clone)
        _save_panels(panels)
    return _render_workspace()


@app.route("/panel/<int:pid>/delete", methods=["POST"])
def panel_delete(pid):
    panels = [p for p in _get_panels() if p["id"] != pid]
    _save_panels(panels)
    return _render_workspace()


@app.route("/panel/<int:pid>/toggle", methods=["POST"])
def panel_toggle(pid):
    panels = _get_panels()
    panel = _find_panel(panels, pid)
    if panel:
        panel["visible"] = not panel["visible"]
        _save_panels(panels)
    return _render_workspace()


@app.route("/panel/<int:pid>/link", methods=["POST"])
def panel_link(pid):
    """Add or remove a panel from the coupling group. The button + data-linked toggle is handled
    by the frontend itself (island, no gallery render). Via OOB we only push the sync control
    and page_data – so the sync buttons appear immediately when linked panels are in single-image
    view (and the arrow-key gate stays current)."""
    panels = _get_panels()
    panel = _find_panel(panels, pid)
    if panel:
        panel["linked"] = not panel.get("linked", False)
        _save_panels(panels)
    page_data = _build_page_data(panels, _active_dataset())
    return (render_template("partials/_sync_control.html", panels=panels, oob=True)
            + render_template("partials/_page_data.html", page_data=page_data, oob=True))


@app.route("/layout/toggle-scroll", methods=["POST"])
def layout_toggle_scroll():
    session["layout_scroll"] = not session.get("layout_scroll", False)
    session.modified = True
    return _render_workspace()


# ── single-image view + (provisional) synchronized stepping ───────────────────

_STEP = {"prev": -1, "next": 1}


@app.route("/panel/<int:pid>/open/<int:index>", methods=["POST"])
def panel_open(pid, index):
    """Click a gallery image → single-image view at position <index>. For a linked panel,
    applied to all linked panels (index clamped to the valid range per panel, F2/a1)."""
    panels = _get_panels()
    panel = _find_panel(panels, pid)
    if panel:
        dataset = _active_dataset()
        meta = load_metadata(dataset)
        for t in _coupled_targets(panels, panel):
            t["view"] = "single"
            t["index"] = _clamp_index(index, len(_panel_images(t, panels, dataset, meta)))
            t["crp_rank"], t["crp_view"] = 1, "heatmap"  # fresh image → most relevant concept, heatmap
            t["craft_concept"] = None                     # CRAFT → back to the attribution map
        _save_panels(panels)
    return _render_workspace()


@app.route("/panel/<int:pid>/gallery", methods=["POST"])
def panel_gallery(pid):
    """Return to gallery view – for a linked panel, applied to all linked panels."""
    panels = _get_panels()
    panel = _find_panel(panels, pid)
    if panel:
        for t in _coupled_targets(panels, panel):
            t["view"] = "gallery"
        _save_panels(panels)
    return _render_workspace()


@app.route("/panel/<int:pid>/crp-select", methods=["POST"])
def panel_crp_select(pid):
    """CRP single view: pick the shown concept (by rank) and/or image type (heatmap|grid).
    Panel-local on purpose – concept choice is per image and does not propagate to linked panels."""
    panels = _get_panels()
    panel = _find_panel(panels, pid)
    if panel:
        if "crp_rank" in request.form:
            try:
                panel["crp_rank"] = int(request.form["crp_rank"])
            except ValueError:
                pass
        if request.form.get("crp_view") in ("heatmap", "grid"):
            panel["crp_view"] = request.form["crp_view"]
        _save_panels(panels)
    return _render_workspace()


@app.route("/panel/<int:pid>/craft-select", methods=["POST"])
def panel_craft_select(pid):
    """CRAFT single view: show the per-image attribution map ("map") or a global concept prototype
    (its id). Panel-local – the map is per image, the concepts are model-global."""
    panels = _get_panels()
    panel = _find_panel(panels, pid)
    if panel:
        raw = request.form.get("craft_concept", "map")
        try:
            panel["craft_concept"] = None if raw == "map" else int(raw)
        except ValueError:
            panel["craft_concept"] = None
        _save_panels(panels)
    return _render_workspace()


@app.route("/panels/link-all", methods=["POST"])
def panels_link_all():
    """Couple all panels, or uncouple them if all visible panels are already coupled."""
    panels = _get_panels()
    visible = [p for p in panels if p.get("visible")]
    new_state = not (visible and all(p.get("linked") for p in visible))
    for p in panels:
        p["linked"] = new_state
    _save_panels(panels)
    return _render_workspace()


def _adopt_base_panels(panels: list) -> list:
    """Panels affected by "adopt base images", in display order: visible image panels (hidden and
    modelcard panels are left out). The first one is the reference – the leftmost, which is not
    necessarily the lowest id (duplicates are inserted next to their original)."""
    return [p for p in panels if p.get("visible") and p.get("mode") != MODE_MODELCARD]


@app.route("/panels/adopt-base", methods=["POST"])
def panels_adopt_base():
    """All affected panels take model + level and the base image set of the leftmost one.

    Model/level are applied as if set in the dialog (unavailable XAI method → original). The common
    image source is the reference's DECLARED source: own filter → follow the reference; collection
    or panel X → that same collection / panel X, so no chain forms (a target that IS panel X stays
    the filter-based root). Own filters are kept for switching back later. Finally, any panel still
    pointing at a no-longer-filter-based panel (e.g. a hidden follower of a target) is redirected
    to the common source – otherwise the no-chaining invariant would break."""
    panels = _get_panels()
    affected = _adopt_base_panels(panels)
    if len(affected) < 2:
        return _render_workspace()
    meta = load_metadata(_active_dataset())
    ref, targets = affected[0], affected[1:]
    ref_src = _image_source(ref)
    common = {"type": "panel", "id": ref["id"]} if ref_src["type"] == "filter" else dict(ref_src)

    def source_for(p):
        is_root = common["type"] == "panel" and common["id"] == p["id"]
        return {"type": "filter"} if is_root else dict(common)

    for p in targets:
        p["model"] = ref["model"]
        p["level"] = ref["level"]
        available = set(meta["xai_availability"].get(p["model"], {}))
        if p["xai_method"] != "original" and p["xai_method"] not in available:
            p["xai_method"] = "original"
        p["image_source"] = source_for(p)
        p["configured"] = True

    for p in panels:
        src = _image_source(p)
        if src["type"] != "panel":
            continue
        source = _find_panel(panels, src.get("id"))
        if source is not None and _image_source(source)["type"] != "filter":
            p["image_source"] = source_for(p)
    _save_panels(panels)
    return _render_workspace()


def _linked_single_panels(panels):
    """The panels that step together: linked, visible, in single-image view (no model cards)."""
    return [p for p in panels
            if p.get("visible") and p.get("view") == "single" and p.get("linked")
            and p.get("mode") != MODE_MODELCARD]


def _step_panels(targets, panels, delta, dataset, meta):
    """Advance each target panel by ``delta`` images (clamped to its own image list).

    CRP rank/view and CRAFT concept are kept across stepping on purpose (comparison across
    images); only opening a gallery image resets them.
    """
    for panel in targets:
        total = len(_panel_images(panel, panels, dataset, meta))
        panel["index"] = _clamp_index(panel.get("index", 0) + delta, total)


@app.route("/panel/<int:pid>/step/<direction>", methods=["POST"])
def panel_step(pid, direction):
    """Step forward or backward in single-image view — for a linked panel: all coupled ones.

    A panel's own arrows are not a private control: while the panel is linked they do exactly
    what the workspace's sync control (/step-all) does, because coupling means the panels move
    together no matter which arrow is pressed.
    """
    delta = _STEP.get(direction)
    panels = _get_panels()
    panel = _find_panel(panels, pid)
    if panel and delta is not None:
        dataset = _active_dataset()
        meta = load_metadata(dataset)
        targets = _linked_single_panels(panels) if panel.get("linked") else []
        if panel not in targets:
            targets.append(panel)
        _step_panels(targets, panels, delta, dataset, meta)
        _save_panels(panels)
    return _render_workspace()


@app.route("/step-all/<direction>", methods=["POST"])
def step_all(direction):
    """Coupling: advance all linked, visible single-image panels synchronously."""
    delta = _STEP.get(direction)
    if delta is not None:
        dataset = _active_dataset()
        meta = load_metadata(dataset)
        panels = _get_panels()
        _step_panels(_linked_single_panels(panels), panels, delta, dataset, meta)
        _save_panels(panels)
    return _render_workspace()


@app.route("/panel/<int:pid>/config", methods=["GET"])
def panel_config(pid):
    panels = _get_panels()
    panel = _find_panel(panels, pid)
    if panel is None:
        return "", 404
    meta = load_metadata(_active_dataset())
    return render_template("partials/_config_modal.html", **_modal_context(panel, panels, meta))


def _percent(value: float):
    """Normalises a percentage: 0.0 → 0, 99.9 → 99.9. Keeps the dialog's number fields free of
    pointless decimals when they are re-rendered from the stored filter."""
    number = round(float(value), 1)
    return int(number) if number.is_integer() else number


def _filters_from_form(form) -> dict:
    """Builds a filter configuration from the (unsaved) dialog form."""
    categories = _categories()
    filters = empty_filter_config(categories)
    for key in categories:
        filters[key] = form.getlist(f"filter_{key}")
    for key, spec in DISTANCE_FILTERS.items():
        try:
            lo = float(form.get(f"{key}_min", spec["min"]))
            hi = float(form.get(f"{key}_max", spec["max"]))
        except ValueError:
            lo, hi = spec["min"], spec["max"]
        filters[key] = [max(spec["min"], lo), min(spec["max"], hi)]
    klass = form.get(CLASSIFICATION_FILTER["key"], "")
    if klass in CLASSIFICATION_FILTER["options"]:
        filters[CLASSIFICATION_FILTER["key"]] = klass
    # Confidence range. The inputs are disabled while the checkbox is off, so an inactive filter
    # submits no values at all → the defaults (full range) apply.
    filters[CONFIDENCE_FILTER["active_key"]] = bool(form.get(CONFIDENCE_FILTER["active_key"]))
    try:
        lo = float(form.get(f"{CONFIDENCE_FILTER['key']}_min", CONFIDENCE_FILTER["min"]))
        hi = float(form.get(f"{CONFIDENCE_FILTER['key']}_max", CONFIDENCE_FILTER["max"]))
    except ValueError:
        lo, hi = CONFIDENCE_FILTER["min"], CONFIDENCE_FILTER["max"]
    filters[CONFIDENCE_FILTER["key"]] = [_percent(max(CONFIDENCE_FILTER["min"], lo)),
                                         _percent(min(CONFIDENCE_FILTER["max"], hi))]
    return filters


def _modelcard_selection_from_form(form):
    """Parses the modelcard radio group ('' = all images, '<key>:<value>' = exactly one
    category option). Unknown key/value → None (= all images)."""
    key, _, value = form.get("modelcard_filter", "").partition(":")
    categories = _categories()
    if key in categories and value in categories[key]["options"]:
        return {"key": key, "value": value}
    return None


def _image_source_from_form(form, pid: int, panels: list) -> dict:
    """Parses the image source from the dialog ('filter', 'panel:<id>' or 'collection:<id>').

    Panel source is validated defensively: it must exist, must not be the panel itself, must be
    filter-based (no chaining), and the panel must have no followers of its own (else a chain
    would form). Collection source is accepted whenever the collection exists. On violation →
    own filter."""
    raw = form.get("image_source", "filter")
    if raw.startswith("collection:"):
        try:
            cid = int(raw.split(":", 1)[1])
        except ValueError:
            return {"type": "filter"}
        return {"type": "collection", "id": cid} if _collection(cid) else {"type": "filter"}
    if raw.startswith("panel:") and not _panel_followers(panels, pid):
        try:
            sid = int(raw.split(":", 1)[1])
        except ValueError:
            return {"type": "filter"}
        source = _find_panel(panels, sid)
        if source is not None and sid != pid and _image_source(source)["type"] == "filter":
            return {"type": "panel", "id": sid}
    return {"type": "filter"}


@app.route("/panel/<int:pid>/filter-counts", methods=["POST"])
def panel_filter_counts(pid):
    """Live (debounced): faceted counts for the current dialog state – OOB spans only."""
    panel = _find_panel(_get_panels(), pid)
    if panel is None:
        return "", 404
    meta = load_metadata(_active_dataset())
    model = request.form.get("model", panel["model"])
    level = request.form.get("level", panel["level"])
    if model not in meta["models"]:
        model = panel["model"]
    if level not in meta["levels"]:
        level = panel["level"]
    counts = _faceted_counts(meta, model, level, _filters_from_form(request.form))
    categories, classification, _, _ = _localized_filter_meta()
    # Both sections are refreshed regardless of the selected mode: the numbers are cheap and
    # the hidden section's OOB spans simply stay up to date (model/level drive the accuracies).
    option_accuracy = _modelcard_stats(meta, model, level, None)["option_accuracy"]
    return render_template("partials/_filter_counts.html", counts=counts,
                           filter_categories=categories,
                           classification_filter=classification,
                           modelcard_accuracy=option_accuracy)


@app.route("/panel/<int:pid>/config", methods=["POST"])
def panel_config_apply(pid):
    panels = _get_panels()
    panel = _find_panel(panels, pid)
    if panel is None:
        return "", 404

    meta = load_metadata(_active_dataset())
    model = request.form.get("model", panel["model"])
    level = request.form.get("level", panel["level"])
    method = request.form.get("xai_method", panel["xai_method"])

    if model in meta["models"]:
        panel["model"] = model
    if level in meta["levels"]:
        panel["level"] = level

    # The display dropdown carries the XAI method and the mode (see MODELCARD_OPTION).
    # In modelcard mode xai_method keeps its value, so switching back restores the rendering.
    if method == MODELCARD_OPTION:
        panel["mode"] = MODE_MODELCARD
        panel["modelcard_filter"] = _modelcard_selection_from_form(request.form)
    else:
        panel["mode"] = MODE_IMAGES
        available = set(meta["xai_availability"].get(panel["model"], {}))
        panel["xai_method"] = method if (method == "original" or method in available) else "original"

        source = _image_source_from_form(request.form, pid, panels)
        panel["image_source"] = source
        # Apply own filters from the form only when type=filter. If the panel follows a foreign
        # source, the existing filters are kept (for switching back later) –
        # the filter section is then disabled in the dialog and sends nothing anyway.
        if source["type"] == "filter":
            panel["filters"] = _filters_from_form(request.form)
    panel["configured"] = True
    _save_panels(panels)

    # re-render workspace + close modal via OOB swap
    return _render_workspace() + '<div id="modal-container" hx-swap-oob="true"></div>'


# ── collections ───────────────────────────────────────────────

@app.route("/collection/from-matrix", methods=["POST"])
def collection_from_matrix():
    """Creates a collection from the ticked confusion-matrix cells of a modelcard panel.

    Form: panel_id, cell=<true>:<predicted> (repeated), optional title, optional open_new_panel.
    On success re-renders the workspace (a new collection-sourced panel is inserted after the
    matrix panel when requested). No cells / no images → 400 (button is guarded client-side)."""
    if "project" not in session:
        return "", 403
    panels = _get_panels()
    panel = _find_panel(panels, request.form.get("panel_id", type=int))
    if panel is None or panel.get("mode") != MODE_MODELCARD:
        return "", 404

    dataset = _active_dataset()
    meta = load_metadata(dataset)
    model, level = panel["model"], panel["level"]
    selection = _modelcard_selection(panel)
    cells = _parse_matrix_cells(request.form, list(meta["class_labels"]))
    if not cells:
        return "", 400
    image_ids = _matrix_cell_images(meta, model, level, selection, cells)
    if not image_ids:
        return "", 400
    if _demo_limit_reached("collections"):
        return "", 429

    title = request.form.get("title", "").strip()
    if not title:
        title = _auto_collection_title(model, level, len(cells),
                                       _collection_titles(dataset))
    collection = COLLECTIONS_STORE.create(
        title=title,
        image_ids=image_ids,
        owner=_owner(),
        user=session.get("user", "?"),
        project=session.get("project", {}).get("name", "?"),
        dataset=dataset,
        origin={"kind": "confmatrix", "model": model, "level": level,
                "selection": ({"key": selection[0], "value": selection[1]} if selection else None),
                "cells": sorted(list(cells))},
    )

    if request.form.get("open_new_panel"):
        new_panel = _default_panel(_next_panel_id(), meta)
        new_panel.update({
            "model": model, "level": level, "xai_method": "original", "mode": MODE_IMAGES,
            "image_source": {"type": "collection", "id": collection["id"]},
            "configured": True,
        })
        panels.insert(panels.index(panel) + 1, new_panel)
        _save_panels(panels)

    return _render_workspace()


# ── collection management ─────────────────────────────────────────────────────
#
# Freeze rule (Entscheidung 2026-07-29): a collection that a note references explicitly is never
# modified in place. Destructive operations (add images, remove an image) clone it first and edit
# the clone; deleting it is refused outright. Rationale: a note — especially an audit-report entry
# — must keep meaning what it meant when it was written, and a collection is a snapshot precisely
# so that stays true. Everything about the rule is made visible in the dialog *before* acting
# (lock marker + "creates a copy" button labels) and reported *after* acting (notice).

def _collection_note_refs(cid: int) -> list:
    """Notes referencing collection <cid> EXPLICITLY (refpoint type "collection") — these freeze it."""
    return [n for n in _notes(dataset=_active_dataset())
            if any(rp.get("type") == "collection" and rp.get("id") == cid
                   for rp in n.get("refpoints", []))]


def _collection_snapshot_refs(cid: int) -> list:
    """Notes whose WORKSPACE snapshot merely contains a panel sourced from collection <cid>.

    Shown for transparency but deliberately NOT freezing (Q5a): such a mention records a panel
    layout, not a statement about the image set — and since a snapshot captures *all* panels,
    freezing on it would freeze nearly every collection the user ever looked at."""
    hits = []
    for note in _notes(dataset=_active_dataset()):
        for rp in note.get("refpoints", []):
            if rp.get("type") != "workspace":
                continue
            if any((ps.get("image_source") or {}).get("type") == "collection"
                   and (ps.get("image_source") or {}).get("id") == cid
                   for ps in rp.get("panels", [])):
                hits.append(note)
                break
    return hits


def _note_ref_labels(notes: list) -> list:
    """Compact display of referencing notes: [{id, title}] with a fallback to the note text."""
    return [{"id": n["id"], "title": n.get("title") or (n.get("text") or "")[:40] or f"#{n['id']}"}
            for n in notes]


def _clone_title(title: str, existing: set) -> str:
    """"X" → "X (Kopie)", then "X (Kopie) 2", … — never collides with an existing title."""
    base = f"{title} ({t('collection.copy_suffix')})"[:TITLE_MAX]
    candidate, n = base, 1
    while candidate in existing:
        n += 1
        candidate = f"{base} {n}"
    return candidate[:TITLE_MAX]


def _collection_edit_target(cid: int):
    """The collection a destructive edit must actually be applied to.

    Returns ``(collection, clone_of)``: for a frozen (note-referenced) collection the returned
    collection is a fresh clone and ``clone_of`` is the untouched original; otherwise the
    collection itself and None. ``(None, None)`` when the id is unknown."""
    collection = _collection(cid)
    if collection is None:
        return None, None
    if not _collection_note_refs(cid):
        return collection, None
    clone = COLLECTIONS_STORE.clone(
        cid, _owner(), _clone_title(collection["title"], _collection_titles(_active_dataset())))
    return clone, collection


def _collection_origin_descr(collection: dict) -> str:
    """One-line provenance ("how was this set captured"), empty for unknown/absent origins."""
    origin = collection.get("origin") or {}
    kind = origin.get("kind")
    if kind == "confmatrix":
        cells = len(origin.get("cells") or [])
        return t("collection.origin_confmatrix", model=origin.get("model", "?"),
                 level=origin.get("level", "?"), cells=cells)
    if kind == "clone":
        return t("collection.origin_clone", title=origin.get("source_title", "?"))
    return ""


def _collection_views() -> list:
    """Display data for the management dialog: one entry per collection of the active dataset,
    including its images (thumbnail URLs; ids gone from the dataset are marked) and the
    reference situation that drives the freeze rule."""
    dataset = _active_dataset()
    meta = load_metadata(dataset)
    by_id = {img["id"]: img for img in meta["images"]}
    views = []
    for collection in _collections(dataset):
        images = []
        for iid in collection["image_ids"]:
            img = by_id.get(iid)
            images.append({
                "id": iid,
                "missing": img is None,
                "url": (url_for("thumbnail", dataset=dataset,
                                relpath=f"originals/imgs/{img['filename']}", v=THUMB_TAG)
                        if img else None),
            })
        note_refs = _collection_note_refs(collection["id"])
        views.append({
            **collection,
            "count": len(collection["image_ids"]),
            "images": images,
            "origin_descr": _collection_origin_descr(collection),
            "frozen": bool(note_refs),
            "note_refs": _note_ref_labels(note_refs),
            "snapshot_refs": _note_ref_labels(_collection_snapshot_refs(collection["id"])),
        })
    return views


def _render_collections_body(notice: dict = None, workspace_changed: bool = False,
                             open_cid: int = None) -> str:
    """The dialog body (swapped into #collections-body). ``workspace_changed`` additionally
    refreshes the panels out-of-band — a panel showing an edited/deleted collection would
    otherwise keep displaying a stale image set behind the dialog. ``open_cid`` keeps that
    collection's image list unfolded: the swap replaces the <details> element, and its open
    state lives in the DOM only, so without this every action would collapse it."""
    html = render_template("partials/_collections_body.html",
                           collections=_collection_views(), notice=notice, open_cid=open_cid)
    if workspace_changed:
        html += _render_workspace(oob=True)
    return html


def _notice(kind: str, key: str, **kwargs) -> dict:
    return {"kind": kind, "text": t(key, **kwargs)}


def _frozen_notice(clone: dict, original: dict) -> dict:
    """Reports what the freeze rule did: original untouched, change landed in a new copy."""
    refs = ", ".join(f"#{n['id']} {n['title']}" for n in _note_ref_labels(
        _collection_note_refs(original["id"])))
    return _notice("warning", "collection.frozen_cloned",
                   original=original["title"], copy=clone["title"], notes=refs)


@app.route("/collections", methods=["GET"])
def collections_dialog():
    """Management dialog (list + rename/clone/merge/remove/delete), opened from the nav bar."""
    if "project" not in session:
        return "", 403
    return render_template("partials/_collections_modal.html",
                           collections=_collection_views(), notice=None, open_cid=None)


@app.route("/collections/<int:cid>/rename", methods=["POST"])
def collection_rename(cid):
    """Renames a collection. Non-destructive → allowed even while frozen (see freeze rule)."""
    if "project" not in session:
        return "", 403
    title = request.form.get("title", "").strip()
    if not title:
        return _render_collections_body(_notice("danger", "collection.title_required"))
    collection = COLLECTIONS_STORE.rename(cid, _owner(), title)
    if collection is None:
        return _render_collections_body(_notice("danger", "collection.gone"))
    # The title is shown on collection-sourced panels (source badge) → refresh the workspace.
    return _render_collections_body(
        _notice("success", "collection.renamed", title=collection["title"]), workspace_changed=True)


@app.route("/collections/<int:cid>/clone", methods=["POST"])
def collection_clone(cid):
    if "project" not in session:
        return "", 403
    source = _collection(cid)
    if source is None:
        return _render_collections_body(_notice("danger", "collection.gone"))
    clone = COLLECTIONS_STORE.clone(
        cid, _owner(), _clone_title(source["title"], _collection_titles(_active_dataset())))
    return _render_collections_body(
        _notice("success", "collection.cloned", title=clone["title"]))


@app.route("/collections/<int:cid>/merge-into", methods=["POST"])
def collection_merge_into(cid):
    """Adds the images of collection <cid> to another one (the target is what changes, so the
    freeze rule applies to the TARGET). The source stays untouched either way."""
    if "project" not in session:
        return "", 403
    source = _collection(cid)
    target_id = request.form.get("target", type=int)
    if source is None or target_id is None or target_id == cid:
        return _render_collections_body(_notice("danger", "collection.merge_invalid"))
    target, frozen_original = _collection_edit_target(target_id)
    if target is None:
        return _render_collections_body(_notice("danger", "collection.gone"))
    before = len(target["image_ids"])
    target = COLLECTIONS_STORE.add_images(target["id"], _owner(), source["image_ids"])
    added = len(target["image_ids"]) - before
    if frozen_original is not None:
        return _render_collections_body(_frozen_notice(target, frozen_original),
                                        workspace_changed=True)
    return _render_collections_body(
        _notice("success", "collection.merged", source=source["title"],
                target=target["title"], added=added), workspace_changed=True)


@app.route("/collections/<int:cid>/remove-images", methods=["POST"])
def collection_remove_images(cid):
    """Removes the ticked images from a collection (destructive → freeze rule applies).

    Deliberately a batch: one selection = one operation = at most ONE clone of a frozen
    collection. The overlap is checked against the original *before* cloning, so a selection
    that matches nothing never leaves a pointless copy behind."""
    if "project" not in session:
        return "", 403
    source = _collection(cid)
    if source is None:
        return _render_collections_body(_notice("danger", "collection.gone"))
    image_ids = request.form.getlist("image_id")
    if not image_ids:
        return _render_collections_body(_notice("danger", "collection.no_selection"), open_cid=cid)
    removed = len(set(image_ids) & set(source["image_ids"]))
    if not removed:
        return _render_collections_body(_notice("danger", "collection.image_not_in_collection"),
                                        open_cid=cid)
    collection, frozen_original = _collection_edit_target(cid)
    result = COLLECTIONS_STORE.remove_images(collection["id"], _owner(), image_ids)
    if frozen_original is not None:
        # The user's work went into the copy → unfold THAT one, not the untouched original.
        return _render_collections_body(_frozen_notice(result, frozen_original),
                                        workspace_changed=True, open_cid=result["id"])
    return _render_collections_body(
        {"kind": "success",
         "text": nt(removed, "collection.images_removed", title=result["title"])},
        workspace_changed=True, open_cid=result["id"])


@app.route("/collections/<int:cid>/delete", methods=["POST"])
def collection_delete(cid):
    """Deletes a collection — refused while a note references it (Q4a: no silent dangling
    references; the dialog lists the notes so the user can resolve them first)."""
    if "project" not in session:
        return "", 403
    collection = _collection(cid)
    if collection is None:
        return _render_collections_body(_notice("danger", "collection.gone"))
    refs = _collection_note_refs(cid)
    if refs:
        notes = ", ".join(f"#{n['id']} {n['title']}" for n in _note_ref_labels(refs))
        return _render_collections_body(
            _notice("danger", "collection.delete_blocked", title=collection["title"], notes=notes))
    COLLECTIONS_STORE.delete(cid, _owner())
    # Panels sourced from it now resolve to an empty set → refresh them.
    return _render_collections_body(
        _notice("success", "collection.deleted", title=collection["title"]), workspace_changed=True)


def _open_collection_panel(collection: dict, meta: dict):
    """Appends a new image panel showing <collection> (gallery view, original rendering)."""
    panels = _get_panels()
    panel = _default_panel(_next_panel_id(), meta)
    panel.update({
        "xai_method": "original", "mode": MODE_IMAGES,
        "image_source": {"type": "collection", "id": collection["id"]},
        "visible": True, "configured": True,
    })
    panels.append(panel)
    _save_panels(panels)


@app.route("/collections/<int:cid>/open-panel", methods=["POST"])
def collection_open_panel(cid):
    """Opens a collection in a new panel straight from the management dialog."""
    if "project" not in session:
        return "", 403
    collection = _collection(cid)
    if collection is None:
        return _render_collections_body(_notice("danger", "collection.gone"))
    _open_collection_panel(collection, load_metadata(_active_dataset()))
    return _render_collections_body(
        _notice("success", "collection.opened", title=collection["title"]), workspace_changed=True)


# ── notes ──────────────────────────────────────────────────────────────

def _xai_display(method: str) -> str:
    return t("common.original") if method == "original" else method


def _image_variant_descr(rp: dict) -> str:
    """Short display of an image refpoint's variant: "Grad-CAM · VGG16 · high" or "Original".
    Empty for legacy refpoints without variant info (created before variants were stored)."""
    method = rp.get("xai_method")
    if method is None:
        return ""
    if method == "original":
        return t("common.original")
    return f"{method} · {rp.get('model')} · {rp.get('level')}"


def _workspace_snapshot(panels: list, dataset: str, meta: dict) -> dict:
    """Reference point "workspace": snapshot of the ENTIRE panel configuration — which panels
    exist, how each is configured, and the concrete image each panel currently displays
    (single view only; ``rel`` = displayed variant incl. XAI rendering, not just the base name)."""
    snap = []
    for p in panels:
        current = None
        if (p.get("view") == "single" and p.get("visible") and p.get("configured", True)
                and p.get("mode") != MODE_MODELCARD):
            images = _panel_images(p, panels, dataset, meta)
            if images:
                img = images[_clamp_index(p.get("index", 0), len(images))]
                current = {"image_id": img["id"], "rel": img["rel"]}
        snap.append({
            "panel_id": p["id"],
            "model": p["model"],
            "level": p["level"],
            "xai_method": p["xai_method"],
            "mode": p.get("mode", MODE_IMAGES),
            "filters": json.loads(json.dumps(p["filters"])),  # deep copy
            "modelcard_filter": (dict(p["modelcard_filter"])
                                 if isinstance(p.get("modelcard_filter"), dict) else None),
            "image_source": _image_source(p).copy(),
            "visible": p.get("visible", True),
            "view": p.get("view", "gallery"),
            # Single-view selection state, so a CRAFT concept / CRP rank+view survives a
            # workspace snapshot instead of resetting to the attribution map / rank 1.
            "crp_rank": p.get("crp_rank", 1),
            "crp_view": p.get("crp_view", "heatmap"),
            "craft_concept": p.get("craft_concept"),
            "current_image": current,
        })
    return {"type": "workspace", "panels": snap}


def _restore_workspace(snapshot_panels: list, meta: dict) -> list:
    """Rebuilds the session panel list from a workspace snapshot. Panel ids are KEPT so that
    image-source references between snapshot panels stay intact. The single-view index is
    resolved from the stored current_image (falls back to 0 if the image is gone)."""
    panels = []
    for ps in snapshot_panels:
        panels.append({
            "id": ps["panel_id"],
            "model": ps.get("model"),
            "level": ps.get("level"),
            "xai_method": ps.get("xai_method", "original"),
            # Legacy snapshots (before the modelcard mode) restore as image panels.
            "mode": MODE_MODELCARD if ps.get("mode") == MODE_MODELCARD else MODE_IMAGES,
            "filters": json.loads(json.dumps(ps.get("filters", empty_filter_config(_categories())))),
            "modelcard_filter": (dict(ps["modelcard_filter"])
                                 if isinstance(ps.get("modelcard_filter"), dict) else None),
            "image_source": dict(ps.get("image_source") or {"type": "filter"}),
            "visible": ps.get("visible", True),
            "view": ps.get("view", "gallery"),
            # Restore the single-view selection (legacy snapshots lack these → defaults, i.e.
            # CRAFT attribution map / CRP rank 1 + heatmap, exactly as before).
            "crp_rank": ps.get("crp_rank", 1),
            "crp_view": ps.get("crp_view", "heatmap"),
            "craft_concept": ps.get("craft_concept"),
            "index": 0,
            "linked": False,
            "configured": True,
        })
    # Second pass (sources must exist first): resolve indexes from the stored current images.
    for panel, ps in zip(panels, snapshot_panels):
        current = ps.get("current_image")
        if panel["view"] == "single" and current:
            base = _resolve_base_images(panel, panels, meta)
            panel["index"] = next(
                (i for i, img in enumerate(base) if img["id"] == current.get("image_id")), 0)
    return panels


def _note_refpoint_views(note: dict) -> list:
    """Display data for a note's reference points: icon + short label + tooltip + type
    (1:1 with note["refpoints"], so template loop indexes address the refpoint). The type
    drives badge actions: workspace → load, image → open in a panel.
    Unknown types (future extensions, e.g. modelcard) degrade to a generic badge."""
    views = []
    for rp in note.get("refpoints", []):
        rtype = rp.get("type")
        if rtype == "image":
            descr = _image_variant_descr(rp)
            label = rp.get("image_id", "?")
            if rp.get("xai_method") and rp["xai_method"] != "original":
                label += f" · {rp['xai_method']}"
            views.append({"type": rtype, "icon": "bi-image", "label": label,
                          "title": t("notes.ref.image_title") + (f" – {descr}" if descr else "")})
        elif rtype == "workspace":
            snap = rp.get("panels", [])
            details = []
            for ps in snap:
                part = f"P{ps.get('panel_id')}: {ps.get('model')} / {ps.get('level')} / " \
                       f"{_xai_display(ps.get('xai_method'))}"
                summary = _filter_summary(ps.get("filters", {}))
                if summary:
                    part += f" [{'; '.join(summary)}]"
                if ps.get("current_image"):
                    part += f" → {Path(ps['current_image'].get('rel', '')).name}"
                details.append(part)
            views.append({
                "type": rtype,
                "icon": "bi-window-split",
                "label": ", ".join(f"P{ps.get('panel_id')}" for ps in snap) or "?",
                "title": t("notes.ref.workspace_title") + (" – " + " | ".join(details)
                                                          if details else ""),
            })
        elif rtype == "panel_config":  # legacy type (pre-workspace notes), render only
            summary = _filter_summary(rp.get("filters", {}))
            title = t("notes.ref.panel_config_title")
            if summary:
                title += " – " + "; ".join(summary)
            views.append({
                "type": rtype,
                "icon": "bi-sliders",
                "label": f"{rp.get('model')} / {rp.get('level')} / {_xai_display(rp.get('xai_method'))}",
                "title": title,
            })
        elif rtype == "collection":
            # Referenced by id, not copied: the collection is frozen against destructive edits
            # (see the freeze rule above), but it can still be deleted-by-hand or predate a
            # store reset — a gone collection degrades to a non-clickable badge.
            collection = _collection(rp.get("id"))
            if collection is None:
                views.append({"type": rtype, "icon": "bi-collection", "gone": True,
                              "label": t("notes.ref.collection_gone", cid=rp.get("id")),
                              "title": t("notes.ref.collection_gone_title")})
            else:
                views.append({
                    "type": rtype, "icon": "bi-collection", "gone": False,
                    "label": collection["title"],
                    "title": t("notes.ref.collection_title",
                               count=len(collection["image_ids"])),
                })
        elif rtype == "model":
            views.append({"type": rtype, "icon": "bi-cpu", "label": rp.get("model", "?"),
                          "title": t("notes.ref.model_title")})
        elif rtype == "xai_method":
            views.append({"type": rtype, "icon": "bi-eye", "label": rp.get("method", "?"),
                          "title": t("notes.ref.method_title")})
        else:
            views.append({"type": rtype, "icon": "bi-question-circle", "label": str(rtype),
                          "title": str(rtype)})
    return views


def _requirements() -> list:
    """The catalog in effect for this session: the user's private copy for the active dataset,
    or the master while there is none (see requirements_store.py)."""
    return REQUIREMENTS_STORE.catalog(_owner(), _active_dataset())[0]


def _requirements_for_display() -> list:
    """Requirements catalog in the current UI language: [{label, text}, ...] in file order."""
    lang = get_lang()
    return [{"label": r["label"], "text": r.get(lang) or r.get("en") or r.get("de")}
            for r in _requirements()]


def _is_report_entry(note: dict) -> bool:
    """Whether a note is an audit-report entry. Reads the ``include_in_report`` flag and also
    honours the legacy ``note_type == "protocol_entry"`` from the first audit-report iteration."""
    return bool(note.get("include_in_report") or note.get("note_type") == "protocol_entry")


def _requirement_status_from_form(form) -> dict:
    """Reads the met/unmet verdicts from the dialog form. Each requirement has two mutually
    exclusive checkboxes (``req_met_<label>`` / ``req_unmet_<label>``); the JS island enforces
    exclusivity client-side, but the server treats "both" and "neither" as "no assessment"
    (label absent) so a tampered form can never store a contradiction."""
    status = {}
    for r in _requirements():
        label = r["label"]
        met = bool(form.get(f"req_met_{label}"))
        unmet = bool(form.get(f"req_unmet_{label}"))
        if met and not unmet:
            status[label] = "met"
        elif unmet and not met:
            status[label] = "unmet"
    return status


def _label_index(label: str) -> int:
    """Numeric sort key for a requirement label ("A10" after "A2", not lexical)."""
    return int(label[1:]) if label[1:].isdigit() else 0


def _auto_title(status: dict) -> str:
    """Auto-title for a report entry left untitled: unmet requirements first, then met — each
    sorted by number, e.g. {A1:unmet, A3:unmet, A2:met, A4:met} -> "A1⚠ A3⚠ A2✓ A4✓". Empty
    when nothing is assessed (caller then keeps the title mandatory)."""
    unmet = sorted((l for l, s in status.items() if s == "unmet"), key=_label_index)
    met = sorted((l for l, s in status.items() if s == "met"), key=_label_index)
    return " ".join([f"{l}⚠" for l in unmet] + [f"{l}✓" for l in met])


def _note_modal_context(note: dict = None) -> dict:
    """Context for the note dialog: reference-point suggestions — the entire panel
    configuration (one workspace snapshot) and the concrete image of each single-view panel —
    plus models/methods for solo refs. For editing, the existing reference points are listed
    with keep-checkboxes. The requirements catalog + the note's current verdicts always ride
    along so the "audit-report entry" checkbox can reveal the assessment table. The
    checkbox defaults to on in an exam project (``pruefung``); when editing it follows the
    note's stored flag."""
    dataset = _active_dataset()
    meta = load_metadata(dataset)
    panels = _get_panels()
    image_suggestions = []
    for p in panels:
        if not (p.get("visible") and p.get("configured", True) and p.get("view") == "single"
                and p.get("mode") != MODE_MODELCARD):
            continue  # a modelcard panel shows no image to suggest
        images = _panel_images(p, panels, dataset, meta)
        if images:
            img = images[_clamp_index(p.get("index", 0), len(images))]
            image_suggestions.append({
                "id": p["id"],
                "image_id": img["id"],
                "descr": _image_variant_descr(
                    {"xai_method": p["xai_method"], "model": p["model"], "level": p["level"]}),
            })
    methods = sorted({mth for mdl in meta["xai_availability"].values() for mth in mdl})
    # Collections offered as reference points. Those a visible panel currently shows are marked,
    # so the common case ("note about the set I am looking at") is one obvious click.
    shown_cids = {_image_source(p).get("id") for p in panels
                  if p.get("visible") and _image_source(p)["type"] == "collection"}
    collections = [{"id": c["id"], "title": c["title"], "count": len(c["image_ids"]),
                    "in_panel": c["id"] in shown_cids}
                   for c in _collections(dataset)]
    is_exam = session.get("project", {}).get("type") == "pruefung"
    is_report_entry = _is_report_entry(note) if note else is_exam
    if note:
        title_note = t("notes.edit_title", nid=note["id"])
        title_report = t("report_entry.edit_title", nid=note["id"])
    else:
        title_note = t("notes.new_title")
        title_report = t("report_entry.new_title")
    return {
        "note": note,
        "is_report_entry": is_report_entry,
        "title_note": title_note,
        "title_report": title_report,
        "requirements": _requirements_for_display(),
        "requirement_status": note.get("requirement_status", {}) if note else {},
        "workspace_panel_ids": ", ".join(f"P{p['id']}" for p in panels),
        "image_suggestions": image_suggestions,
        "collections": collections,
        "models": meta["models"],
        "methods": methods,
        "title_max": TITLE_MAX,
        "existing_refpoints": _note_refpoint_views(note) if note else [],
    }


def _note_refpoints_from_form(form, panels: list, dataset: str, meta: dict) -> list:
    """Builds the reference-point list from the note dialog form. The workspace refpoint is a
    snapshot of the entire panel configuration (panels are ephemeral session state). Image
    refpoints reference the CONCRETE variant: panel suggestions carry the panel's rendering
    (model/level/XAI method), free-text ids are originals. Ids are validated against the
    metadata; unknown ids are dropped (the dialog hint documents this)."""
    refpoints = []
    known_ids = {img["id"] for img in meta["images"]}
    seen_images = set()

    def add_image(image_id, model=None, level=None, method="original"):
        key = (image_id, model, level, method)
        if image_id not in known_ids or key in seen_images:
            return
        rp = {"type": "image", "image_id": image_id, "xai_method": method}
        if model is not None:
            rp["model"] = model
        if level is not None:
            rp["level"] = level
        refpoints.append(rp)
        seen_images.add(key)

    for p in panels:
        img_id = form.get(f"ref_image_{p['id']}", "")
        if img_id:
            add_image(img_id, model=p["model"], level=p["level"], method=p["xai_method"])
    if form.get("ref_workspace") and panels:
        refpoints.append(_workspace_snapshot(panels, dataset, meta))
    for raw in form.get("image_ids", "").replace(",", " ").split():
        add_image(raw)
    seen_collections = set()
    for raw in form.getlist("ref_collection"):
        try:
            cid = int(raw)
        except ValueError:
            continue
        if cid in seen_collections or _collection(cid) is None:
            continue
        refpoints.append({"type": "collection", "id": cid})
        seen_collections.add(cid)
    for model in form.getlist("ref_model"):
        if model in meta["models"]:
            refpoints.append({"type": "model", "model": model})
    all_methods = {mth for mdl in meta["xai_availability"].values() for mth in mdl}
    for method in form.getlist("ref_method"):
        if method in all_methods:
            refpoints.append({"type": "xai_method", "method": method})
    return refpoints


def _note_list_views() -> list:
    notes = _notes(dataset=_active_dataset())
    return [{**n, "is_report_entry": _is_report_entry(n),
             "refpoint_views": _note_refpoint_views(n)} for n in notes]


def _render_notes_list() -> str:
    """Just the note list (swapped into #notes-list, e.g. after a delete)."""
    return render_template("partials/_notes_list.html", notes=_note_list_views())


def _render_notes_panel(notice: dict = None) -> str:
    """The whole dock content: actions + list (swapped into #notes-content), optionally with a
    banner on top (the demo ceilings are the only thing that raises one here)."""
    return render_template("partials/_notes_panel.html", notes=_note_list_views(), notice=notice)


def _demo_limit_reached(kind: str) -> bool:
    """Whether this owner has hit the demo ceiling for notes/collections in the active dataset.

    Only in demo mode: an internal instance is used by people whose work is the point, and a
    ceiling that silently refuses to save would be worse than a full disk."""
    if not DEMO_MODE:
        return False
    if kind == "notes":
        return len(_notes(dataset=_active_dataset())) >= DEMO_MAX_NOTES
    return len(_collections(_active_dataset())) >= DEMO_MAX_COLLECTIONS


_CLOSE_MODAL_OOB = '<div id="modal-container" hx-swap-oob="true"></div>'


@app.route("/notes")
def notes_list():
    if "project" not in session:
        return "", 403
    return _render_notes_panel()


# ── requirements catalog editing ────────────────────────────

def _requirement_warnings(warnings: list) -> list:
    """Parser warnings → displayable sentences. Translated here (not in the template) so the
    catalogue keys stay literal in exactly one place."""
    return [t(f"protocol.catalog.warn.{w['code']}",
              **{key: value for key, value in w.items() if key != "code"})
            for w in warnings]


def _catalog_modal(source=None, error=None, warnings=None, saved=False, purged=0):
    """The catalog editing dialog. Without arguments: the currently active source plus the
    warnings its parsing produces (the user asked to see them after loading, not live)."""
    owner, dataset = _owner(), _active_dataset()
    if source is None:
        source = REQUIREMENTS_STORE.source(owner, dataset)
    if warnings is None:
        warnings = REQUIREMENTS_STORE.catalog(owner, dataset)[1]
    return render_template("partials/_requirements_modal.html",
                           source=source, error=error, saved=saved, purged=purged,
                           warnings=_requirement_warnings(warnings),
                           is_custom=REQUIREMENTS_STORE.is_custom(owner, dataset))


def _requirements_oob(status: dict, form) -> str:
    """Out-of-band refresh of the requirements table + legend in the *open* note form.

    The catalog dialog is opened from within that form, so after an edit the table behind it
    would show stale labels/texts. The verdicts on screen ride along in the request (the dialog
    includes the note form), so nothing the user had ticked is lost — except verdicts whose
    requirement no longer exists, which is exactly the intended structural cleanup."""
    context = {"requirements": _requirements_for_display(), "requirement_status": status,
               "is_report_entry": bool(form.get("is_report_entry")), "oob": True}
    return (render_template("partials/_req_section.html", **context)
            + render_template("partials/_req_legend.html", **context))


@app.route("/requirements", methods=["GET"])
def requirements_edit():
    """Editing dialog for the requirements catalog (pencil next to the (i) in a report entry)."""
    if "project" not in session:
        return "", 403
    return _catalog_modal()


@app.route("/requirements", methods=["POST"])
def requirements_save():
    """Stores the user's own catalog for the active dataset. Invalid TOML → nothing is written."""
    if "project" not in session:
        return "", 403
    source = request.form.get("source", "")
    if DEMO_MODE and len(source) > DEMO_MAX_CATALOG_CHARS:
        return _catalog_modal(source=source[:DEMO_MAX_CATALOG_CHARS],
                              error=t("demo.catalog_limit", n=DEMO_MAX_CATALOG_CHARS))
    # Read the on-screen verdicts BEFORE saving: _requirement_status_from_form iterates over the
    # catalog in effect, which is still the old one at this point.
    old_status = _requirement_status_from_form(request.form)
    catalog, warnings, error = REQUIREMENTS_STORE.save(
        _owner(), _active_dataset(), source)
    if error:
        return _catalog_modal(source=source, error=error)
    labels = {r["label"] for r in catalog}
    purged = NOTES_STORE.purge_requirement_labels(labels, _owner(), _active_dataset())
    status = {label: verdict for label, verdict in old_status.items() if label in labels}
    return (_catalog_modal(warnings=warnings, saved=True, purged=purged)
            + _requirements_oob(status, request.form))


@app.route("/requirements/reset", methods=["POST"])
def requirements_reset():
    """Discards the user's own catalog — the read-only master applies again."""
    if "project" not in session:
        return "", 403
    old_status = _requirement_status_from_form(request.form)
    REQUIREMENTS_STORE.reset(_owner(), _active_dataset())
    catalog, warnings = REQUIREMENTS_STORE.catalog(_owner(), _active_dataset())
    labels = {r["label"] for r in catalog}
    purged = NOTES_STORE.purge_requirement_labels(labels, _owner(), _active_dataset())
    status = {label: verdict for label, verdict in old_status.items() if label in labels}
    return (_catalog_modal(warnings=warnings, saved=True, purged=purged)
            + _requirements_oob(status, request.form))


def _resolve_note_title(form, is_report_entry: bool, status: dict):
    """The title the user typed, or — for an untitled report entry with at least one assessed
    requirement — an auto-generated one (see ``_auto_title``). Returns None when a title is
    still required (plain note, or report entry without any assessment): the caller sends 400."""
    title = form.get("title", "").strip()
    if title:
        return title
    if is_report_entry:
        auto = _auto_title(status)
        if auto:
            return auto
    return None


@app.route("/notes/new", methods=["GET"])
def note_new():
    if "project" not in session:
        return "", 403
    return render_template("partials/_note_form.html", **_note_modal_context())


@app.route("/notes/new", methods=["POST"])
def note_create():
    if "project" not in session:
        return "", 403
    is_report_entry = bool(request.form.get("is_report_entry"))
    status = _requirement_status_from_form(request.form)
    title = _resolve_note_title(request.form, is_report_entry, status)
    if title is None:
        return "", 400
    if _demo_limit_reached("notes"):
        return _render_notes_panel(_notice("warning", "demo.note_limit", n=DEMO_MAX_NOTES))
    dataset = _active_dataset()
    meta = load_metadata(dataset)
    refpoints = _note_refpoints_from_form(request.form, _get_panels(), dataset, meta)
    NOTES_STORE.create(
        title=title,
        text=request.form.get("text", "").strip(),
        refpoints=refpoints,
        owner=_owner(),
        user=session.get("user", "?"),
        project=session.get("project", {}).get("name", "?"),
        dataset=dataset,
        include_in_report=is_report_entry,
        requirement_status=status,
    )
    return _render_notes_panel()


@app.route("/notes/<int:nid>/edit", methods=["GET"])
def note_edit(nid):
    if "project" not in session:
        return "", 403
    note = _note(nid)
    if note is None:
        return "", 404
    return render_template("partials/_note_form.html", **_note_modal_context(note))


@app.route("/notes/<int:nid>/edit", methods=["POST"])
def note_update(nid):
    if "project" not in session:
        return "", 403
    note = _note(nid)
    if note is None:
        return "", 404
    is_report_entry = bool(request.form.get("is_report_entry"))
    status = _requirement_status_from_form(request.form)
    title = _resolve_note_title(request.form, is_report_entry, status)
    if title is None:
        return "", 400
    dataset = _active_dataset()
    meta = load_metadata(dataset)
    # Refpoints are frozen snapshots by default; a kept workspace refpoint can be explicitly
    # refreshed to the CURRENT workspace via its ref_refresh_<i> box (opt-in, so editing the
    # note text never silently overwrites the captured state).
    panels = _get_panels()
    kept = []
    for i, rp in enumerate(note["refpoints"]):
        if not request.form.get(f"ref_keep_{i}"):
            continue
        if rp.get("type") == "workspace" and request.form.get(f"ref_refresh_{i}") and panels:
            kept.append(_workspace_snapshot(panels, dataset, meta))
        else:
            kept.append(rp)
    added = _note_refpoints_from_form(request.form, panels, dataset, meta)
    refpoints = kept + [rp for rp in added if rp not in kept]
    NOTES_STORE.update(
        nid, _owner(), title=title, text=request.form.get("text", "").strip(), refpoints=refpoints,
        include_in_report=is_report_entry, requirement_status=status)
    return _render_notes_panel()


@app.route("/notes/<int:nid>/delete", methods=["POST"])
def note_delete(nid):
    if "project" not in session:
        return "", 403
    NOTES_STORE.delete(nid, _owner())
    return _render_notes_list()


def _note_refpoint(nid: int, idx: int, expected_type: str):
    """Reference point <idx> of note <nid> if it exists and has the expected type, else None."""
    note = _note(nid)
    refpoints = note.get("refpoints", []) if note else []
    if 0 <= idx < len(refpoints) and refpoints[idx].get("type") == expected_type:
        return refpoints[idx]
    return None


@app.route("/notes/<int:nid>/refpoint/<int:idx>/load-workspace", methods=["POST"])
def note_load_workspace(nid, idx):
    """Loads a stored workspace snapshot: the current panels are discarded (the badge asks
    for confirmation client-side) and replaced by the snapshot's panel configuration."""
    if "project" not in session:
        return "", 403
    rp = _note_refpoint(nid, idx, "workspace")
    if rp is None or not rp.get("panels"):
        return "", 404
    meta = load_metadata(_active_dataset())
    panels = _restore_workspace(rp["panels"], meta)
    _save_panels(panels)
    session["next_panel_id"] = max(p["id"] for p in panels) + 1
    return _render_workspace()


@app.route("/notes/<int:nid>/refpoint/<int:idx>/open-collection", methods=["POST"])
def note_open_collection(nid, idx):
    """Opens a referenced collection in a NEW panel (no target chooser: unlike a single image, a
    whole image set replacing a panel's content is more disruptive than adding a panel)."""
    if "project" not in session:
        return "", 403
    rp = _note_refpoint(nid, idx, "collection")
    collection = _collection(rp.get("id")) if rp else None
    if collection is None:  # stale note: collection deleted since
        return "", 404
    _open_collection_panel(collection, load_metadata(_active_dataset()))
    return _render_workspace()


@app.route("/notes/<int:nid>/refpoint/<int:idx>/open-image", methods=["GET"])
def note_image_target(nid, idx):
    """Target chooser for an image reference point: open in which panel (or a new one)?"""
    if "project" not in session:
        return "", 403
    rp = _note_refpoint(nid, idx, "image")
    if rp is None:
        return "", 404
    choices = [
        {"id": p["id"], "descr": f"{p['model']} / {p['level']} / {_xai_display(p['xai_method'])}"}
        for p in _get_panels() if p.get("visible")
    ]
    return render_template("partials/_note_target_modal.html",
                           nid=nid, idx=idx, image_id=rp["image_id"], choices=choices,
                           variant=_image_variant_descr(rp))


@app.route("/notes/<int:nid>/refpoint/<int:idx>/open-image", methods=["POST"])
def note_image_open(nid, idx):
    """Opens the referenced image in the chosen panel (or a new one) in single view. The
    refpoint references a CONCRETE variant, so the panel is switched to the stored rendering
    (model/level/XAI method; validated against the metadata — unavailable method falls back
    to original, like in the config dialog). Refpoints without model/level (free-text ids,
    legacy notes) keep the panel's model/level and show the original. The panel's filters
    are cleared and a foreign image source is reset, so returning to the gallery shows the
    UNFILTERED gallery in this panel's rendering."""
    if "project" not in session:
        return "", 403
    rp = _note_refpoint(nid, idx, "image")
    if rp is None:
        return "", 404
    meta = load_metadata(_active_dataset())
    pos = next((i for i, img in enumerate(meta["images"]) if img["id"] == rp["image_id"]), None)
    if pos is None:  # stale note: image no longer in the dataset
        return "", 404

    panels = _get_panels()
    target = request.form.get("target", "new")
    if target.startswith("panel:"):
        try:
            panel = _find_panel(panels, int(target.split(":", 1)[1]))
        except ValueError:
            panel = None
        if panel is None:
            return "", 404
    else:
        panel = _default_panel(_next_panel_id(), meta)
        panels.append(panel)
    # Restore the stored variant on the target panel.
    if rp.get("model") in meta["models"]:
        panel["model"] = rp["model"]
    if rp.get("level") in meta["levels"]:
        panel["level"] = rp["level"]
    method = rp.get("xai_method", "original")
    available = set(meta["xai_availability"].get(panel["model"], {}))
    panel["xai_method"] = method if (method == "original" or method in available) else "original"
    # Unfiltered base set → `pos` (metadata order) is the image's index in the panel list.
    panel["filters"] = empty_filter_config(filter_categories(meta))
    panel["image_source"] = {"type": "filter"}
    # A modelcard panel shows no images – opening one switches it back to image mode.
    panel["mode"] = MODE_IMAGES
    panel["view"] = "single"
    panel["index"] = pos
    panel["visible"] = True
    panel["configured"] = True
    _save_panels(panels)
    return _render_workspace() + _CLOSE_MODAL_OOB


# ── audit report ──────────────────────────────────────────────────────

def _report_entries() -> list:
    """The current owner's audit-report entries for the active dataset, newest first.

    Scoped to one person since 2026-08-14: the requirements catalog is per owner+dataset, so a
    report that mixed in other people's entries would render them against a catalog that never
    applied to them (their labels could mean something else entirely). Since 2026-08-30 that
    scoping is inherent — ``_notes()`` only ever returns the current owner's notes."""
    return [n for n in _notes(dataset=_active_dataset()) if _is_report_entry(n)]


def _report_context() -> dict:
    """Builds the audit report: an aggregate requirements overview (per catalog requirement,
    how many entries rate it met/unmet/no-assessment) plus each protocol entry with its
    assessed requirements (in catalog order) and reference points."""
    catalog = _requirements()
    texts = text_for(catalog, get_lang())  # label -> text (current UI language)
    entries = _report_entries()

    overview = []
    for r in catalog:
        label = r["label"]
        verdicts = [e.get("requirement_status", {}).get(label) for e in entries]
        met = verdicts.count("met")
        unmet = verdicts.count("unmet")
        overview.append({"label": label, "text": texts[label],
                         "met": met, "unmet": unmet, "na": len(entries) - met - unmet})

    entry_views = []
    for e in entries:
        status = e.get("requirement_status", {})
        # Assessed requirements in catalog order; unknown labels (catalog changed since) appended.
        catalog_labels = [r["label"] for r in catalog]
        ordered = [lbl for lbl in catalog_labels if lbl in status]
        ordered += [lbl for lbl in status if lbl not in catalog_labels]
        entry_views.append({
            **e,
            "verdicts": [{"label": lbl, "status": status[lbl],
                          "text": texts.get(lbl, lbl)} for lbl in ordered],
            "refpoint_views": _note_refpoint_views(e),
        })

    project = session.get("project", {})
    return {
        "project_name": project.get("name", "?"),
        "project_type": project.get("type"),
        "dataset": _active_dataset(),
        "user": session.get("user", "?"),
        "generated_at": date.today().isoformat(),
        "overview": overview,
        "entries": entry_views,
        "n_entries": len(entries),
        "has_catalog": bool(catalog),
    }


@app.route("/report")
def audit_report():
    """Standalone audit report page (opened in a new tab; print-friendly for PDF)."""
    if "project" not in session:
        return redirect(url_for("login"))
    return render_template("report.html", pdf=False, **_report_context())


@app.route("/report.pdf")
def audit_report_pdf():
    """Server-side PDF of the audit report. Renders the same template with ``pdf=True`` (drops
    the toolbar and non-embeddable glyphs) and converts it via xhtml2pdf — a pure-Python engine
    (no system libs, fits the heterogeneous-environment constraint). Imported lazily so a
    machine without xhtml2pdf still runs the app and only this route degrades."""
    if "project" not in session:
        return redirect(url_for("login"))
    try:
        from xhtml2pdf import pisa
    except ImportError:
        return t("report.pdf_unavailable"), 501
    html = render_template("report.html", pdf=True, **_report_context())
    buf = io.BytesIO()
    if pisa.CreatePDF(html, dest=buf, encoding="utf-8").err:
        return t("report.pdf_failed"), 500
    buf.seek(0)
    filename = f"pruefbericht_{_active_dataset()}_{date.today().isoformat()}.pdf"
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=filename)


# ── dev entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True)

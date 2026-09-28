"""Production WSGI entry point (gunicorn target ``wsgi:app``).

On the server this file lives at ``<deployment_path>/deployment/wsgi.py``; the app is a flat
script layout under ``<deployment_path>/xai_viewer/`` (no package), so this
module puts that directory on ``sys.path`` and *then* imports the Flask app.

It also bridges the deployment ``config.toml`` into the environment before the import: the app
reads secret key, Basic-Auth credentials and the datasets root from ``XAI_*`` environment
variables, so the server needs exactly one configuration file.
"""

import os
import re
import sys
import tomllib
from pathlib import Path

# On the server: <deployment_path>/deployment/wsgi.py → project root one level up.
_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "config.toml"

with open(_CONFIG_PATH, "rb") as f:
    _cfg = tomllib.load(f)


def _expand(cfg: dict) -> dict:
    """Resolves ``%(key)s`` references between top-level string values.

    deploymentutils does this when *it* reads the file; plain ``tomllib`` does not, and this
    entry point must not depend on deploymentutils (it is a deploy-time tool, not a runtime one).
    """
    pattern = re.compile(r"%\((.+?)\)s")
    for _ in range(5):  # a few passes: references may themselves contain references
        changed = False
        for key, value in cfg.items():
            if isinstance(value, str) and pattern.search(value):
                cfg[key] = pattern.sub(lambda m: str(cfg.get(m.group(1), m.group(0))), value)
                changed = True
        if not changed:
            break
    return cfg


_cfg = _expand(_cfg)

# ── config.toml → environment (read by xai_viewer/app.py) ─────────────────────

# Refuse the placeholder: with the secret from config-example.toml (public in the repository)
# anyone can forge session cookies for any account. deploy.py checks this too, but a server
# started directly with `gunicorn wsgi:app` never runs deploy.py.
_secret = str(_cfg.get("SECRET_KEY", "")).strip()
if not _secret or "example" in _secret:
    raise SystemExit("SECRET_KEY in config.toml is empty or still the example value — generate one: "
                     "python3 -c \"import secrets; print(secrets.token_urlsafe(50))\"")
os.environ["XAI_SECRET_KEY"] = _secret

# Access protection: only forwarded when configured — an empty user leaves the app open.
for _key in ("AUTH_USER", "AUTH_PASSWORD_HASH"):
    if _cfg.get(_key):
        os.environ[f"XAI_{_key}"] = str(_cfg[_key])

# Datasets live beside the deployment directory (they survive deploys and --purge). The default
# the app derives on its own points at the same place; setting it explicitly makes it configurable.
if _cfg.get("datasets_path"):
    os.environ["XAI_DATASETS_ROOT"] = str(_cfg["datasets_path"])

# Preselected dataset. Without this the app falls back to its code default ("RailPer"), which
# answers with the "setup required" page (HTTP 500) when only other datasets were uploaded.
if _cfg.get("default_dataset"):
    os.environ["XAI_DEFAULT_DATASET"] = str(_cfg["default_dataset"])

# Public demo. Booleans are forwarded as "1"/"" because the app
# reads plain environment strings; only a true value is passed on, so an absent key means off.
for _flag in ("DEMO_MODE", "COOKIE_SECURE"):
    if _cfg.get(_flag):
        os.environ[f"XAI_{_flag}"] = "1"
for _value in ("GUEST_TTL_HOURS", "ALLOWED_ORIGINS", "ACCOUNTS_FILE", "LEGAL_FILE"):
    if _cfg.get(_value):
        os.environ[f"XAI_{_value}"] = str(_cfg[_value])

sys.path.insert(0, str(_ROOT / "xai_viewer"))

# noqa: F401 — `app` is not used here, it *is* the gunicorn entry point (`wsgi:app`).
from app import app  # noqa: E402,F401  (import after the environment is populated)

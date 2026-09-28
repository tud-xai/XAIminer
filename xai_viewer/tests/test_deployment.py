"""Tests for the deployment-facing parts: the HTTP-Basic gate and the config example.

The gate itself lives in ``app.py`` (module-level constants fed from the environment by
``deployment/wsgi.py``); the tests patch those constants instead of the environment, because
``app.py`` is imported once per test session.
"""

import ast
import base64
import os
import re
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
from app import app as flask_app
from werkzeug.security import generate_password_hash

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_EXAMPLE = REPO_ROOT / "config-example.toml"
DEPLOY_PY = REPO_ROOT / "deployment" / "deploy.py"
WSGI_PY = REPO_ROOT / "deployment" / "wsgi.py"


def _deploy_constant(name: str):
    """A module-level list constant from deploy.py, read without importing it (importing runs
    its sanity checks and needs deploymentutils)."""
    tree = ast.parse(DEPLOY_PY.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in deploy.py")


def _rsync_excludes(pattern: str, path: str) -> bool:
    """Whether rsync would exclude ``path`` (relative to the transfer root) for ``pattern``.

    The rule that matters here: a pattern *without* a slash is matched against the file name at
    **every** depth; one starting with a slash is anchored to the transfer root.
    """
    if pattern.startswith("/"):
        return path == pattern.lstrip("/")
    if "/" in pattern.rstrip("/"):
        return path.endswith(pattern)
    return Path(path).name == pattern

# Keys deployment/wsgi.py and deploy.py read from the deployment config.
REQUIRED_CONFIG_KEYS = [
    "remote", "user", "PROJECT_NAME", "port", "venv", "python_version",
    "deployment_path", "datasets_path", "datasets", "default_dataset", "BACKUP_PATH", "BASE_URL",
    "SECRET_KEY", "AUTH_USER", "AUTH_PASSWORD_HASH",
]


class TestBasicAuthGate(unittest.TestCase):

    def setUp(self):
        flask_app.config["TESTING"] = True
        self.client = flask_app.test_client()
        self._saved = (app_module.AUTH_USER, app_module.AUTH_PASSWORD_HASH)

    def tearDown(self):
        app_module.AUTH_USER, app_module.AUTH_PASSWORD_HASH = self._saved

    def _protect(self, user="xai", password="xai"):
        app_module.AUTH_USER = user
        app_module.AUTH_PASSWORD_HASH = generate_password_hash(password)

    @staticmethod
    def _header(user, password):
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        return {"Authorization": f"Basic {token}"}

    def test_d01__gate_is_off_without_credentials(self):
        """Unconfigured = open: local development and every other test are unaffected."""
        app_module.AUTH_USER, app_module.AUTH_PASSWORD_HASH = "", ""
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_d02__configured_gate_demands_authentication(self):
        self._protect()
        resp = self.client.get("/login")
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Basic", resp.headers.get("WWW-Authenticate", ""))

    def test_d03__wrong_credentials_are_rejected(self):
        self._protect(password="correct")
        self.assertEqual(self.client.get("/login", headers=self._header("xai", "wrong")).status_code, 401)
        self.assertEqual(self.client.get("/login", headers=self._header("nope", "correct")).status_code, 401)

    def test_d04__correct_credentials_pass_through(self):
        self._protect(password="correct")
        self.assertEqual(self.client.get("/login", headers=self._header("xai", "correct")).status_code, 200)

    def test_d05a__a_user_without_a_hash_leaves_the_gate_off(self):
        """Half-configured is the dangerous state: whoever filled in a user name believes the
        instance is protected. The app must not pretend to be gated — and says so at startup
        (the usual cause is a config key named AUTH_PASSWORD instead of AUTH_PASSWORD_HASH)."""
        app_module.AUTH_USER, app_module.AUTH_PASSWORD_HASH = "xai", ""
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_d05b__a_hash_without_a_user_leaves_the_gate_off(self):
        app_module.AUTH_USER, app_module.AUTH_PASSWORD_HASH = "", generate_password_hash("x")
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_d05__gate_also_covers_labelling_and_image_routes(self):
        """It must sit in front of *everything* — side tools have no login of their own."""
        self._protect()
        paths = ["/data/mock/originals/imgs/nonexistent.jpg", "/"]
        for path in paths:
            self.assertEqual(self.client.get(path).status_code, 401, path)


class TestConfigExample(unittest.TestCase):

    def test_d10__example_config_carries_every_key_the_deployment_reads(self):
        with open(CONFIG_EXAMPLE, "rb") as f:
            cfg = tomllib.load(f)
        missing = [k for k in REQUIRED_CONFIG_KEYS if k not in cfg]
        self.assertFalse(missing, f"config-example.toml lacks: {missing}")

    def test_d11__placeholders_reference_existing_keys(self):
        """`%(key)s` is resolved by deploymentutils (and by wsgi.py) — a typo would surface as a
        literal placeholder in a path, i.e. as a wrong deployment directory."""
        with open(CONFIG_EXAMPLE, "rb") as f:
            cfg = tomllib.load(f)
        unknown = set()
        for value in cfg.values():
            if isinstance(value, str):
                unknown.update(k for k in re.findall(r"%\((.+?)\)s", value) if k not in cfg)
        self.assertFalse(unknown, f"config-example.toml references unknown keys: {sorted(unknown)}")

    def test_d13__python_version_names_an_explicit_minor_version(self):
        """Bare `python3` is 3.6 on the target host while python3.11…3.14 exist as separate
        executables — the venv must be created with an explicit one (the app needs >= 3.11)."""
        with open(CONFIG_EXAMPLE, "rb") as f:
            cfg = tomllib.load(f)
        self.assertRegex(cfg["python_version"], r"^python3\.(1[1-9]|[2-9]\d)$")

    def test_d12__example_secrets_are_recognisable_as_placeholders(self):
        """deploy.py refuses to deploy while these still contain 'example'/'replace-me'."""
        with open(CONFIG_EXAMPLE, "rb") as f:
            cfg = tomllib.load(f)
        self.assertIn("example", cfg["SECRET_KEY"])
        self.assertTrue("example" in cfg["AUTH_PASSWORD_HASH"] or "replace-me" in cfg["AUTH_PASSWORD_HASH"])


class TestUploadExcludes(unittest.TestCase):
    """What must not travel to the server — and what must."""

    def test_d14__user_data_is_excluded_only_inside_xai_viewer(self):
        """A bare `notes.json` pattern also hides `demo_seed/notes.json`: rsync matches an
        unanchored pattern at every depth, so the example notes every demo sandbox starts with
        would silently never reach the server."""
        patterns = [f"/xai_viewer/{name}" for name in _deploy_constant("USER_DATA_GLOBS")]
        self.assertTrue(any(_rsync_excludes(p, "xai_viewer/notes.json") for p in patterns),
                        "the real notes store must not be uploaded")
        self.assertFalse(any(_rsync_excludes(p, "xai_viewer/demo_seed/notes.json")
                             for p in patterns),
                         "the demo seed must be uploaded")

    def test_d15__deploy_uses_the_anchored_form(self):
        """Guards the call site, not just the constant."""
        source = DEPLOY_PY.read_text(encoding="utf-8")
        self.assertIn('f"/xai_viewer/{name}" for name in USER_DATA_GLOBS', source)
        self.assertNotIn("            *USER_DATA_GLOBS,", source)

    def test_d16__accounts_file_is_never_uploaded(self):
        """It holds password hashes, and local test accounts would replace the server's."""
        self.assertIn("accounts.toml", _deploy_constant("USER_DATA_GLOBS"))

    def test_d17__every_setting_wsgi_bridges_appears_in_the_config_example(self):
        """wsgi.py silently skips a key that is absent — a setting missing from the example is
        therefore a setting nobody knows exists."""
        wsgi = WSGI_PY.read_text(encoding="utf-8")
        keys = set(re.findall(r'_cfg\.get\("(\w+)"\)', wsgi))
        keys |= set(re.findall(r'_cfg\["(\w+)"\]', wsgi))
        # The loops list their keys as plain tuples of strings; pick those up too.
        for group in re.findall(r'for _(?:flag|value) in \(([^)]*)\):', wsgi):
            keys |= set(re.findall(r'"(\w+)"', group))
        example = CONFIG_EXAMPLE.read_text(encoding="utf-8")   # text: some keys are commented out
        missing = sorted(k for k in keys if k not in example)
        self.assertFalse(missing, f"config-example.toml does not mention: {missing}")

    def test_d18__wsgi_refuses_the_example_secret(self):
        """`gunicorn wsgi:app` bypasses deploy.py and its checks. With the public example secret
        anyone could forge session cookies, so the entry point itself must refuse to start."""
        import shutil
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "deployment").mkdir()
            shutil.copy(WSGI_PY, Path(tmp) / "deployment" / "wsgi.py")
            shutil.copy(CONFIG_EXAMPLE, Path(tmp) / "config.toml")
            result = subprocess.run([sys.executable, str(Path(tmp) / "deployment" / "wsgi.py")],
                                    capture_output=True, text=True, timeout=60)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("SECRET_KEY", result.stderr)


if __name__ == "__main__":
    unittest.main()

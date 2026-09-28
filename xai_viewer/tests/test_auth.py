"""
Identity and accounts (auth.py + the login route + the ownership the stores enforce).

The property under test throughout: *who may see and change a piece of data is decided by the
server-assigned owner key, never by the name someone typed.* See auth.py for the design.
"""

import json
import os
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as app_module
from app import COLLECTIONS_STORE, NOTES_STORE, REQUIREMENTS_STORE, app as flask_app
from auth import (AccountError, AccountStore, RateLimiter, account_of, account_owner,
                  accounts_path, is_account, is_guest, new_guest_owner, owner_slug,
                  valid_account_name)
import demo_seed
from tools import import_note, make_demo_seed
from notes import NotesStore, owner_of

TEST_DATASET = "mock"


class _Clock:
    """Hand-cranked time source for the rate limiter (no sleeping in tests)."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


# ── owner keys ────────────────────────────────────────────────────────────────

class TestOwnerKeys(unittest.TestCase):

    def test_a01__account_and_guest_keys_live_in_separate_namespaces(self):
        """No name a visitor can type produces a guest key, and vice versa."""
        self.assertTrue(is_account(account_owner("Anna")))
        self.assertFalse(is_guest(account_owner("Anna")))
        guest = new_guest_owner()
        self.assertTrue(is_guest(guest))
        self.assertFalse(is_account(guest))
        # Even a name that looks like the other namespace stays in its own.
        self.assertEqual(account_owner("guest:abc"), "user:guest:abc")
        self.assertFalse(is_guest(account_owner("guest:abc")))

    def test_a02__account_names_are_normalized_but_guest_tokens_are_unique(self):
        self.assertEqual(account_owner("  Anna  "), account_owner("anna"))
        self.assertNotEqual(new_guest_owner(), new_guest_owner())

    def test_a03__account_name_validation(self):
        for good in ("anna", "kunde-a", "tu.dresden", "a1"):
            self.assertTrue(valid_account_name(good), good)
        for bad in ("", "a", "-anna", "Anna", "anna müller", "anna/../etc", "x" * 41):
            self.assertFalse(valid_account_name(bad), bad)

    def test_a04__slug_is_readable_for_accounts_and_hashed_for_guests(self):
        """A guest token is case-sensitive randomness: lower-casing it into a file name would
        throw away entropy, so it is hashed instead."""
        self.assertEqual(owner_slug(account_owner("anna")), "user-anna")
        a, b = new_guest_owner(), new_guest_owner()
        self.assertTrue(owner_slug(a).startswith("guest-"))
        self.assertNotEqual(owner_slug(a), owner_slug(b))
        # Two tokens differing only in case must not collapse onto one file.
        self.assertNotEqual(owner_slug("guest:AbC"), owner_slug("guest:abc"))

    def test_a05__notes_written_before_owners_belong_to_that_account(self):
        self.assertEqual(owner_of({"user": "Anna"}), account_owner("anna"))
        self.assertEqual(owner_of({"owner": "guest:x", "user": "Anna"}), "guest:x")

    def test_a06__accounts_path_prefers_environment_then_config(self):
        self.assertEqual(accounts_path("/base"), Path("/base/accounts.toml"))
        self.assertEqual(accounts_path("/base", {"paths": {"accounts_file": "/x/a.toml"}}),
                         Path("/x/a.toml"))
        os.environ["XAI_ACCOUNTS_FILE"] = "/env/a.toml"
        try:
            self.assertEqual(accounts_path("/base", {"paths": {"accounts_file": "/x/a.toml"}}),
                             Path("/env/a.toml"))
        finally:
            del os.environ["XAI_ACCOUNTS_FILE"]


# ── account store ─────────────────────────────────────────────────────────────

class TestAccountStore(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = AccountStore(Path(self._tmp.name) / "accounts.toml")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a10__missing_file_means_no_accounts(self):
        self.assertEqual(self.store.names(), [])
        self.assertFalse(self.store.exists("anna"))
        self.assertFalse(self.store.verify("anna", "x"))

    def test_a11__add_list_verify_roundtrip_survives_a_new_instance(self):
        self.store.add("Anna", "geheim")
        reread = AccountStore(self.store.path)
        self.assertEqual(reread.names(), ["anna"])
        self.assertTrue(reread.verify("ANNA", "geheim"))   # name normalized, password is not
        self.assertFalse(reread.verify("anna", "Geheim"))

    def test_a12__password_is_not_stored_in_clear(self):
        self.store.add("anna", "geheim")
        self.assertNotIn("geheim", self.store.path.read_text(encoding="utf-8"))

    def test_a13__file_is_not_world_readable(self):
        self.store.add("anna", "geheim")
        self.assertEqual(self.store.path.stat().st_mode & 0o077, 0)

    def test_a14__refuses_bad_name_duplicate_and_empty_password(self):
        self.store.add("anna", "geheim")
        for name, password in (("anna", "x"), ("Anna B", "x"), ("a", "x"), ("neu", "  ")):
            with self.assertRaises(AccountError):
                self.store.add(name, password)

    def test_a15__password_change_and_removal(self):
        self.store.add("anna", "alt")
        self.store.set_password("anna", "neu")
        self.assertFalse(self.store.verify("anna", "alt"))
        self.assertTrue(self.store.verify("anna", "neu"))
        self.store.remove("anna")
        self.assertEqual(self.store.names(), [])
        with self.assertRaises(AccountError):
            self.store.remove("anna")


# ── login rate limit ──────────────────────────────────────────────────────────

class TestRateLimiter(unittest.TestCase):

    def test_a20__blocks_after_the_limit_and_recovers_with_time(self):
        clock = _Clock()
        limiter = RateLimiter(max_attempts=3, window_seconds=60, clock=clock)
        self.assertTrue(all(limiter.allow("ip") for _ in range(3)))
        self.assertFalse(limiter.allow("ip"))
        clock.now += 61
        self.assertTrue(limiter.allow("ip"))

    def test_a21__one_client_cannot_lock_out_another(self):
        limiter = RateLimiter(max_attempts=1, window_seconds=60, clock=_Clock())
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))

    def test_a22__reset_and_prune_keep_the_table_small(self):
        clock = _Clock()
        limiter = RateLimiter(max_attempts=1, window_seconds=60, clock=clock)
        limiter.allow("a")
        limiter.reset("a")
        self.assertTrue(limiter.allow("a"))
        clock.now += 61
        limiter.prune()
        self.assertEqual(limiter._hits, {})


# ── login route ───────────────────────────────────────────────────────────────

class TestLoginRoute(unittest.TestCase):
    """The two entrances: a guest without a password, an account with one."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_accounts = app_module.ACCOUNTS
        self._orig_demo = app_module.DEMO_MODE
        self._orig_limiter = app_module.LOGIN_LIMITER
        app_module.ACCOUNTS = AccountStore(Path(self._tmp.name) / "accounts.toml")
        app_module.ACCOUNTS.add("anna", "geheim")

    def tearDown(self):
        app_module.ACCOUNTS = self._orig_accounts
        app_module.DEMO_MODE = self._orig_demo
        app_module.LOGIN_LIMITER = self._orig_limiter
        self._tmp.cleanup()

    def _owner(self):
        with self.client.session_transaction() as sess:
            return sess.get("owner")

    def test_a30__guest_without_demo_mode_keeps_the_typed_name_as_owner(self):
        """An internal instance behaves as before: a developer finds their own notes again."""
        app_module.DEMO_MODE = False
        self.client.post("/login", data={"username": "kim", "password": ""})
        self.assertEqual(self._owner(), account_owner("kim"))

    def test_a31__guest_in_demo_mode_gets_a_private_sandbox(self):
        app_module.DEMO_MODE = True
        self.client.post("/login", data={"username": "Besucher", "password": ""})
        first = self._owner()
        self.assertTrue(is_guest(first))
        other = flask_app.test_client()
        other.post("/login", data={"username": "Besucher", "password": ""})
        with other.session_transaction() as sess:
            self.assertNotEqual(sess["owner"], first)  # same typed name, different sandbox

    def test_a32__display_name_survives_but_does_not_decide_anything(self):
        app_module.DEMO_MODE = True
        self.client.post("/login", data={"username": "Dr. Müller", "password": ""})
        with self.client.session_transaction() as sess:
            self.assertEqual(sess["user"], "Dr. Müller")
            self.assertTrue(is_guest(sess["owner"]))

    def test_a33__an_account_name_cannot_be_entered_without_its_password(self):
        """The attack the owner split exists for: typing someone's name must not be enough."""
        app_module.DEMO_MODE = True
        resp = self.client.post("/login", data={"username": "anna", "password": ""})
        self.assertEqual(resp.status_code, 401)
        self.assertIsNone(self._owner())

    def test_a34__wrong_password_is_refused(self):
        resp = self.client.post("/login", data={"username": "anna", "password": "falsch"})
        self.assertEqual(resp.status_code, 401)
        self.assertIsNone(self._owner())

    def test_a35__correct_password_yields_the_account_owner(self):
        resp = self.client.post("/login", data={"username": "Anna", "password": "geheim"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self._owner(), account_owner("anna"))
        self.assertEqual(account_of(self._owner()), "anna")

    def test_a36__a_password_for_a_non_account_is_refused_rather_than_ignored(self):
        """Silently letting someone in as a guest would hide a typo in the account name."""
        resp = self.client.post("/login", data={"username": "niemand", "password": "x"})
        self.assertEqual(resp.status_code, 401)

    def test_a37__repeated_attempts_are_rate_limited(self):
        app_module.LOGIN_LIMITER = RateLimiter(max_attempts=3, window_seconds=60, clock=_Clock())
        for _ in range(3):
            self.client.post("/login", data={"username": "anna", "password": "falsch"})
        resp = self.client.post("/login", data={"username": "anna", "password": "geheim"})
        self.assertEqual(resp.status_code, 429)
        self.assertIsNone(self._owner())

    def test_a38__logout_drops_the_owner(self):
        self.client.post("/login", data={"username": "anna", "password": "geheim"})
        self.client.post("/logout")
        self.assertIsNone(self._owner())

    def test_a39__the_guest_form_ignores_a_password_it_never_offered(self):
        """The guest section has no password field; a smuggled one must not turn the request
        into an account login (and must not fail on a name that has no account)."""
        app_module.DEMO_MODE = True
        resp = self.client.post("/login", data={"mode": "guest", "username": "Besucher",
                                                "password": "smuggled"})
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(is_guest(self._owner()))

    def test_a40__a_guest_picking_an_account_name_is_told_what_to_do(self):
        """Same refusal as a33, but worded for a form without a password field."""
        app_module.DEMO_MODE = True
        resp = self.client.post("/login", data={"mode": "guest", "username": "anna"})
        self.assertEqual(resp.status_code, 401)
        self.assertIsNone(self._owner())
        body = resp.data.decode()
        self.assertIn("registrierten Konto", body)
        self.assertNotIn("Passwort stimmt nicht", body)

    def test_a41__both_entrances_are_offered_separately(self):
        app_module.DEMO_MODE = True
        body = self.client.get("/login").data.decode()
        self.assertIn('value="guest"', body)
        self.assertIn('value="account"', body)
        self.assertIn("Als registrierter Nutzer anmelden", body)
        self.assertIn(f"{app_module.GUEST_TTL_HOURS} Stunden", body)  # deletion is stated

    def test_a42__the_deletion_notice_is_demo_only(self):
        """On an internal instance a guest keeps their typed name, and nothing is swept."""
        app_module.DEMO_MODE = False
        body = self.client.get("/login").data.decode()
        self.assertNotIn("automatisch gelöscht", body)

    def test_a43__the_account_form_is_folded_away_but_opens_on_its_own_error(self):
        """Collapsed by default so the guest entrance is the obvious one; open again when the
        visitor is looking at an error that belongs to it."""
        self.assertNotIn("<details class=\"login-account\" open>",
                         self.client.get("/login").data.decode())
        resp = self.client.post("/login", data={"mode": "account", "username": "anna",
                                                "password": "falsch"})
        self.assertIn("<details class=\"login-account\" open>", resp.data.decode())



# ── data isolation, end to end ────────────────────────────────────────────────

class TestDataIsolation(unittest.TestCase):
    """Two sessions, one store: neither may see, edit or delete the other's notes."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = NOTES_STORE.path
        NOTES_STORE.path = Path(self._tmp.name) / "notes.json"
        self.a = self._session("guest:aaa", "Anna")
        self.b = self._session("guest:bbb", "Bert")

    def tearDown(self):
        NOTES_STORE.path = self._orig_path
        self._tmp.cleanup()

    def _session(self, owner, name):
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess["user"] = name
            sess["owner"] = owner
            sess["role"] = "validierer"
            sess["project"] = {"name": "P", "type": "test", "dataset": TEST_DATASET,
                               "created_at": date.today().isoformat()}
        return client

    def test_a40__a_note_is_invisible_to_the_other_session(self):
        self.a.post("/notes/new", data={"title": "Annas Notiz", "text": "x",
                                        "ref_model": ["VGG16"]})
        self.assertIn("Annas Notiz", self.a.get("/notes").data.decode())
        self.assertNotIn("Annas Notiz", self.b.get("/notes").data.decode())

    def test_a41__someone_elses_note_can_be_neither_opened_nor_edited_nor_deleted(self):
        self.a.post("/notes/new", data={"title": "Annas Notiz", "text": "x",
                                        "ref_model": ["VGG16"]})
        nid = NOTES_STORE.list_notes("guest:aaa")[0]["id"]
        self.assertEqual(self.b.get(f"/notes/{nid}/edit").status_code, 404)
        self.assertEqual(self.b.post(f"/notes/{nid}/edit",
                                     data={"title": "gekapert"}).status_code, 404)
        # A delete that hits nothing must leave the note alone (it answers with the list).
        self.b.post(f"/notes/{nid}/delete")
        self.assertEqual(NOTES_STORE.get(nid, "guest:aaa")["title"], "Annas Notiz")

    def test_a42__the_audit_report_shows_only_the_own_entries(self):
        self.a.post("/notes/new", data={"title": "A-Eintrag", "text": "", "ref_model": ["VGG16"],
                                        "is_report_entry": "1"})
        self.b.post("/notes/new", data={"title": "B-Eintrag", "text": "", "ref_model": ["VGG16"],
                                        "is_report_entry": "1"})
        body = self.b.get("/report").data.decode()
        self.assertIn("B-Eintrag", body)
        self.assertNotIn("A-Eintrag", body)

    def test_a43__deleting_a_sandbox_removes_exactly_that_owner(self):
        self.a.post("/notes/new", data={"title": "A", "text": "", "ref_model": ["VGG16"]})
        self.b.post("/notes/new", data={"title": "B", "text": "", "ref_model": ["VGG16"]})
        self.assertEqual(NOTES_STORE.delete_owner("guest:aaa"), 1)
        self.assertEqual(NOTES_STORE.list_notes("guest:aaa"), [])
        self.assertEqual(len(NOTES_STORE.list_notes("guest:bbb")), 1)
        self.assertEqual(NOTES_STORE.owners(), {"guest:bbb"})


class TestLegacyData(unittest.TestCase):
    """Data written before owners existed must stay reachable for that account."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = NotesStore(Path(self._tmp.name) / "notes.json")

    def tearDown(self):
        self._tmp.cleanup()

    def test_a50__a_note_with_only_a_user_field_belongs_to_that_account(self):
        note = self.store.create(title="Alt", text="", refpoints=[], owner="", user="Kim",
                                 project="p", dataset=TEST_DATASET)
        # Simulate the pre-2026-08-30 shape: no owner key at all.
        data = self.store._load()
        del data["notes"][0]["owner"]
        self.store._save(data)
        self.assertEqual(self.store.list_notes(account_owner("kim"))[0]["title"], "Alt")
        self.assertIsNotNone(self.store.get(note["id"], account_owner("Kim")))
        self.assertIsNone(self.store.get(note["id"], "guest:x"))


class TestDemoModeConfiguration(unittest.TestCase):
    """How the switch is read. Reimplements app.py's two lines against a fake environment —
    the module-level value is fixed at import time and cannot be re-derived here."""

    @staticmethod
    def _resolve(env: str, config_enabled: bool) -> bool:
        env = (env or "").strip().lower()
        return (env in ("1", "true", "yes", "on") if env else config_enabled)

    def test_a58__environment_wins_over_config_in_both_directions(self):
        """`XAI_DEMO_MODE=0` in front of an enabling config.toml must switch the mode OFF."""
        self.assertTrue(self._resolve("1", False))
        self.assertFalse(self._resolve("0", True))
        self.assertFalse(self._resolve("false", True))

    def test_a59__without_the_variable_the_config_decides(self):
        self.assertTrue(self._resolve("", True))
        self.assertFalse(self._resolve("", False))
        self.assertFalse(self._resolve("   ", False))


# ── demo mode ─────────────────────────────────────────────────────────────────

class TestDemoMode(unittest.TestCase):
    """What the switch actually switches: side tools off, ceilings on, no indexing."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = NOTES_STORE.path
        NOTES_STORE.path = Path(self._tmp.name) / "notes.json"
        self._orig_demo = app_module.DEMO_MODE
        self._orig_max_notes = app_module.DEMO_MAX_NOTES
        with self.client.session_transaction() as sess:
            sess["user"] = "Gast"
            sess["owner"] = "guest:demo"
            sess["role"] = "validierer"
            sess["project"] = {"name": "P", "type": "test", "dataset": TEST_DATASET,
                               "created_at": date.today().isoformat()}

    def tearDown(self):
        app_module.DEMO_MODE = self._orig_demo
        app_module.DEMO_MAX_NOTES = self._orig_max_notes
        NOTES_STORE.path = self._orig_path
        self._tmp.cleanup()


    def test_a61__robots_txt_disallows_everything(self):
        body = self.client.get("/robots.txt").data.decode()
        self.assertIn("User-agent: *", body)
        self.assertIn("Disallow: /", body)

    def test_a62__demo_pages_carry_a_noindex_header(self):
        app_module.DEMO_MODE = False
        self.assertNotIn("X-Robots-Tag", self.client.get("/").headers)
        app_module.DEMO_MODE = True
        self.assertIn("noindex", self.client.get("/").headers.get("X-Robots-Tag", ""))

    def test_a63__note_ceiling_refuses_and_says_so(self):
        app_module.DEMO_MODE = True
        app_module.DEMO_MAX_NOTES = 1
        form = {"title": "Eine", "text": "", "ref_model": ["VGG16"]}
        self.client.post("/notes/new", data=form)
        body = self.client.post("/notes/new", data={**form, "title": "Zwei"}).data.decode()
        self.assertIn("alert-warning", body)  # the dock explains why nothing was saved
        stored = [n["title"] for n in NOTES_STORE.list_notes("guest:demo")]
        self.assertEqual(stored, ["Eine"])

    def test_a64__the_ceiling_does_not_apply_off_demo_mode(self):
        app_module.DEMO_MODE = False
        app_module.DEMO_MAX_NOTES = 1
        for title in ("Eine", "Zwei"):
            self.client.post("/notes/new", data={"title": title, "text": "",
                                                 "ref_model": ["VGG16"]})
        self.assertEqual(len(NOTES_STORE.list_notes("guest:demo")), 2)

    def test_a65__request_size_and_cookie_flags_are_configured(self):
        self.assertIsNotNone(flask_app.config["MAX_CONTENT_LENGTH"])
        self.assertTrue(flask_app.config["SESSION_COOKIE_HTTPONLY"])
        self.assertEqual(flask_app.config["SESSION_COOKIE_SAMESITE"], "Lax")

    def test_a66__a_note_body_cannot_grow_without_bound(self):
        from notes import TEXT_MAX
        note = NOTES_STORE.create(title="T", text="x" * (TEXT_MAX + 500), refpoints=[],
                                  owner="guest:demo", user="Gast", project="p",
                                  dataset=TEST_DATASET)
        self.assertEqual(len(note["text"]), TEXT_MAX)


# ── cross-site writes ─────────────────────────────────────────────────────────

class TestCrossSiteWrites(unittest.TestCase):
    """A POST that declares a foreign origin must not change anything (see app.py)."""

    def setUp(self):
        flask_app.config["TESTING"] = True
        flask_app.config["SECRET_KEY"] = "test-secret"
        self.client = flask_app.test_client()
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_path = NOTES_STORE.path
        NOTES_STORE.path = Path(self._tmp.name) / "notes.json"
        with self.client.session_transaction() as sess:
            sess["user"] = "Gast"
            sess["owner"] = "guest:xs"
            sess["role"] = "validierer"
            sess["project"] = {"name": "P", "type": "test", "dataset": TEST_DATASET,
                               "created_at": date.today().isoformat()}

    def tearDown(self):
        NOTES_STORE.path = self._orig_path
        self._tmp.cleanup()

    def _note(self):
        self.client.post("/notes/new", data={"title": "Meine Notiz", "text": "",
                                             "ref_model": ["VGG16"]})
        return NOTES_STORE.list_notes("guest:xs")[0]["id"]

    def test_a70__a_foreign_origin_cannot_delete_a_note(self):
        nid = self._note()
        resp = self.client.post(f"/notes/{nid}/delete", headers={"Origin": "https://evil.test"})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNotNone(NOTES_STORE.get(nid, "guest:xs"))

    def test_a71__a_foreign_referer_is_refused_too(self):
        """Older browsers send only Referer on a cross-site POST."""
        nid = self._note()
        resp = self.client.post(f"/notes/{nid}/delete",
                                headers={"Referer": "https://evil.test/page"})
        self.assertEqual(resp.status_code, 403)
        self.assertIsNotNone(NOTES_STORE.get(nid, "guest:xs"))

    def test_a72__our_own_origin_is_accepted(self):
        nid = self._note()
        resp = self.client.post(f"/notes/{nid}/delete", headers={"Origin": "http://localhost"})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(NOTES_STORE.get(nid, "guest:xs"))

    def test_a73__a_request_without_origin_still_works(self):
        """Server-side clients (and this suite) send neither header — and carry no victim's
        cookie either, so there is nothing to protect against."""
        nid = self._note()
        self.assertEqual(self.client.post(f"/notes/{nid}/delete").status_code, 200)

    def test_a74__reads_are_never_blocked(self):
        self.assertEqual(self.client.get("/notes",
                                         headers={"Origin": "https://evil.test"}).status_code, 200)


# ── example content for new sandboxes ─────────────────────────────────────────

class TestDemoSeed(unittest.TestCase):
    """Every visitor starts with a copy of the prepared notes — their own, editable copy."""

    SEED = [{"title": "Beispielnotiz", "text": "warum", "user": "Beispiel",
             "dataset": TEST_DATASET, "refpoints": [{"type": "model", "model": "VGG16"}]},
            {"title": "Anderer Datensatz", "text": "", "user": "Beispiel",
             "dataset": "sonstwas", "refpoints": []}]

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = NotesStore(Path(self._tmp.name) / "notes.json")

    def tearDown(self):
        self._tmp.cleanup()

    def _apply(self, owner="guest:neu"):
        return demo_seed.apply_notes(self.store, self.SEED, owner, TEST_DATASET, "Projekt")

    def test_a90__a_fresh_sandbox_receives_the_seed_of_its_dataset_only(self):
        self.assertEqual(self._apply(), 1)
        notes = self.store.list_notes("guest:neu")
        self.assertEqual([n["title"] for n in notes], ["Beispielnotiz"])
        self.assertEqual(notes[0]["refpoints"], [{"type": "model", "model": "VGG16"}])

    def test_a91__the_copy_belongs_to_the_visitor_but_keeps_the_seed_author(self):
        """It is theirs to edit and delete; the name on it is not their own, they did not write it."""
        self._apply()
        note = self.store.list_notes("guest:neu")[0]
        self.assertEqual(note["owner"], "guest:neu")
        self.assertEqual(note["user"], "Beispiel")
        self.assertEqual(note["project"], "Projekt")
        self.assertTrue(self.store.delete(note["id"], "guest:neu"))

    def test_a92__two_sandboxes_get_independent_copies(self):
        self._apply("guest:a")
        self._apply("guest:b")
        a = self.store.list_notes("guest:a")[0]
        self.store.update(a["id"], "guest:a", title="umbenannt")
        self.assertEqual(self.store.list_notes("guest:b")[0]["title"], "Beispielnotiz")

    def test_a93__seeding_is_idempotent_and_never_overwrites_own_work(self):
        self.assertEqual(self._apply(), 1)
        self.assertEqual(self._apply(), 0)          # already seeded
        self.assertEqual(len(self.store.list_notes("guest:neu")), 1)

    def test_a94__a_visitor_who_deleted_the_example_does_not_get_it_back(self):
        self._apply()
        note = self.store.list_notes("guest:neu")[0]
        self.store.delete(note["id"], "guest:neu")
        # A second project on the same dataset would re-seed only if nothing else is there;
        # that is the accepted trade-off — but within one project the example stays gone.
        self.assertEqual(self.store.list_notes("guest:neu"), [])

    def test_a95__a_broken_or_missing_seed_file_is_not_an_error(self):
        self.assertEqual(demo_seed.load(Path(self._tmp.name) / "gibtsnicht.json"), [])
        broken = Path(self._tmp.name) / "broken.json"
        broken.write_text("{nope", encoding="utf-8")
        self.assertEqual(demo_seed.load(broken), [])

    def test_a96__the_shipped_seed_file_is_loadable_and_complete(self):
        """Guards the actual file in the repository, not just the mechanism."""
        seeds = demo_seed.load(Path(__file__).resolve().parent.parent / "demo_seed"
                               / "notes.json")
        self.assertTrue(seeds, "demo_seed/notes.json yields no usable note")
        for note in seeds:
            self.assertTrue(note.get("dataset"), "a seed note without a dataset is never used")
            self.assertTrue(note.get("title"))


class TestMakeDemoSeed(unittest.TestCase):
    """The extraction tool. Its default must be *adding*: a seed whose source note is gone from
    notes.json cannot be recovered, and datasets get their examples one session at a time."""

    NOTES = {"next_id": 3, "notes": [
        {"id": 1, "title": "Erste", "text": "a", "refpoints": [], "dataset": "ds-a",
         "owner": "user:x", "user": "x", "project": "p", "created_at": "2026-01-01"},
        {"id": 2, "title": "Zweite", "text": "b", "refpoints": [], "dataset": "ds-b",
         "owner": "user:x", "user": "x", "project": "p", "created_at": "2026-01-02"},
    ]}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.notes = Path(self._tmp.name) / "notes.json"
        self.notes.write_text(json.dumps(self.NOTES), encoding="utf-8")
        self.out = Path(self._tmp.name) / "seed.json"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, *args):
        make_demo_seed.main([*args, "--notes-file", str(self.notes), "--out", str(self.out)])
        return json.loads(self.out.read_text(encoding="utf-8"))["notes"]

    def _titles(self, seeds):
        return [n["title"] for n in seeds]

    def test_a97__a_second_run_adds_instead_of_replacing(self):
        self._run("1")
        seeds = self._run("2")
        self.assertEqual(self._titles(seeds), ["Erste", "Zweite"])

    def test_a98__re_extracting_the_same_example_updates_it_in_place(self):
        """Improving a note in the app and pulling it again must not double the seed."""
        self._run("1")
        seeds = self._run("1", "--text", "besser")
        self.assertEqual(self._titles(seeds), ["Erste"])
        self.assertEqual(seeds[0]["text"], "besser")

    def test_a99__overwrite_starts_from_scratch(self):
        self._run("1")
        seeds = self._run("2", "--overwrite")
        self.assertEqual(self._titles(seeds), ["Zweite"])

    def test_a100__title_and_text_replace_what_the_note_said(self):
        """A note written for one's own use rarely carries the wording a visitor needs."""
        seeds = self._run("1", "--title", "Clever Hans", "--text", "warum")
        self.assertEqual((seeds[0]["title"], seeds[0]["text"]), ("Clever Hans", "warum"))
        self.assertNotIn("id", seeds[0])  # identity of the original is dropped

    def test_a101__the_same_title_in_another_dataset_is_a_different_seed(self):
        data = json.loads(self.notes.read_text(encoding="utf-8"))
        data["notes"][1]["title"] = "Erste"  # same title, other dataset
        self.notes.write_text(json.dumps(data), encoding="utf-8")
        self._run("1")
        seeds = self._run("2")
        self.assertEqual([(n["dataset"], n["title"]) for n in seeds],
                         [("ds-a", "Erste"), ("ds-b", "Erste")])


# ── guest sandbox expiry ──────────────────────────────────────────────────────

class TestGuestExpiry(unittest.TestCase):
    """Throwaway means throwaway — but only for guests, and only once they went quiet."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self._orig = (NOTES_STORE.path, COLLECTIONS_STORE.path, REQUIREMENTS_STORE.directory)
        NOTES_STORE.path = tmp / "notes.json"
        COLLECTIONS_STORE.path = tmp / "collections.json"
        REQUIREMENTS_STORE.directory = tmp

    def tearDown(self):
        (NOTES_STORE.path, COLLECTIONS_STORE.path, REQUIREMENTS_STORE.directory) = self._orig
        self._tmp.cleanup()

    def _note(self, owner, stamp=None):
        note = NOTES_STORE.create(title="N", text="", refpoints=[], owner=owner, user="x",
                                  project="p", dataset=TEST_DATASET)
        if stamp:
            data = NOTES_STORE._load()
            for stored in data["notes"]:
                if stored["id"] == note["id"]:
                    stored["updated_at"] = stamp
            NOTES_STORE._save(data)
        return note

    def _old(self):
        return (datetime.now() - timedelta(days=30)).isoformat(timespec="seconds")

    def test_a80__an_idle_guest_sandbox_is_collected(self):
        self._note("guest:alt", stamp=self._old())
        self.assertEqual(app_module._sweep_expired_guests(), 1)
        self.assertEqual(NOTES_STORE.list_notes("guest:alt"), [])

    def test_a81__a_recent_guest_sandbox_survives(self):
        self._note("guest:frisch")
        self.assertEqual(app_module._sweep_expired_guests(), 0)
        self.assertEqual(len(NOTES_STORE.list_notes("guest:frisch")), 1)

    def test_a82__accounts_are_never_collected_however_old(self):
        """Their persistence is exactly what a known user was given an account for."""
        self._note(account_owner("anna"), stamp=self._old())
        self.assertEqual(app_module._sweep_expired_guests(), 0)
        self.assertEqual(len(NOTES_STORE.list_notes(account_owner("anna"))), 1)

    def test_a83__collections_and_catalogue_go_with_the_sandbox(self):
        owner = "guest:komplett"
        self._note(owner, stamp=self._old())
        COLLECTIONS_STORE.create(title="C", image_ids=["a"], owner=owner, user="x", project="p",
                                 dataset=TEST_DATASET)
        REQUIREMENTS_STORE.save(owner, TEST_DATASET, '[[requirement]]\nlabel = "A1"\n'
                                                     'de = "eins"\nen = "one"\n')
        self.assertTrue(REQUIREMENTS_STORE.is_custom(owner, TEST_DATASET))
        # Make the collection look old as well, otherwise it keeps the sandbox alive.
        data = COLLECTIONS_STORE._load()
        data["collections"][0]["updated_at"] = self._old()
        COLLECTIONS_STORE._save(data)

        self.assertEqual(app_module._sweep_expired_guests(), 1)
        self.assertEqual(COLLECTIONS_STORE.list_collections(owner), [])
        self.assertFalse(REQUIREMENTS_STORE.is_custom(owner, TEST_DATASET))

    def test_a84__activity_in_either_store_keeps_the_sandbox(self):
        owner = "guest:teilaktiv"
        self._note(owner, stamp=self._old())
        COLLECTIONS_STORE.create(title="C", image_ids=["a"], owner=owner, user="x", project="p",
                                 dataset=TEST_DATASET)  # fresh
        self.assertEqual(app_module._sweep_expired_guests(), 0)
        self.assertEqual(len(NOTES_STORE.list_notes(owner)), 1)


class TestImportNote(unittest.TestCase):
    """The import tool (tools/import_note.py): a note from another store becomes a note of the
    target account — a copy with a fresh identity, never a second original."""

    NOTE = {"id": 22, "title": "Falsche Gründe", "text": "191 ff.", "owner": "user:quelle",
            "user": "quelle", "project": "Analyse", "dataset": "ds-a", "include_in_report": False,
            "requirement_status": {}, "created_at": "2026-01-01", "updated_at": "2026-01-02",
            "refpoints": [{"type": "workspace", "panels": []}]}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.target = Path(self._tmp.name) / "notes.json"
        self.store = NotesStore(self.target)

    def tearDown(self):
        self._tmp.cleanup()

    def _input(self, payload, name="export.json"):
        path = Path(self._tmp.name) / name
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def _run(self, payload, *args):
        return import_note.main([self._input(payload), "--notes-file", str(self.target), *args])

    def test_a110__the_copy_gets_a_fresh_identity_and_keeps_the_content(self):
        created = self._run(self.NOTE)
        self.assertNotEqual(created["id"], 22)          # the source id belongs to the source store
        self.assertNotEqual(created["created_at"], self.NOTE["created_at"])
        self.assertEqual(created["owner"], "user:quelle")
        self.assertEqual((created["title"], created["text"], created["refpoints"]),
                         (self.NOTE["title"], self.NOTE["text"], self.NOTE["refpoints"]))
        self.assertEqual(self.store.list_notes("user:quelle", dataset="ds-a"), [created])

    def test_a111__the_target_account_is_the_point_of_the_tool(self):
        """The usual case: a note written elsewhere is handed to one specific user here."""
        created = self._run(self.NOTE, "--account", "Anna", "--author", "Beispiel",
                            "--project", "P2")
        self.assertEqual(created["owner"], "user:anna")  # account names are lower-cased
        self.assertEqual((created["user"], created["project"]), ("Beispiel", "P2"))
        self.assertEqual(self.store.list_notes("user:quelle"), [])

    def test_a112__every_accepted_input_shape_yields_the_same_note(self):
        for payload in (self.NOTE, [self.NOTE], {"notes": [self.NOTE]},
                        {"next_id": 23, "notes": [self.NOTE]}):
            with self.subTest(payload=type(payload).__name__):
                created = self._run(payload, "--force")
                self.assertEqual(created["title"], self.NOTE["title"])

    def test_a113__several_notes_need_an_explicit_id(self):
        other = {**self.NOTE, "id": 7, "title": "Andere"}
        with self.assertRaises(SystemExit):
            self._run({"notes": [self.NOTE, other]})
        created = self._run({"notes": [self.NOTE, other]}, "--id", "7")
        self.assertEqual(created["title"], "Andere")

    def test_a114__a_collection_reference_point_is_refused(self):
        """It names a collection id of the SOURCE store — here it dangles or, worse, hits a
        different collection. Importing it anyway must be a deliberate act."""
        note = {**self.NOTE, "refpoints": [{"type": "collection", "id": 3}]}
        with self.assertRaises(SystemExit):
            self._run(note)
        self.assertEqual(self.store.list_notes("user:quelle"), [])
        self.assertTrue(self._run(note, "--force"))

    def test_a115__the_same_note_is_not_imported_twice_by_accident(self):
        """The tool is run by hand on a server; a repeated run must not silently duplicate."""
        self._run(self.NOTE)
        with self.assertRaises(SystemExit):
            self._run(self.NOTE)
        self.assertEqual(len(self.store.list_notes("user:quelle")), 1)
        self._run(self.NOTE, "--force")
        self.assertEqual(len(self.store.list_notes("user:quelle")), 2)

    def test_a116__a_note_without_a_dataset_would_be_invisible(self):
        """Notes are listed per owner AND dataset — one without a dataset is unreachable."""
        note = {k: v for k, v in self.NOTE.items() if k != "dataset"}
        with self.assertRaises(SystemExit):
            self._run(note)
        self.assertEqual(self._run(note, "--dataset", "ds-b")["dataset"], "ds-b")

    def test_a117__dry_run_writes_nothing(self):
        self.assertIsNone(self._run(self.NOTE, "--dry-run"))
        self.assertFalse(self.target.exists())


if __name__ == "__main__":
    unittest.main()

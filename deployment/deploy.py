"""
Deployment script for XAIminer on Uberspace.

Prerequisites:
    eval $(ssh-agent); ssh-add -t 10m

Initial deployment:
    python deployment/deploy.py --initial
    python deployment/deploy.py --upload-datasets   # separately: ~1 GB, see below

Regular update:
    python deployment/deploy.py

Adapted from a generic Uberspace skeleton. What is specific about this app:

- **No package.** `xai_viewer/` is a flat script layout with flat imports, so there is no
  `pip install -e .`; requirements are installed and gunicorn runs with `--chdir xai_viewer`.
- **No database.** User data are JSON files *inside* the deployed tree
  (`xai_viewer/notes.json`, `collections.json`, `labeling_*.json`). They are excluded from
  every upload (a local dev file would otherwise overwrite real data) and backed up before
  each deploy.
- **Datasets are not part of the deployment.** They are huge (RailPer ~1 GB / 17.500 files),
  live beside the deployment directory and are transferred only on demand
  (`--upload-datasets`), so a routine deploy stays small and never touches them.
"""

# NOTE: this targets Uberspace **7**, where user daemons are managed by supervisord
# (`~/etc/services.d/<name>.ini` + `supervisorctl reread/update/restart`). Uberspace 8 replaced
# that with systemd user units (`systemctl --user`); the corresponding template and the command
# equivalents are kept in comments so a migration is a small, obvious change.

import os
import sys
import time
import tomllib
from os.path import join as pjoin
from packaging import version
from ipydex import IPS, activate_ips_on_exception

min_du_version = version.parse("0.9.0")
try:
    import deploymentutils as du

    if version.parse(du.__version__) < min_du_version:
        print(f"deploymentutils>={min_du_version} required. Quit.")
        exit()
except ImportError:
    print("Install `deploymentutils` to run this script.")
    exit()

activate_ips_on_exception()

# Searched starting at *this file's* directory and then upwards → the repo root. The app's own
# `xai_viewer/config.toml` is in a different branch and can never be picked up by mistake.
config = du.get_nearest_config("config.toml")


def cfg(key):
    """Config access with a helpful message instead of a bare KeyError. The likely cause of a
    missing key is a config.toml copied from another project — this one grew keys of its own
    (datasets_path, AUTH_*)."""
    try:
        return config(key)
    except KeyError:
        print(du.bred(f"Key '{key}' is missing in {config.path}\n  Compare with config-example.toml."))
        sys.exit(1)


remote = cfg("remote")
user = cfg("user")
project_name = cfg("PROJECT_NAME")
service_name = project_name  # supervisord program name (Uberspace 8: f"{project_name}.service")
log_file = f"/home/{user}/logs/{project_name}.log"
port = cfg("port")
venv = cfg("venv")
venv_path = f"/home/{user}/{venv}"
# Explicit minor version (e.g. "python3.12"), NOT bare "python3" — see create_and_setup_venv().
python_version = cfg("python_version")
target_deployment_path = cfg("deployment_path")
target_datasets_path = cfg("datasets_path")
backup_path = cfg("BACKUP_PATH")
base_url = cfg("BASE_URL").strip("/")

asset_dir = pjoin(du.get_dir_of_this_file(), "files")
temp_workdir = pjoin(du.get_dir_of_this_file(), "tmp_workdir")
project_root = os.path.dirname(du.get_dir_of_this_file())

# Requirements file (the app is not a package, so this is what gets installed).
requirements_file = "xai_viewer/requirements.txt"

# User data of the deployed app: JSON stores *inside* the tree. Never uploaded, always backed up.
# NOTE `requirements_*.toml` are the per owner+dataset catalogs — the underscore keeps the
# read-only master `requirements.toml` out of this list, so that one still travels with a deploy.
#
# These are *file names* relative to xai_viewer/ (that is how the backup below uses them). For
# the rsync exclude they must be ANCHORED (see upload_files): an unanchored rsync pattern
# matches a bare file name at every depth, so plain `notes.json` would also have excluded
# `xai_viewer/demo_seed/notes.json` — the example notes every demo sandbox starts with would
# silently never reach the server.
USER_DATA_GLOBS = ["notes.json", "collections.json", "labeling_*.json", "requirements_*.toml",
                   # Accounts of the known demo users: password hashes, created ON the server
                   # with tools/manage_accounts.py. Uploading local ones would overwrite them.
                   "accounts.toml",
                   # Usage-tracking records (tracking.py) — the only DIRECTORY in this list,
                   # which is why the backup copies recursively. Note that this default location
                   # is inside the deployed tree: `--purge` deletes it. An instance that runs
                   # usability sessions should point `tracking_dir` outside.
                   "tracking"]

# Fail fast if run from an unexpected location (sanity check on the project root).
if not os.path.isfile(pjoin(project_root, "xai_viewer", "app.py")):
    print(
        du.bred(
            f"xai_viewer/app.py not found in project root:\n  {project_root}\n"
            "This script must live at <project_root>/deployment/deploy.py. Quit."
        )
    )
    sys.exit(1)

# Ensure a clean temp workdir so no stale rendered config files linger.
os.system(f"rm -rf {temp_workdir}")
os.makedirs(temp_workdir)

du.argparser.add_argument("-q", "--omit-requirements", help="skip pip install", action="store_true")
du.argparser.add_argument("--omit-upload-files", help="skip file upload", action="store_true")
du.argparser.add_argument(
    "-x", "--omit-backup", help="skip user-data backup before deployment", action="store_true"
)
# Datasets are big and rarely change → never part of a routine deploy.
du.argparser.add_argument(
    "--upload-datasets",
    help="additionally rsync the datasets named in config.toml (slow, GB-scale)",
    action="store_true",
)
du.argparser.add_argument(
    "-p",
    "--purge",
    help="purge target deployment dir before deploying (requires --omit-backup)",
    action="store_true",
)
du.argparser.add_argument("--debug", help="open IPS shell and exit", action="store_true")

args = du.parse_args(sys.argv[1:] + ["remote"])

c = du.StateConnection(remote, user=user, target=args.target)
print("Connection established. Adapting PATH ...")
PATH_ENV = c.run("echo $PATH", hide=True).stdout
c.env_variables["PATH"] = f"/home/{user}/.local/bin:{PATH_ENV}".strip("\n")
assert c.last_result.return_code == 0
print(du.bgreen("OK."))


class MainManager:
    def create_and_setup_venv(self):
        # `python -m venv` from the stdlib instead of bootstrapping `virtualenv` via the account's
        # pip: on Uberspace the bare `python3` can be ancient (3.6 on some hosts) while the usable
        # interpreters exist as explicit `python3.11`…`python3.14` executables. Installing anything
        # with *that* pip would target the wrong interpreter. `python_version` therefore names an
        # explicit minor version (see config-example.toml).
        res = c.run(f"{python_version} -V", target_spec="remote", warn=True)
        if res.exited != 0:
            print(du.bred(f"'{python_version}' not found on the remote host.\n"
                          "  Check `ls /usr/bin/python3.*` there and set `python_version` accordingly."))
            sys.exit(1)
        print(du.bgreen(f"Remote interpreter: {res.stdout.strip()}"))
        c.chdir("~")
        c.run(f"rm -rf {venv}")
        c.run(f"{python_version} -m venv {venv}")
        c.activate_venv(f"~/{venv}/bin/activate")
        c.run("pip install --upgrade pip")
        c.run("pip install gunicorn")

    def render_and_upload_config_files(self):
        c.activate_venv(f"~/{venv}/bin/activate")

        # Uberspace 7: supervisord service definition in ~/etc/services.d/.
        # (Uberspace 8 would render files/uberspace/.config/systemd/user/*.service instead.)
        tmpl_dir = os.path.join("uberspace", "etc", "services.d")
        tmpl_name = "template_PROJECT_NAME.ini"
        target_name = tmpl_name.replace("PROJECT_NAME", project_name).replace("template_", "")

        os.makedirs(pjoin(temp_workdir, tmpl_dir), exist_ok=True)
        du.render_template(
            tmpl_path=pjoin(asset_dir, tmpl_dir, tmpl_name),
            target_path=pjoin(temp_workdir, tmpl_dir, target_name),
            context=dict(
                venv_abs_bin_path=f"{venv_path}/bin",
                project_name=project_name,
                port=port,
                deployment_path=target_deployment_path,
                log_file=log_file,
                time_stamp=time.strftime(r"%Y-%m-%d %H-%M-%S"),
            ),
        )

        filters = "--exclude='**/README.md' --exclude='**/template_*'"
        c.rsync_upload(
            pjoin(temp_workdir, "uberspace") + "/",
            "~",
            filters=filters,
            target_spec="remote",
        )

    def enable_service(self):
        # Uberspace 7 (supervisord): `reread` re-reads ~/etc/services.d/, `update` applies the
        # difference and starts newly added programs (autostart=yes in the .ini).
        #   Uberspace 8 equivalent:
        #     c.run("systemctl --user daemon-reload", target_spec="remote")
        #     c.run(f"systemctl --user enable --now {service_name}.service", target_spec="remote")
        c.run(f"mkdir -p {os.path.dirname(log_file)}", target_spec="remote")  # stdout_logfile target
        c.run("supervisorctl reread", target_spec="remote")
        c.run("supervisorctl update", target_spec="remote")
        self.report_service_state()

    def report_service_state(self, timeout=60):
        """Waits for the program to report RUNNING; on failure prints the tail of its log.

        Polled rather than slept: `startsecs=30` in the .ini means supervisord reports STARTING
        for the first half minute, so a single immediate check would always look like a failure.
        (Uberspace 8: `systemctl --user is-active` + `journalctl --user -u <unit>`.)
        """
        deadline = time.time() + timeout
        state = ""
        while time.time() < deadline:
            res = c.run(f"supervisorctl status {service_name}", target_spec="remote", warn=True, hide=True)
            state = res.stdout.strip()
            if "RUNNING" in state:
                print(du.bgreen(f"Service is running: {state}"))
                return True
            if any(marker in state for marker in ("FATAL", "BACKOFF", "no such process")):
                break
            print(f"  service state: {state or '(no output)'} — waiting ...")
            time.sleep(5)
        print(du.bred(f"Service {service_name} is not running:\n  {state}"))
        c.run(f"tail -n 40 {log_file}", target_spec="remote", warn=True)
        return False

    def check_config(self):
        with open(config.path, "rb") as f:
            raw = tomllib.load(f)
        secret = raw.get("SECRET_KEY", "")
        if not secret or "example" in secret:
            print(
                du.bred(
                    "Error: SECRET_KEY not set (or still the example value) in config.toml — aborting.\n"
                    '  Generate one:  python3 -c "import secrets; print(secrets.token_urlsafe(50))"'
                )
            )
            sys.exit(1)
        print(du.bgreen("SECRET_KEY configured."))

        # Access protection: refuse to deploy an app that is silently world-readable because the
        # example hash was left in place. Running without protection stays possible, but only
        # deliberately (empty AUTH_USER).
        auth_user = raw.get("AUTH_USER", "")
        auth_hash = raw.get("AUTH_PASSWORD_HASH", "")
        if not auth_user:
            print(du.yellow("Warning: AUTH_USER empty — the deployment will be publicly accessible."))
        elif not auth_hash or "example" in auth_hash or "replace-me" in auth_hash:
            print(
                du.bred(
                    "Error: AUTH_USER is set but AUTH_PASSWORD_HASH is missing/still the example.\n"
                    '  Generate:  python3 -c "from werkzeug.security import generate_password_hash;'
                    " print(generate_password_hash('your-password'))\""
                )
            )
            sys.exit(1)
        else:
            print(du.bgreen(f"HTTP Basic Auth configured (user {auth_user!r})."))

        # A preselected dataset that was never uploaded makes every entry point answer with the
        # "setup required" page (HTTP 500) — and it leaves *no* trace in the log, because the
        # FileNotFoundError is handled. Warn (not abort): the dataset may already be on the
        # server from an earlier run.
        default_dataset = config("default_dataset", ignore_undefined=True, default=None)
        datasets = config("datasets", ignore_undefined=True, default=[]) or []
        if default_dataset and datasets and default_dataset not in datasets:
            print(du.yellow(
                f"Warning: default_dataset '{default_dataset}' is not in datasets {datasets}.\n"
                "  Make sure it is already on the server, otherwise every page answers 500."))

    def check_port(self):
        res = c.run("uberspace web backend list", target_spec="remote", warn=True)
        if f"http:{port}" in res.stdout:
            print(du.yellow(f"Warning: port {port} is already used by another backend:"))
            for line in res.stdout.splitlines():
                if f"http:{port}" in line:
                    print(du.yellow(f"  {line.strip()}"))
            print(du.yellow("Choose a different port in config.toml if this is not intentional."))
        else:
            print(du.bgreen(f"Port {port} is available."))

    def set_web_backend(self):
        # One-time domain setup (run manually before --initial if needed):
        #   uberspace web domain add <domain>
        # For subdomains of username.uber.space no DNS changes are needed.
        # For external domains, set A+AAAA records at your registrar first.

        res = c.run("uberspace web domain list", target_spec="remote", warn=True)
        if base_url not in res.stdout:
            print(du.yellow(f"Warning: '{base_url}' not found in domain list."))
            print(du.yellow(f"  -> uberspace web domain add {base_url}"))
            print(du.yellow("Continuing anyway — backend will be set, but requests may not arrive."))

        # Uberspace 7 syntax (Uberspace 8 uses `uberspace web backend add <domain> port <port>`).
        # Tolerated on failure: the CLI syntax differs between Uberspace generations, and a wrong
        # guess here should not abort a deployment that is otherwise complete.
        cmd = f"uberspace web backend set {base_url} --http --port {port}"
        res = c.run(cmd, target_spec="remote", warn=True)
        if res.exited != 0:
            print(du.bred(f"Could not set the web backend automatically:\n  {cmd}"))
            print(du.yellow("Run it by hand (check `uberspace web backend --help` for the syntax "
                            "of your Uberspace generation)."))

    def purge_deployment_dir(self):
        answer = input(f" -> {du.yellow('purging')} remote:{target_deployment_path} (y/N) ")
        if answer.lower() != "y":
            print(du.bred("Aborted."))
            sys.exit(1)
        c.run(f"rm -rf {target_deployment_path}", target_spec="remote")
        # Deliberately NOT the datasets: they live beside the deployment dir precisely so that a
        # purge does not throw away a GB-scale upload.
        print(du.bgreen("Deployment directory purged (datasets untouched)."))

    def perform_backup(self):
        """Copies the JSON stores (this app's 'database') into a timestamped backup directory."""
        timestamp = time.strftime("%Y-%m-%d__%H-%M-%S")
        target = f"{backup_path}/{timestamp}"
        source_dir = f"{target_deployment_path}/xai_viewer"
        c.run(f"mkdir -p {target}", target_spec="remote")
        found = False
        for pattern in USER_DATA_GLOBS:
            res = c.run(
                # -r: every pattern but the tracking directory is a plain file, and -r is
                # harmless for those.
                f"cp -r {source_dir}/{pattern} {target}/ 2>/dev/null", target_spec="remote", warn=True
            )
            found = found or res.exited == 0
        if found:
            print(du.bgreen(f"User data backed up: {target}"))
        else:
            c.run(f"rmdir {target}", target_spec="remote", warn=True)
            print(du.yellow("No user data found on remote — skipping backup (first deployment?)."))

    def upload_files(self):
        c.run(f"mkdir -p {target_deployment_path}", target_spec="remote")
        c.activate_venv(f"~/{venv}/bin/activate")
        # The deployment config first, explicitly: the project rsync below excludes every
        # config.toml (so the machine-specific xai_viewer/config.toml never lands on the server).
        c.rsync_upload(config.path, target_deployment_path, target_spec="remote")
        # It carries SECRET_KEY and the Basic-Auth hash and lives on a shared host.
        c.run(f"chmod 600 {target_deployment_path}/config.toml", target_spec="remote")
        exclude_patterns = [
            ".git/",
            ".idea/",
            "__pycache__/",
            "*.pyc",
            "deployment/tmp_workdir/",
            ".pytest_cache/",
            ".orchester/",
            # NEVER upload the user data: the deployed stores hold real notes, collections,
            # labels, accounts and requirements catalogs; a local dev copy would silently
            # overwrite them. Anchored to xai_viewer/ so that same-named files elsewhere in the
            # tree (demo_seed/notes.json) still travel — see USER_DATA_GLOBS.
            *[f"/xai_viewer/{name}" for name in USER_DATA_GLOBS],
            # Local-only config (datasets_root etc.); the server is configured via the
            # deployment config.toml, bridged by deployment/wsgi.py.
            "config.toml",
            # Local scratch files (a naming convention for never-committed notes).
            "*__gitignore__*",
            # aihook artefacts from UI verification runs.
            "aihook-screenshots/",
            "aihook-lock.yml",
            # aider working files (chat history, tag caches) — local tooling, not the app.
            ".aider*",
        ]
        filters = " ".join(f"--exclude='{p}'" for p in exclude_patterns)
        c.rsync_upload(project_root + "/", target_deployment_path, filters=filters, target_spec="remote")

    def upload_datasets(self):
        """Transfers the datasets named in config.toml. Slow and rarely needed → opt-in.

        `dataset_upload_excludes` can restrict what travels: rsync runs *without* --delete, so a
        partial upload now and the rest later is a supported workflow (e.g. the originals first,
        the XAI renderings and thumbnails once they are needed).
        """
        datasets = config("datasets", ignore_undefined=True, default=[]) or []
        if not datasets:
            print(du.yellow("No datasets configured (key `datasets`) — nothing to upload."))
            return
        local_root = config("local_datasets_root", ignore_undefined=True, default=None)
        local_root = local_root or os.path.abspath(pjoin(project_root, "..", "datasets"))
        excludes = config("dataset_upload_excludes", ignore_undefined=True, default=None)
        excludes = list(excludes) if excludes else ["thumbs-old/"]
        # Never silently: an exclude list is easy to forget and hard to notice on the server.
        print(du.yellow(f"Dataset upload excludes: {excludes}"))
        filters = " ".join(f"--exclude='{p}'" for p in excludes)

        c.run(f"mkdir -p {target_datasets_path}", target_spec="remote")
        for name in datasets:
            source = pjoin(local_root, name)
            if not os.path.isdir(source):
                print(du.bred(f"Dataset directory not found: {source} — skipping."))
                continue
            if not os.path.isfile(pjoin(source, "metadata.json")):
                print(du.yellow(f"{name}: no metadata.json — run tools/build_metadata.py first."))
            if not os.path.isdir(pjoin(source, "thumbs")) and not any("thumbs" in e for e in excludes):
                # Lazy generation on the server would cost ~0.6 s per image on first gallery view.
                print(du.yellow(f"{name}: no thumbs/ — run `python tools/make_thumbnails.py {name}` first."))
            print(f"Uploading dataset {name} (this takes a while) ...")
            c.rsync_upload(
                source + "/",
                f"{target_datasets_path}/{name}",
                filters=filters,
                target_spec="remote",
                # --copy-links: follow symlinks and upload what they point at. A dataset
                # directory may stitch together renderings that live elsewhere (a mounted group
                # drive, several export runs) instead of holding GBs of copies. Without this
                # rsync would *skip* the links silently ("skipping non-regular file") — the base
                # command is `rsync -pthrvz`, which carries neither -l nor -L.
                # --hard-links: keep hard links as hard links. A dataset may address the same
                # image under several model/level directories although it depends on neither
                # (Scene/Mask/Depth, see DATA.md); without this each copy travels in full and
                # the server stores it four times over.
                additional_flags="--copy-links --hard-links",
            )
            print(du.bgreen(f"Dataset {name} uploaded."))

    def install_requirements(self):
        # No `pip install -e .`: the app is a flat script layout, not a package.
        c.activate_venv(f"~/{venv}/bin/activate")
        c.chdir(target_deployment_path)
        c.run(f"pip install -r {requirements_file}", target_spec="remote")

    def finalize(self):
        # The app shows this stamp in its footer (app.py: DEPLOYMENT_STAMP_FILE), so the path must
        # be explicit — the working directory here depends on which steps were skipped.
        py_cmd = "import time; print(time.strftime(r'%Y-%m-%d %H:%M:%S'))"
        c.run(f'{python_version} -c "{py_cmd}" > {target_deployment_path}/deployment_date.txt',
              target_spec="remote")
        # Uberspace 8: c.run(f"systemctl --user restart {service_name}.service", ...)
        c.run(f"supervisorctl restart {service_name}", target_spec="remote", warn=True)
        if self.report_service_state():
            print(du.bgreen("Deployment done — service running."))
        else:
            print(du.bred(f"Deployment finished, but the service is down. Inspect: {log_file}"))

    def debug(self):
        mm.set_web_backend()
        IPS(-1)
        exit()


if __name__ == "__main__":
    mm = MainManager()

    if args.debug:
        mm.debug()

    # Data-destruction warning (bypass with --unsafe); guards every run, not just `--initial`.
    du.warn_user(
        project_name,
        args.target,
        getattr(args, "unsafe", False),
        deployment_path=target_deployment_path,
        user=user,
        host=remote,
    )

    mm.check_config()

    # Purge before deploying. Refuses to run without --omit-backup so we never back up a
    # directory that is about to be deleted.
    if args.purge:
        if not args.omit_backup:
            print(
                du.bred(
                    "--purge requires --omit-backup (refusing to back up a dir about to be deleted). Quit."
                )
            )
            sys.exit(1)
        mm.purge_deployment_dir()

    if args.initial:
        mm.check_port()
        print(du.yellow(f"Make sure the domain {base_url} is set up on Uberspace."))
        if input("Continue (N/y)? ").lower() != "y":
            print(du.bred("Aborted."))
            exit()
        mm.create_and_setup_venv()
        mm.render_and_upload_config_files()

    if not args.initial and not args.omit_backup:
        mm.perform_backup()

    if not args.omit_upload_files:
        mm.upload_files()

    if args.upload_datasets:
        mm.upload_datasets()

    if not args.omit_requirements:
        mm.install_requirements()

    if args.initial:
        # After the first upload: the unit file needs the code in place before it can start.
        mm.enable_service()
        mm.set_web_backend()
        print(du.yellow("Datasets are not deployed automatically — run with --upload-datasets."))

    mm.finalize()

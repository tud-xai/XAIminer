# Deployment (Uberspace 7, gunicorn + supervisord)

Deployment von XAIminer auf einen [Uberspace](https://uberspace.de)-Account. Das
Skelett stammt aus einem anderen Flask-Projekt und wurde an dieses hier angepasst — was
dabei anders ist, steht unten unter „Was an dieser App besonders ist".

## Prinzip

Alles läuft über ein Skript, `deploy.py`, auf Basis des Pakets `deploymentutils` (`du`):

1. **Config** — `deploy.py` lädt `config.toml`, gesucht **ab dem eigenen Verzeichnis aufwärts**
   (also `<repo>/config.toml`; die App-Config `xai_viewer/config.toml` liegt in einem anderen
   Ast und kann nicht versehentlich erwischt werden). Vorlage: `../config-example.toml`.
2. **Verbindung** — `du.StateConnection` öffnet eine SSH-Session (`c.run(..., target_spec="remote")`).
3. **Rendern** — die Service-Definition wird aus `files/.../template_PROJECT_NAME.ini` nach
   `tmp_workdir/` gerendert (`{{context.*}}`).
4. **Hochladen** — der gerenderte `uberspace/`-Baum landet im entfernten `$HOME` (die
   Service-Datei also in `~/etc/services.d/`), das Projekt in `deployment_path`.
5. **Service** — `gunicorn` unter **supervisord** (`supervisorctl reread && update`, Logs in
   `~/logs/<name>.log`), gebunden an ein Uberspace-Web-Backend.

### Uberspace 7 vs. 8

Dieses Deployment zielt auf **Uberspace 7** (supervisord). Uberspace 8 hat supervisord durch
systemd-User-Units ersetzt. Das entsprechende Template liegt weiterhin unter
`files/uberspace/.config/systemd/user/` und die Kommando-Äquivalente stehen als Kommentare
direkt neben den `supervisorctl`-Aufrufen in `deploy.py` — eine Migration ist damit eine
überschaubare, offensichtliche Änderung (Template-Pfad + drei Kommandos + Service-Name mit
`.service`-Suffix).

| | Uberspace 7 (hier) | Uberspace 8 |
|---|---|---|
| Service-Definition | `~/etc/services.d/<name>.ini` | `~/.config/systemd/user/<name>.service` |
| Übernehmen | `supervisorctl reread && supervisorctl update` | `systemctl --user daemon-reload && systemctl --user enable --now <name>` |
| Status / Neustart | `supervisorctl status\|restart <name>` | `systemctl --user status\|restart <name>` |
| Logs | `~/logs/<name>.log` | `journalctl --user -u <name>` |

## Was an dieser App besonders ist

- **Kein Paket.** `xai_viewer/` ist ein flaches Skript-Layout mit flachen Imports — es gibt
  kein `pip install -e .`. Installiert wird `xai_viewer/requirements.txt`, und gunicorn läuft
  mit `--chdir <deployment_path>/deployment` auf `wsgi:app`; `wsgi.py` legt `xai_viewer/` in
  den `sys.path`.
- **Keine Datenbank, aber Nutzerdaten im Baum.** `xai_viewer/notes.json`,
  `collections.json`, `labeling_*.json` und die eigenen Anforderungskataloge
  (`requirements_*.toml`) sind die „Datenbank". Sie sind von **jedem** Upload
  ausgeschlossen (sonst überschreibt ein lokaler Dev-Stand echte Daten) und werden vor jedem
  Deploy nach `BACKUP_PATH/<timestamp>/` kopiert.
- **Datensätze sind nicht Teil des Deployments.** RailPer ist ~1 GB / 17.500 Dateien. Sie
  liegen **neben** dem Deployment-Verzeichnis (`datasets_path`), werden nur mit
  `--upload-datasets` übertragen und überleben dadurch jeden Deploy und sogar `--purge`.
  Vorher lokal `python tools/make_thumbnails.py <dataset>` laufen lassen — sonst erzeugt der Server
  8000 Thumbnails einzeln beim ersten Galerie-Aufruf.
- **Zugriffsschutz** ist HTTP Basic Auth in der App selbst (`AUTH_USER` /
  `AUTH_PASSWORD_HASH` in der Config, über `wsgi.py` in die Umgebung gebrückt). Ein Uberspace-Web-Backend
  routet direkt auf gunicorn — es gibt kein `.htaccess`, in das man sich einhängen könnte.
- **`python_version` immer mit expliziter Minor-Version** (`python3.12`). Auf dem Zielhost ist
  `python3` = 3.6, die brauchbaren Interpreter liegen als eigene `python3.11`…`python3.14`
  daneben. Das venv wird mit `<python_version> -m venv` erzeugt (stdlib) — dadurch braucht es
  weder `virtualenv` noch das (uralte) System-`pip3`. `deploy.py` prüft vor dem Anlegen, ob der
  angegebene Interpreter überhaupt existiert.

## Ablauf

```bash
eval $(ssh-agent); ssh-add -t 10m

cp config-example.toml config.toml     # ausfüllen: remote, user, SECRET_KEY, AUTH_*
python deployment/deploy.py --initial          # venv, Service, Backend, erster Upload
python deployment/deploy.py --upload-datasets  # einmalig: die GB-Daten
python deployment/deploy.py                    # jedes weitere Update
```

Nützliche Flags: `--omit-requirements`, `--omit-upload-files`, `--omit-backup`,
`--upload-datasets`, `--purge` (Deployment-Verzeichnis vorher löschen, verlangt
`--omit-backup`; Datensätze bleiben unangetastet), `--unsafe` (überspringt die
Sicherheitsabfrage), `--debug` (IPS-Shell).

## Fehlersuche

**Logs:** `~/logs/<PROJECT_NAME>.log` (im `.ini` als `stdout_logfile` gesetzt, `redirect_stderr=true`,
gunicorn läuft mit `--capture-output`). `supervisorctl tail -1000 <name> stderr` meldet deshalb
korrekterweise „no log file" — es gibt bewusst nur einen Strom.

**500er ohne Traceback im Log** heißt fast immer: kein Fehler, sondern die Seite
„Einrichtung erforderlich". Die App fängt `FileNotFoundError` (fehlende `metadata.json`) mit einem
eigenen Errorhandler ab und liefert sie mit Status 500 — ein *behandelter* Fehler wird nicht
geloggt. Ursache ist dann ein Datensatz, der auf dem Server nicht liegt. Prüfen:

```bash
ssh <user>@<host> "ls <datasets_path>"          # was ist wirklich da?
```

und in der Config `default_dataset` auf einen davon setzen (`deploy.py` warnt, wenn er nicht in
`datasets` steht).

**Traceback erzwingen** (statt der 500-Seite), auf dem Server:

```python
# ~/probe.py — mit <deployment_path>/<venv> anpassen
import sys
sys.path.insert(0, "<deployment_path>/deployment")
import wsgi, app as m
m.AUTH_USER = ""            # Basic-Auth-Gate für die lokale Probe aus
wsgi.app.testing = True     # Exception durchreichen statt 500-Seite
print(m.DATASETS_ROOT, m.DEFAULT_DATASET, m.available_datasets())
print(wsgi.app.test_client().get("/login").status_code)
```

## Inhalt dieses Verzeichnisses

| Pfad | Zweck |
|---|---|
| `deploy.py` | Das Deploy-Skript. |
| `wsgi.py` | WSGI-Einstiegspunkt (`wsgi:app`): Config → Umgebung, `sys.path`, `from app import app`. |
| `files/uberspace/etc/services.d/template_*.ini` | supervisord-Service als Template (Uberspace 7, **aktiv**). |
| `files/uberspace/.config/systemd/user/template_*.service` | systemd-User-Unit als Template (Uberspace 8, **ungenutzt**). |
| `files/uberspace/README.md` | markiert den Baum, der nach remote `$HOME` gespiegelt wird. |
| `../config-example.toml` | Config-Vorlage — nach `config.toml` kopieren und ausfüllen. |

Das relative Layout (`deploy.py` in `deployment/`, `files/` daneben, `config.toml` eine Ebene
höher) ist tragend — nicht umsortieren.

---

## Anhang: einzelne Remote-Kommandos ohne vollen Deploy (SSH ControlMaster)

Praktisch, um Logs zu lesen oder den Service-Status zu prüfen, ohne jedes Mal komplett zu
deployen. Ein ControlMaster-Socket erlaubt weitere SSH-/SCP-Aufrufe **ohne erneute
Authentifizierung** — auch aus einer Agent-Session ohne Zugriff auf den privaten Schlüssel.

```bash
# einmalig: Master-Verbindung öffnen (aus einem Terminal mit SSH-Zugang)
ssh -M -S /tmp/uberspace-ctl -fN <user>@<server>.uberspace.de

# danach den Socket wiederverwenden:
ssh -S /tmp/uberspace-ctl <user>@<server>.uberspace.de "supervisorctl status xaiviewer"
ssh -S /tmp/uberspace-ctl <user>@<server>.uberspace.de "tail -n 50 ~/logs/xaiviewer.log"

# Metadaten auf dem Server neu bauen (nach einem Dataset-Upload):
ssh -S /tmp/uberspace-ctl <user>@<server>.uberspace.de \
  "cd <deployment_path>/xai_viewer && /home/<user>/<venv>/bin/python tools/build_metadata.py RailPer"

# Master-Verbindung schließen:
ssh -S /tmp/uberspace-ctl -O exit <user>@<server>.uberspace.de
```

Flags: `-M` ControlMaster (legt den Socket an), `-S` Socket-Pfad (frei wählbar), `-f`
Hintergrund, `-N` kein Kommando (hält die Verbindung nur offen).

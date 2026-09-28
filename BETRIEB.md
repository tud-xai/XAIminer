# XAIminer – Betriebshandbuch

Referenz für alle, die XAIminer **betreiben**: Einrichtung, Skripte, Deployment, Konten,
Demo-Modus, Tracking, Hilfswerkzeuge, Tests. Einen geführten Einstieg von null bis zur laufenden
App gibt [`xai_viewer/docs/setup.md`](xai_viewer/docs/setup.md), die Bedienung beschreibt
[`xai_viewer/docs/usage.md`](xai_viewer/docs/usage.md), Datensatz-Layout und Dateiformate stehen in
[`DATA.md`](DATA.md).

Alle Befehle mit `tools/…` werden aus `xai_viewer/` heraus ausgeführt.

## Daten

Bilder und Metadaten liegen **außerhalb** dieses Repos in einem Datensatz-Verzeichnis
(`../datasets/<name>/`), je Datensatz mit `originals/`, `xai/`, `inference/` und einer
generierten `metadata.json`. Aufbau, Namenskonventionen, Roh-Quellformate und das
`metadata.json`-Schema sind in [`DATA.md`](DATA.md) dokumentiert.

## Ersteinrichtung (einmalig nach dem Klonen)

```bash
# 1. Config anlegen und ggf. datasets_root anpassen
cd xai_viewer
cp config.toml.example config.toml

# 2. Metadaten für den Datensatz generieren (außerhalb des Repos, nicht eingecheckt)
python tools/build_metadata.py RailPer

# 3. App starten
flask --app app run        # http://127.0.0.1:5000
```

> **Ohne Schritt 2** zeigt die App eine hilfreiche Fehlerseite statt eines 500ers –
> mit dem genauen Pfad und dem Befehl zum Beheben.

## Setup (Details)

- Python ≥ 3.11. Abhängigkeiten: `pip install -r xai_viewer/requirements.txt` (plus `pytest`
  für die Tests).
- `datasets_root` in `config.toml`: ohne Eintrag wird `../../datasets` relativ zu
  `xai_viewer/` verwendet.

## Skripte im Überblick

Sie liegen in `xai_viewer/tools/` und werden aus `xai_viewer/` heraus ausgeführt
(`python tools/<name>.py …`). Die Daten-Pipeline-Skripte sind unten im Detail beschrieben,
die betriebsnahen jeweils in ihrem eigenen Abschnitt:

| Skript | Wofür | Details |
|---|---|---|
| `tools/build_metadata.py` | `metadata.json` eines Datensatzes erzeugen | unten |
| `tools/make_mock_dataset.py` | kleinen Testdatensatz für die Tests erzeugen | unten |
| `tools/make_thumbnails.py` | Galerie-Thumbnails vorab erzeugen | unten |
| `tools/manage_accounts.py` | Konten der bekannten Nutzer:innen anlegen/ändern/löschen | „Nutzer und Konten" |
| `tools/make_demo_seed.py` | eine Notiz aus der App zur Beispielnotiz für neue Sandboxen machen | „Demo-Modus → Beispielnotizen" |
| `tools/import_note.py` | eine exportierte Notiz ins Konto einer bestimmten Person legen | „Nutzer und Konten" |
| `tools/make_overview_figure.py` | Überblicksgrafik für `/docs/overview` (DE/EN) neu erzeugen | unten |

## Daten-Pipeline-Skripte

### `tools/build_metadata.py` – `metadata.json` erzeugen

Leitet die App-Metadaten aus den Roh-Quellen eines Datensatzes ab (Labels, Annotationen,
XAI-Verzeichnisse, Inference-CSVs) und schreibt `<dataset>/metadata.json`. Einmal pro
Datensatz bzw. nach Datenänderungen ausführen.

```bash
python tools/build_metadata.py RailPer      # Default-Datensatz
python tools/build_metadata.py mock
python tools/build_metadata.py --help       # u.a. die Rangfolge der Handlabel-Quellen
```

**Handlabels einbeziehen.** Ohne Handlabels sind die Filter-Attribute
(`umgebung`, `objekte`, `tageszeit`, `wetter`) **gemockt**. Das Skript liest sie aus der
**ersten** dieser Quellen und schreibt beim Lauf hin, welche es genommen hat:

| # | Quelle | wofür |
|---|--------|-------|
| 1 | `--labels PFAD` | eine Datei irgendwo — z. B. der Export, den jemand geschickt hat |
| 2 | `<dataset>/originals/manual_labels.json` | die Kopie, die mit dem Datensatz reist |

Danach die App neu starten (die `metadata.json` wird im Prozess gecacht). Eine Label-Datei
(Export) ist das **Transportformat zwischen Rechnern**: als `--labels <datei>` direkt
verwendbar oder als Kopie unter (2) — beide Formate werden am Inhalt erkannt, nicht am
Dateinamen. Details zum Schema: `DATA.md`.

### `tools/make_mock_dataset.py` – kleinen Testdatensatz erzeugen

Synthetisiert `datasets/mock/` im exakten Roh-Layout (winzige Bilder, gefakte Annotationen,
Labels, XAI- und Inference-Dateien). Dient als leichte Fixture für Tests und lokale
Entwicklung ohne den echten Datensatz. Danach `tools/build_metadata.py mock` ausführen.

### `tools/make_overview_figure.py` – Überblicksgrafik erzeugen

Die Hilfe der Hauptanwendung ist die Seite `/docs/overview` (öffentlich, öffnet in neuem Tab):
die gerenderte Anleitung `xai_viewer/docs/usage.md` (`docs_render.py`; `/docs/setup` analog für
`setup.md`). Sie beginnt mit der Grafik „Was ist XAIminer?" je UI-Sprache:
`static/docs/overview-<lang>.svg`. Die Screenshots der Anleitung liegen unter
`static/docs/usage/` (WebP, aufgenommen gegen RailPer).
Die SVGs sind **eingecheckte Build-Artefakte** (Thumbnails, Screenshot, Logo und Emojis
eingebettet) — der Server braucht weder Datensatz noch Skript. Neu erzeugen nur nach Änderungen
an Texten/Layout oder am Screenshot:

```bash
python tools/make_overview_figure.py      # braucht ../../datasets/RailPer (Stapel-Thumbnails)
```

- Screenshot: `tools/overview_assets/screenshot-two-panels-<lang>.png` (1420 × 640); fehlt die
  Sprache, wird der deutsche verwendet.
- **Nur hierfür** nötig: `fonttools` + `brotli` (`uv pip install fonttools brotli`) und eine
  CBDT-Emoji-Schrift (Noto Color Emoji; sonst `--emoji-font PFAD`).

### `tools/make_thumbnails.py` – Galerie-Thumbnails vorab erzeugen (Pre-Warm)

**Warum es das gibt:** Eine Galerie zeigt bis zu ~800 Bilder pro Panel. Würden dafür die
Originale in voller Auflösung geladen, wären das pro Panel hunderte MB (RailPer: ~480 MB,
einzelne PNGs bis ~5 MB) – die Seite wird träge. Deshalb liefert die App stattdessen kleine
**320×180-WebP-Thumbnails** (≈ 8 KB, 16:9-Center-Crop passend zu `object-fit: cover`) aus
`<dataset>/thumbs/`. Fehlende Thumbnails erzeugt die App zwar **lazy** beim ersten Aufruf –
dann aber zahlt der *erste* Nutzer die Generierungszeit (große PNGs ~0,6 s/Stück; RailPer hat
seit den Neu-Renderings vom 2026-08 rund **44.000** Quellbilder). Dieses
Skript erzeugt **alle** Thumbnails **vorab**, sodass im Betrieb niemand darauf warten muss. Die
Logik selbst steckt in `thumbnails.py` (gemeinsam genutzt von Skript und App-Route).

Der Lauf folgt **Symlinks** innerhalb des Datensatzes (`os.walk(followlinks=True)`) — ein
Datensatzverzeichnis darf seine Renderings von woanders einbinden. Ein Lauf über zehntausende
Bilder dauert entsprechend lange; er ist idempotent und kann jederzeit erneut gestartet werden,
bereits erzeugte Thumbnails werden übersprungen.

**Wann ausführen:** einmal pro Datensatz, nachdem dessen Bilder (originals oder xai) angelegt
oder geändert wurden. Das Skript ist idempotent – vorhandene Thumbnails werden übersprungen.

```bash
python tools/make_thumbnails.py RailPer        # Default-Datensatz vorwärmen
python tools/make_thumbnails.py mock           # Mock-Fixture
python tools/make_thumbnails.py RailPer --force  # alle neu erzeugen (z. B. nach Größenänderung)
```

In der **Einzelbild-Ansicht** lädt die App zusätzlich das jeweils nächste Bild der Panel-Sequenz
im Hintergrund vor (verstecktes `<img>`, `fetchpriority="low"`), damit Weiterblättern keinen
Download mehr kostet — dort werden die Bilder in voller Auflösung ausgeliefert.

**Ergebnis:** `datasets/<name>/thumbs/` spiegelt die Quellstruktur (`originals/` + `xai/`).
Das Verzeichnis liegt außerhalb des Repos und ist jederzeit reproduzierbar. Wird die
Thumbnail-Spezifikation in `thumbnails.py` geändert (Größe/Qualität), ändert sich der
Cache-Buster `?v=` der Bild-URLs automatisch; die alten Thumbnails dann mit `--force`
neu erzeugen.

## Bestimmte Bilder ganz vorn (`xai_viewer/featured_images.toml`)

Für Vorführungen (Messe, Demo-Termin): eine Handvoll Bilder soll **ohne Scrollen** erreichbar
sein. Die Datei `xai_viewer/featured_images.toml` listet pro Datensatz die Dateinamen, die in
jeder Bildliste vorn stehen sollen; alle übrigen Bilder folgen unverändert in der bisherigen
Reihenfolge dahinter.

```toml
[RailPer]
images = [
    "img_00421.jpg",   # Endung darf weggelassen werden
    "img_01337",
]
```

- **Reihenfolge in der Datei = Reihenfolge in der App.**
- Ein vorgezogenes Bild **ersetzt keinen Filter**: passt es nicht zum Filter des Panels, taucht
  es auch nicht auf. Es steht nur dann vorn, wenn es ohnehin im Ergebnis ist.
- Unbekannte Namen werden übersprungen und geloggt (`featured image … is not part of dataset …`).
- Wirkt in allen Bildlisten (Galerie, Einzelbild-Ansicht, aus Matrixzellen erzeugte Sammlungen,
  Hilfswerkzeuge), weil die Reihenfolge einmal zentral beim Laden der Metadaten gesetzt wird.
  Bereits **gespeicherte Sammlungen behalten ihre Reihenfolge** (sie sind Schnappschüsse).
- Änderungen wirken nach einem **Neustart** der App.
- Die Datei ist eingecheckt und wird mitdeployt — anders als jede `config.toml`, die `deploy.py`
  vom Upload ausschließt.

## Deployment

Auf einen Uberspace-Account (gunicorn unter supervisord). Alles Nötige liegt in
`deployment/` — **Details und Ablauf: `deployment/README.md`**. Kurz:

```bash
cp config-example.toml config.toml     # ausfüllen (gitignored: enthält SECRET_KEY + Passwort-Hash)
eval $(ssh-agent); ssh-add -t 10m
python deployment/deploy.py --initial          # venv, Service, Web-Backend, erster Upload
python deployment/deploy.py --upload-datasets  # einmalig: die Datensätze (~1 GB)
python deployment/deploy.py                    # jedes weitere Update
```

Drei Dinge, die man wissen sollte:

- **Nutzerdaten** (`xai_viewer/notes.json`, `collections.json`, `labeling_*.json` und die eigenen
  Anforderungskataloge `requirements_*.toml`) werden nie hochgeladen und vor jedem Deploy
  gesichert — sonst überschriebe ein lokaler Stand die echten.
- **Datensätze** liegen neben dem Deployment-Verzeichnis und werden nur mit `--upload-datasets`
  übertragen. Vorher lokal `python tools/make_thumbnails.py <dataset>` laufen lassen.
- **Zugriffsschutz** ist HTTP Basic Auth vor der ganzen App (`AUTH_USER`/`AUTH_PASSWORD_HASH` in
  der Config — der Hash, **nicht** ein Klartext-Passwort; ist nur eines von beiden gesetzt,
  bleibt das Gate aus und die App sagt es beim Start). Ohne gesetzte Credentials läuft die App ungeschützt — für eine **öffentliche
  Demo** ist genau das gewollt (sonst käme kein Besucher an den Login); für eine interne Instanz
  nicht. Wer die Demo-Instanz betreibt, konfiguriert Basic Auth **nicht** und schaltet
  stattdessen den Demo-Modus ein (siehe unten). Beides zusammen ergibt keinen Sinn und wird
  beim Start als Warnung gemeldet.

## Nutzer und Konten

Die App kennt zwei Arten von Nutzer:innen (siehe `xai_viewer/auth.py`):

- **Gast** — meldet sich mit einem beliebigen Namen und *ohne* Passwort an. Im Demo-Modus
  bekommt er eine private, wegwerfbare Sandbox; seine Notizen und Sammlungen gehören ihm allein
  und verschwinden mit der Zeit wieder.
- **Bekannte:r Nutzer:in** — meldet sich mit Name **und** Passwort an einem vorab angelegten
  Konto an und hat echte Persistenz. Kein Selbstregistrieren, kein Passwort-Reset.

Entscheidend ist die Trennung von *Anzeigename* und *Eigentümerschlüssel*: der eingetippte Name
steht nur in der Navbar und als Verfasser an Notizen; wer welche Daten sehen darf, entscheidet
ein serverseitig vergebener Schlüssel. Ein fremder Name allein öffnet nichts.

### Konten anlegen

```bash
cd xai_viewer
python tools/manage_accounts.py list
python tools/manage_accounts.py add anna          # fragt das Passwort zweimal ab
python tools/manage_accounts.py passwd anna
python tools/manage_accounts.py remove anna       # löscht das Konto, nicht seine Daten
```

Kontonamen sind 2–40 Zeichen aus `a–z 0–9 . _ -`. Die Datei `xai_viewer/accounts.toml` enthält
Passwort-Hashes, ist gitignored und wird **nie** hochgeladen — Konten für den Server werden
deshalb *auf dem Server* angelegt, im Deployment-Verzeichnis:

```bash
python xai_viewer/tools/manage_accounts.py add <name>
```

### Eine Notiz in ein fremdes Konto legen

Eine Notiz, die jemand anders geschrieben hat (oder die auf einer anderen Instanz entstanden ist),
kommt mit `tools/import_note.py` in das Konto einer bestimmten Person. Gedacht für den Server:
`notes.json` wird von keinem Deploy hochgeladen, die Notiz muss also dort hinein, wo sie hin soll.

```bash
cd xai_viewer
python tools/import_note.py export.json                 # Eigentümer/Verfasser wie in der Datei
python tools/import_note.py export.json --account anna  # stattdessen in Annas Konto
python tools/import_note.py export.json --id 22         # eine von mehreren Notizen auswählen
python tools/import_note.py export.json --dry-run       # nur sagen, was passieren würde
```

Als Eingabe taugt eine ganze `notes.json`, ein `{"notes": […]}`-Umschlag, eine Liste oder eine
einzelne Notiz. Die Notiz wird **kopiert**: frische Id, frische Zeitstempel, Eigentümer ist das
Zielkonto. Geschrieben wird über denselben `NotesStore.create()`, den auch die App benutzt —
also mit Sperre und atomarem Schreiben, und **ohne Neustart** des Servers (der Store liest die
Datei bei jedem Zugriff neu). Von Hand in `notes.json` zu editieren hat beides nicht.

Zwei Schutzschalter, beide mit `--force` zu übergehen: ein `collection`-Bezugspunkt wird
abgelehnt (er nennt eine Sammlungs-Id des *Quell*-Stores, die hier nichts oder das Falsche
trifft), und ein bereits vorhandener gleicher Titel im selben Datensatz ebenso — ein zweiter
Aufruf auf dem Server soll nicht unbemerkt duplizieren.

## Demo-Modus (öffentliche Instanz)

Ein Schalter, der dieselbe App für den offenen Betrieb härtet:

```bash
cd xai_viewer
XAI_DEMO_MODE=1 flask --app app run      # lokal ausprobieren
```

oder dauerhaft in `xai_viewer/config.toml`:

```toml
[demo]
enabled = true
```

Im Deployment steht der Schalter in der Deployment-`config.toml` (`DEMO_MODE = true`, siehe
`config-example.toml`). Was er ändert:

| | Demo-Modus aus | Demo-Modus an |
|---|---|---|
| Anmeldung ohne Passwort | Eigentümer = eingetippter Name | private Wegwerf-Sandbox |
| Maximale Request-Größe | 32 MB | 2 MB |
| Notizen / Sammlungen je Datensatz | unbegrenzt | 100 / 50 |
| Anforderungskatalog | unbegrenzt | 20 000 Zeichen |
| Gast-Daten | — | nach 48 h ohne Aktivität gelöscht |
| Suchmaschinen | `robots.txt` | zusätzlich `X-Robots-Tag: noindex` |

Unabhängig vom Schalter gilt: Notizen, Sammlungen und Anforderungskataloge gehören genau einer
Person (siehe oben), schreibende Requests von fremden Seiten werden abgelehnt (siehe „Schutz vor
Cross-Site-Requests" unten), und `/login` ist auf 10 Versuche pro Minute begrenzt.

**Beim Aufsetzen der öffentlichen Instanz beachten:**

- **Kein Basic Auth** konfigurieren (`AUTH_USER` leer lassen) — sonst kommt kein Besucher an den
  Login. Die App warnt beim Start, wenn beides gesetzt ist.
- **`SECRET_KEY` setzen.** Ohne ihn signiert die App Sessions mit einem Wert, der im Repo steht;
  im Demo-Modus wird das beim Start als Warnung gemeldet.
- **`COOKIE_SECURE = true`** bei HTTPS. Lokal über `http://` **nicht** setzen, sonst verwirft der
  Browser das Cookie.
- **Konten anlegen** für die bekannten Nutzer:innen (siehe oben), auf dem Server.
- Wenn nach dem Deploy jede Aktion mit „Cross-site request rejected" (403) antwortet: siehe
  „Schutz vor Cross-Site-Requests (CSRF)", Abschnitt *Im Fehlerfall*.

> **Eine bestehende Instanz auf Demo-Modus umstellen?** Dann sind die dort liegenden Notizen
> und Sammlungen für neue Besucher unsichtbar — sie gehören `user:<damaliger Name>`, und Gäste
> bekommen im Demo-Modus einen Zufallsschlüssel. Zwei Wege: entweder eine **eigene Instanz**
> für die Demo (empfohlen, dann bleiben Arbeits- und Demo-Daten getrennt), oder für die
> betroffene Person ein **Konto mit genau ihrem bisherigen Anzeigenamen** anlegen
> (`tools/manage_accounts.py add <name>`, klein geschrieben) — nach der Anmeldung daran sind ihre
> alten Notizen wieder da.

### Beispielnotizen (Seed)

Ein leerer Arbeitsbereich ist ein schlechter erster Eindruck — ein Besucher sieht Panels und
Filter, aber nicht, wozu Notizen und Prüfbericht da sind. Deshalb bekommt jede frische Sandbox
**Kopien** vorbereiteter Notizen: eigene Kopien, die man ändern und löschen darf.

Die Vorlagen stehen in `xai_viewer/demo_seed/notes.json` (eingecheckt — Beispielinhalt, keine
Nutzerdaten). Eine neue Vorlage schreibt man **in der laufenden App** und zieht sie dann heraus:

```bash
cd xai_viewer
python tools/make_demo_seed.py 1                     # Notiz mit id 1 aus notes.json
python tools/make_demo_seed.py 1 3 --author "Beispiel"
python tools/make_demo_seed.py 7 --title "Clever Hans Effect" --text "..."
```

Id, Eigentümer, Zeitstempel und Projekt werden dabei verworfen — die gehören zur Kopie, nicht
zur Vorlage. `--title`/`--text` überschreiben Titel und Text: Eine Notiz, die man für sich
selbst geschrieben hat, trägt selten die Formulierung, die ein Erstbesucher braucht.
`--help` zeigt die restlichen Schalter (`--notes-file`, `--out`). Zu beachten:

- **Vorlagen werden ergänzt, nicht ersetzt.** Ein Lauf für einen Datensatz darf die Vorlagen
  eines anderen nicht stillschweigend wegwerfen — die sind verloren, sobald ihre Ursprungsnotiz
  nicht mehr in `notes.json` steht. Gleicher Titel im gleichen Datensatz gilt als *dieselbe*
  Vorlage und wird an Ort und Stelle aktualisiert, ein erneutes Ziehen ist also gefahrlos.
  `--overwrite` wirft die Datei weg und fängt neu an, wenn das wirklich gemeint ist.
- Die Datei darf von Hand bearbeitet werden; unbekannte Schlüssel ignoriert der Loader (ein
  `_comment` zur Herkunft einer Vorlage darf also darin stehen bleiben).

- **An einen Datensatz gebunden.** Bezugspunkte nennen konkrete Bild-IDs, deshalb wird eine
  Vorlage nur in ein Projekt kopiert, das *ihren* Datensatz geöffnet hat (`dataset` in der
  Datei). Eine Vorlage für einen Datensatz, den der Server nicht hat, bleibt einfach ungenutzt.
- **Einmal je Person und Datensatz**, und nur solange dort noch keine Notiz liegt. Wer das
  Beispiel löscht, bekommt es nicht wieder vorgesetzt.
- **Nur im Demo-Modus.** Auf einer internen Instanz gehört die Notizliste den Leuten, die darin
  arbeiten.
- Der **Anforderungskatalog** braucht keine Vorlage: der schreibgeschützte Master
  (`requirements.toml`) gilt ohnehin für alle, bis jemand ihn bearbeitet.

Die Sandbox-Bereinigung läuft träge beim Request (höchstens alle 15 Minuten), es braucht also
keinen Cronjob. Gelöscht wird eine Gast-Sandbox erst, wenn ihr **jüngster** Eintrag älter als
`GUEST_TTL_HOURS` ist; Konten werden nie bereinigt.

## Tracking-Modus (Usability-Sitzungen)

Für begleitete Usability-Sitzungen kann die App die **Klickstrecke** aufzeichnen: was angeklickt
wurde, welche Einstellung sich wie geändert hat, welche Texte eingegeben wurden. Das beantwortet,
was die Request-Logs nicht können — ein Klick auf „Abbrechen" erzeugt gar keinen Request, und ein
Filter-Request sagt nicht, welches Häkchen ihn ausgelöst hat.

**Einschalten in drei Stufen** — ohne die erste existiert das Feature nicht:

1. **Verfügbar machen** (Konfiguration): `[tracking] enabled = true` in `xai_viewer/config.toml`
   oder `XAI_TRACKING=1` in der Umgebung. Ohne das enthält die ausgelieferte Seite keinerlei
   Tracking-Code und `/track` antwortet 404.
2. **Scharfschalten** (je Sitzung, von Hand): **Strg+Alt+Umschalt+T**. Der Shortcut wirkt auf
   jeder Seite, also auch schon vor der Anmeldung der Testperson.
3. **Erkennbar** am gedimmten **„(R)"** hinter dem Produktnamen. Das erscheint erst im
   Hauptbereich (ab offenem Projekt) — im Anmeldebereich gibt es keinen Hinweis, weil dort auch
   nichts aufgezeichnet wird.

Dieselbe Tastenkombination schaltet wieder aus; die Aufzeichnung endet außerdem mit dem Browser.

**Was aufgezeichnet wird:** Klicks (mit Beschriftung, Panel und Dialog), Änderungen an Feldern
inklusive eingegebener Texte, die App-Shortcuts, die ausgelösten HTMX-Requests sowie Seitenaufrufe
— jeweils mit Zeitstempel, Anzeigename, Projekt und Datensatz. **Nicht** aufgezeichnet werden
Passwörter (doppelt abgesichert: die Insel liest solche Felder nicht, der Server verwirft den Wert
noch einmal), alles außerhalb des Hauptbereichs und das Scrollen.

**Wo es landet:** eine JSONL-Datei je Tracking-Sitzung unter `tracking_dir` bzw.
`XAI_TRACKING_DIR`, per Default `xai_viewer/tracking/` (nicht eingecheckt). Achtung: dieser
Default liegt **im Deployment-Baum** und wäre bei `deploy.py --purge` weg — auf einer Instanz, die
wirklich Sitzungen aufzeichnet, das Verzeichnis nach außen legen. `deploy.py` sichert es im Backup
mit und lädt es nie hoch.

**Datenschutz:** Ist der Schalter gesetzt, zeigt `/privacy` automatisch eine angepasste Fassung
plus einen eigenen Abschnitt zur Aufzeichnung. Die **öffentliche Demo läuft ohne den Schalter** —
nur deshalb bleibt dort die Aussage „keine Analyse- oder Tracking-Werkzeuge" wahr. Beides
gleichzeitig zu konfigurieren ist möglich, die App warnt dann beim Start; gedacht ist es nicht.

### Auswerten

```bash
python tools/analyze_tracking.py                    # vorhandene Aufzeichnungen auflisten
python tools/analyze_tracking.py <session-id>       # Klickstrecke mit Verweildauern
python tools/analyze_tracking.py <session-id> --summary    # nur die Kennzahlen
python tools/analyze_tracking.py --dir /pfad/zu/tracking   # anderer Ablageort
```

Ausgegeben werden die Klickstrecke in Klartext (mit der Dauer bis zur nächsten Aktion), die
Dauer je Dialog-Besuch, die Verteilung der Ereignisarten und die häufigsten Klicks.

**Zu den Verweildauern:** aufgezeichnet sind Zeitpunkte, keine Dauern — jede Dauer ist der
Abstand zum *nächsten* Ereignis. Das misst gut, wie lange jemand vor einem Schritt saß, und gar
nicht, was die Aufzeichnung nicht sehen kann: wer weggeht, erzeugt dieselbe Lücke wie wer
nachdenkt. Pausen über 120 s werden deshalb getrennt als *idle* ausgewiesen und nicht in die
aktive Zeit gezählt. Die letzte Aktion vor dem Ausschalten hat keinen Nachfolger und damit keine
Dauer. Gerechnet wird mit dem Browser-Zeitstempel (`ts`), nicht mit dem des Servers — der misst
die Zustellung, nicht die Person.

## Schutz vor Cross-Site-Requests (CSRF)

**Wogegen.** Ein Browser hängt das Session-Cookie automatisch an *jeden* Request an die App —
auch an einen, den eine ganz andere Seite ausgelöst hat. Eine fremde Seite (oder eine Mail mit
HTML) könnte also ein Formular enthalten, das unbemerkt `POST /notes/5/delete` an unseren Server
schickt. Ist der Besucher gerade angemeldet, führt die App die Aktion in seinem Namen aus. Das
ist *Cross-Site Request Forgery*: nicht der Angreifer schickt den Request, sondern der Browser
des Opfers.

**Wie es hier gelöst ist.** Der übliche Weg ist ein Zufalls-Token in jedem Formular, das der
Server gegenprüft. Dieses Projekt nimmt stattdessen zwei einfachere Lagen, die zusammen dasselbe
leisten und ohne Änderung an Formularen und HTMX-Aufrufen auskommen:

1. **`SameSite=Lax` am Session-Cookie.** Der Browser hängt das Cookie an einen POST, der von
   einer fremden Seite kommt, gar nicht erst an. Der Request kommt also *unangemeldet* an und
   kann nichts ändern.
2. **Herkunftsprüfung** (`reject_cross_site_writes` in `app.py`). Bei jedem schreibenden Request
   (POST) wird der vom Browser mitgeschickte `Origin`- bzw. `Referer`-Header gegen den eigenen
   Host geprüft. Passt er nicht, antwortet die App mit **403 „Cross-site request rejected"**.
   Das deckt ab, was Lage 1 nicht sieht: alte Browser und Seiten auf derselben Site, aber
   anderem Origin.

Lesende Requests (GET) werden nie blockiert.

**Warum Requests ganz ohne diese Header durchgelassen werden.** Ein Browser sendet bei einem
Cross-Site-POST immer einen der beiden Header und lässt sich nicht dazu bringen, sie
wegzulassen — fehlen sie, kommt der Request also nicht aus einem fremden Browser-Tab. Wer sie
weglassen *kann* (curl, ein Skript), hat kein fremdes Session-Cookie und damit kein Opfer.
Nebeneffekt: serverseitige Werkzeuge und die Testsuite laufen ohne Token-Verdrahtung.

**Im Fehlerfall.** Antwortet nach einem Deploy **jede** Aktion mit 403 „Cross-site request
rejected", während Seiten sich normal laden, dann reicht der Webserver den Host-Header nicht
unverändert an die App durch — sie vergleicht dann die echte Adresse mit einer falschen. Abhilfe:
die öffentliche Adresse in der Deployment-`config.toml` eintragen und neu starten.

```toml
ALLOWED_ORIGINS = "https://demo.example.org"
```

(Mehrere durch Komma getrennt; lokal wird das nie gebraucht.)

## Footer und Versionsnummer

Alle Seiten (außer dem Prüfbericht) tragen einen Footer mit Produktname, Version,
Impressum/Datenschutz/Kontakt und dem Deployment-Zeitstempel. Er liegt **bewusst unterhalb der
Falz**: Beim Öffnen einer Seite ist er nie sichtbar, man scrollt einmal nach unten. Jede Seite ist
dadurch scrollbar, auch kurze — siehe `.page-fold` in `static/css/style.css`.

- **Version:** `xai_viewer/version.py` (`__version__`), von Hand gepflegt — bei
  benutzersichtbaren Änderungen hochzählen.
- **Deployment-Zeitstempel:** aus `deployment_date.txt` im Projektverzeichnis, geschrieben von
  `deployment/deploy.py`. Auf einer Arbeitskopie fehlt sie → im Footer steht „local dev system".

## Impressum, Datenschutz, Kontakt (`xai_viewer/legal.toml`)

Was **der Betreiber** einer Instanz angeben muss — Impressum, Verantwortlicher,
Datenschutzbeauftragte:r, Rechtsgrundlage, Beschwerdestelle, Kontakt-Link in Footer und Login —
steht **nicht** in den i18n-Katalogen, sondern in einer eigenen Datei je Instanz:
`XAI_LEGAL_FILE` → `[paths] legal_file` in `config.toml` → `xai_viewer/legal.toml`. Die Kataloge
enthalten nur, was die *App* tut (Cookie, gespeicherte Inhalte, Löschfrist, keine Dritten,
Tracking) — das muss dem Code folgen, nicht dem Betreiber.

- **Vorlage:** `xai_viewer/legal.example.toml` nach `legal.toml` kopieren und ausfüllen. Die Datei
  liegt im Deployment-Baum und wird von `deploy.py` mit hochgeladen.
- Solange in einem Abschnitt noch ein `[Platzhalter]` steht, zeigt die Seite einen Entwurfshinweis.
- Fehlt die Datei, antworten `/imprint` und `/privacy` mit „nicht konfiguriert", die Kontakt-Links
  entfallen, und die App warnt beim Start.

## Tests

```bash
cd xai_viewer
python -m pytest tests/                  # Backend + Inseln
python -m pytest tests/test_backend.py   # nur Backend
```

Die Backend-Tests laufen gegen die `datasets/mock`-Fixture. `pytest.ini` deaktiviert global
installierte, projektfremde pytest-Plugins (randomly/django/dash/langsmith), deren
Teardown-Hooks die Collection in manchen Umgebungen (z. B. conda-base) stören.

### Insel-Tests (JS) – brauchen Node, sonst werden sie geskippt

`tests/test_islands.py` führt zusätzlich das **ausgelieferte Inline-JS** („Inseln" in den
Templates) in Node gegen einen DOM-Stub aus: Scroll-Kopplung, Pfeiltasten-Stepping,
Link-Toggle und den Konfig-Dialog. Das schließt eine Lücke, die die Backend-Tests
prinzipiell haben – die rendern Markup, führen es aber nie aus (Anlass war ein Bug, bei dem
das Markup korrekt und die Suite grün war, das JS aber tot).

- **Ohne installiertes `node` werden diese Tests stillschweigend übersprungen** – die Suite
  bleibt grün (die Insel-Tests erscheinen als *skipped*). **Node ist keine Voraussetzung**, um am Projekt zu
  arbeiten oder die Backend-Tests zu fahren.
- Ob und warum geskippt wurde, zeigt `python -m pytest tests/ -rs`.
- Kein npm, kein `package.json`, kein jsdom, kein Browser – nur Node und sein eingebautes `vm`.
- Geprüft wird die **JS-Logik**, nicht Rendering, Layout oder echte Events.

Details, Grenzen und wie man eine Insel ergänzt: `tests/islands/README.md`.

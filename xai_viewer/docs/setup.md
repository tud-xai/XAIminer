# XAIminer – Installation und Inbetriebnahme

Diese Anleitung führt von einem frischen Klon bis zur laufenden Anwendung im Browser. Sie
richtet sich an Personen, die XAIminer **lokal betreiben** oder einen **eigenen Datensatz**
einbinden wollen. Für den Betrieb auf einem Server siehe Abschnitt
[Betrieb auf einem Server](#9-betrieb-auf-einem-server).

Die Bedienung der Anwendung selbst beschreibt [`usage.md`](usage.md).

> **Maßgebliche Quellen.** Diese Anleitung ist ein geführter Einstieg. Bei Abweichungen gelten
> [`BETRIEB.md`](../../BETRIEB.md) (Skripte, Betrieb) und [`DATA.md`](../../DATA.md)
> (Datensatz-Layout, Dateiformate, `metadata.json`-Schema).

---

## Inhalt

1. [Anforderungen](#1-anforderungen)
2. [Repository und Python-Umgebung](#2-repository-und-python-umgebung)
3. [Konfiguration (`config.toml`)](#3-konfiguration-configtoml)
4. [Datensatz vorbereiten](#4-datensatz-vorbereiten)
5. [Metadaten erzeugen (`metadata.json`)](#5-metadaten-erzeugen-metadatajson)
6. [Thumbnails vorab erzeugen (empfohlen)](#6-thumbnails-vorab-erzeugen-empfohlen)
7. [Anwendung starten und prüfen](#7-anwendung-starten-und-prüfen)
8. [Optionale Einrichtung](#8-optionale-einrichtung)
9. [Betrieb auf einem Server](#9-betrieb-auf-einem-server)
10. [Fehlerbehebung](#10-fehlerbehebung)

---

## 1. Anforderungen

**Software**

| Was | Version / Hinweis |
|---|---|
| Python | **≥ 3.11** (wegen `tomllib`) |
| Python-Pakete | `flask`, `pillow`, `xhtml2pdf`, `markdown` — siehe `xai_viewer/requirements.txt` |
| Browser | aktueller Desktop-Browser (Firefox, Chrome/Chromium, Edge, Safari) |
| git | zum Klonen |
| Node.js | **optional**, nur für die JS-Insel-Tests; ohne Node werden sie übersprungen |

Es gibt **keinen Build-Schritt**: kein npm, kein Bundler. Bootstrap, Bootstrap-Icons und HTMX
liegen im Repo unter `xai_viewer/static/vendor/` und werden von der Anwendung selbst
ausgeliefert.

**Hardware / Umgebung**

- **Bildschirmbreite ≥ 768 px.** Darunter blendet die Anwendung bewusst einen Hinweis ein statt
  des Arbeitsbereichs — der Panel-Vergleich nebeneinander braucht Platz. Empfohlen ist ein
  Desktop- oder Laptop-Bildschirm (Full HD oder mehr).
- **Speicherplatz** für den Datensatz: der Projektdatensatz `RailPer` belegt rund 1 GB
  (Originale, XAI-Renderings, Thumbnails). Er wird nicht mit dem Repository verteilt.

**Daten**

Die Anwendung bringt **keine Bilddaten** mit. Sie braucht mindestens einen Datensatz im
unten beschriebenen Layout — entweder einen eigenen bzw. gelieferten (z. B. `RailPer`, nicht öffentlich) oder den
synthetischen Testdatensatz `mock`, den ein Skript erzeugt (siehe
[Abschnitt 4.4](#44-ohne-echte-daten-ausprobieren-mock-datensatz)).

---

## 2. Repository und Python-Umgebung

```bash
git clone <repo-url> interaktionssystem
cd interaktionssystem
```

Eine eigene virtuelle Umgebung ist empfehlenswert, aber nicht Pflicht — jede Umgebung mit
Python ≥ 3.11 und den Paketen aus `requirements.txt` genügt.

```bash
python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r xai_viewer/requirements.txt
```

`xhtml2pdf` wird nur für den PDF-Download des Prüfberichts gebraucht. Fehlt es, läuft die
Anwendung trotzdem; der PDF-Knopf meldet dann, dass der Export nicht verfügbar ist (Drucken
aus dem Browser funktioniert weiterhin).

**Alle weiteren Befehle** in dieser Anleitung werden aus dem Verzeichnis `xai_viewer/` heraus
ausgeführt:

```bash
cd xai_viewer
```

---

## 3. Konfiguration (`config.toml`)

```bash
cp config.toml.example config.toml
```

Die Datei ist gitignored — sie enthält maschinenspezifische Pfade. Für den Einstieg sind nur
zwei Einträge relevant:

```toml
[paths]
# Wo die Datensätze liegen. Ohne Eintrag: ../../datasets relativ zu xai_viewer/,
# also ein Verzeichnis "datasets" NEBEN dem Repo.
# datasets_root = "/pfad/zu/datasets"

[dataset]
# Vorauswahl im Projektdialog. Muss ein vorhandener Datensatz sein.
default = "RailPer"
```

Alle übrigen Schalter (Demo-Modus, Tracking, Symlinks …) sind in
`config.toml.example` kommentiert und werden in [Abschnitt 8](#8-optionale-einrichtung) kurz
eingeordnet.

> **Nach `git worktree add`** muss die `config.toml` ebenfalls neu angelegt bzw. aus der
> bestehenden Arbeitskopie kopiert werden — sie wandert als gitignorierte Datei nicht mit.

---

## 4. Datensatz vorbereiten

### 4.1 Wo der Datensatz liegt

Datensätze liegen **außerhalb des Repos** und werden nie eingecheckt. Ohne eigene Angabe
in der `config.toml` erwartet die Anwendung folgende Anordnung:

```
<übergeordnetes Verzeichnis>/
├── interaktionssystem/        # dieses Repo
│   └── xai_viewer/
└── datasets/                  # datasets_root (Default)
    ├── RailPer/
    ├── mock/
    └── <weiterer Datensatz>/
```

Jedes Unterverzeichnis von `datasets/`, das eine `metadata.json` enthält, erscheint im
Projektdialog der Anwendung als wählbarer Datensatz.

### 4.2 Layout eines Datensatzes

```
datasets/<name>/
├── originals/
│   ├── imgs/                   # PFLICHT: die Bilder (<basename>.jpg | .png)
│   ├── all_labels.csv          # PFLICHT: image_name,label  → wahre Klasse
│   ├── annots/                 # optional: Bounding-Boxen (OSDaR-artig)
│   ├── manual_labels.json      # optional: Handlabels (Filter-Attribute)
│   └── scene_attributes.csv    # optional, nur synthetische Datensätze
├── xai/
│   └── <Modell>/<Methode>/<Stufe>/<basename><anhang>.{jpg|png}
├── inference/
│   └── inference_results_<Modell>_<Stufe>.csv
├── model_meta.csv              # optional: Modell-Metadaten für den Modellüberblick
└── metadata.json               # wird in Schritt 5 ERZEUGT
```

Was die einzelnen Teile in der Anwendung bewirken:

| Quelle | Pflicht? | Wirkung in der Anwendung |
|---|---|---|
| `originals/imgs/` | **ja** | die Bilder in Galerie und Einzelbildansicht |
| `originals/all_labels.csv` | **ja** | wahre Klasse (Ground Truth) je Bild |
| `inference/*.csv` | praktisch ja | Vorhersage + Konfidenz je Modell/Stufe; ohne sie gibt es keine Klassifikationsfilter und keine Konfusionsmatrix |
| `xai/…` | praktisch ja | die Erklärungsbilder; fehlt eine Kombination, zeigt das Panel das Original mit dem Hinweis „kein XAI" |
| `model_meta.csv` | nein | Block „Modell-Metadaten" im Modellüberblick; ohne sie steht dort „N/A" |
| Handlabels / `scene_attributes.csv` | nein | echte Werte für die Filter Umgebung, Objekte, Tageszeit, Wetter (und bei synthetischen Daten die Distanzen); ohne sie werden diese Attribute **gemockt** |

**Wichtige Konventionen** (Details und alle Dateiformate: [`DATA.md`](../../DATA.md)):

- **Verknüpfung über den Basisnamen ohne Endung.** `img_0042.jpg` in `imgs/`, die Zeile
  `img_0042` in `all_labels.csv`, `img_0042_gradcam.png` unter `xai/` und die Zeile in der
  Inference-CSV gehören zusammen.
- **Modelle, XAI-Methoden und Stufen werden aus dem Dateisystem erkannt**, nicht aus einer
  festen Liste: jedes Verzeichnis `xai/<Modell>/<Methode>/<Stufe>/` mit mindestens einem Bild
  zählt. Namen sind überall identisch (Verzeichnis = Anzeige), z. B. `VGG16`, `Grad-CAM`,
  `high`.
- **Stufen** werden in der Reihenfolge `low` → `mid`/`middle` → `high` sortiert.
- **Klassifikation ist binär**: `person` / `not person` (auch `no person` wird akzeptiert).
- **CRP und CRAFT** haben eine eigene Unterstruktur (Konzept-Heatmaps, globale
  Konzept-Prototypen, `concepts.csv`) — siehe [`DATA.md`](../../DATA.md), Abschnitte
  „CRP-Konzept-Struktur" und „CRAFT-Struktur".

### 4.3 Minimalbeispiel für einen eigenen Datensatz

```
datasets/MeinDatensatz/
├── originals/
│   ├── imgs/
│   │   ├── bild_001.jpg
│   │   └── bild_002.jpg
│   └── all_labels.csv
├── xai/
│   └── ResNet50/
│       └── Grad-CAM/
│           └── high/
│               ├── bild_001_gradcam.jpg
│               └── bild_002_gradcam.jpg
└── inference/
    └── inference_results_ResNet50_high.csv
```

`originals/all_labels.csv`:

```csv
image_name,label
bild_001.jpg,person
bild_002.jpg,not person
```

`inference/inference_results_ResNet50_high.csv` (`sigmoid_output` = Wahrscheinlichkeit für
„Person"):

```csv
image_name,sigmoid_output,classification,ground_truth
bild_001.jpg,0.9821,person,person
bild_002.jpg,0.4130,not person,not person
```

### 4.4 Ohne echte Daten ausprobieren (`mock`-Datensatz)

Für einen ersten Test oder für die Entwicklung erzeugt ein Skript einen winzigen,
synthetischen Datensatz im exakt gleichen Layout (8 Bilder, drei Modelle, drei Stufen,
Grad-CAM sowie CRP und CRAFT für VGG16/high):

```bash
python tools/make_mock_dataset.py          # schreibt <datasets_root>/mock/
python tools/build_metadata.py mock
```

Die automatischen Tests laufen gegen genau diesen Datensatz.

> Das Skript hat keine Optionen und überschreibt einen vorhandenen `mock`-Datensatz
> ohne Rückfrage.

---

## 5. Metadaten erzeugen (`metadata.json`)

Die Anwendung liest **ausschließlich** die generierte `metadata.json` — nie die
Rohdateien direkt. Sie wird einmal pro Datensatz erzeugt und nach **jeder** Änderung an den
Rohdaten neu:

```bash
python tools/build_metadata.py RailPer
python tools/build_metadata.py MeinDatensatz
python tools/build_metadata.py --help      # alle Optionen
```

Das Skript meldet am Ende, was es gefunden hat (Anzahl Bilder, Modelle, Stufen,
XAI-Verfügbarkeit) und gibt **Warnungen** aus, statt abzubrechen — z. B. wenn die wahre
Klasse in der Inference-CSV von `all_labels.csv` abweicht (dann gilt `all_labels.csv`). Diese
Warnungen lohnt es sich zu lesen.

**Handlabels für die Filter-Attribute.** Ohne Handlabels sind Umgebung, Objekte, Tageszeit
und Wetter **gemockt** (deterministische Zufallswerte je Bild) — die Filter funktionieren
dann technisch, filtern inhaltlich aber nach Zufall. Echte Werte liest das Skript aus der
**ersten** vorhandenen dieser Quellen:

| # | Quelle | typischer Fall |
|---|---|---|
| 1 | `--labels PFAD` | eine Label-Datei (Export), die jemand geschickt hat |
| 2 | `<datensatz>/originals/manual_labels.json` | Kopie, die mit dem Datensatz mitreist |


> **Nach einem Neubau der `metadata.json` die Anwendung neu starten** — die Datei wird im
> laufenden Prozess zwischengespeichert.

---

## 6. Thumbnails vorab erzeugen (empfohlen)

Die Galerie zeigt kleine WebP-Vorschaubilder (320 × 180) statt der Originale. Fehlende
Vorschaubilder erzeugt die Anwendung zwar beim ersten Aufruf selbst, bei großen Datensätzen
wartet dann aber die erste Person spürbar. Deshalb vorab:

```bash
python tools/make_thumbnails.py RailPer
python tools/make_thumbnails.py RailPer --force   # alle neu erzeugen
```

Das Ergebnis liegt unter `<datensatz>/thumbs/`. Der Lauf ist idempotent (vorhandene
Thumbnails werden übersprungen) und kann bei zehntausenden Bildern lange dauern.

---

## 7. Anwendung starten und prüfen

```bash
flask --app app run
```

Dann im Browser <http://127.0.0.1:5000> öffnen.

**Kurzer Funktionstest:**

1. Die Anmeldeseite erscheint. Unter „Als Gast anmelden" einen beliebigen Namen eingeben.
2. Rolle **Validierer** wählen.
3. Im Projektdialog den Datensatz wählen und **Starten**.
4. Der Konfigurationsdialog von Panel P1 öffnet sich automatisch → **Anwenden**.
5. Die Galerie zeigt Bilder. Stellt man unter „Anzeige" eine XAI-Methode ein, erscheinen
   deren Renderings (und nicht überall „kein XAI").

Wie es von dort weitergeht: [`usage.md`](usage.md).

**Tests** (optional, aus `xai_viewer/`):

```bash
python -m pytest tests/
```

---

## 8. Optionale Einrichtung

Die folgenden Punkte sind für den lokalen Einstieg **nicht** nötig. Jeweils nur die
Einordnung — die Details stehen in [`BETRIEB.md`](../../BETRIEB.md) im genannten Abschnitt.

| Was | Wofür | Wo einstellen | Abschnitt in `BETRIEB.md` |
|---|---|---|---|
| **Konten** | Personen, die sich mit Passwort anmelden und dauerhaft eigene Daten haben | `python tools/manage_accounts.py add <name>` | „Nutzer und Konten" |
| **Demo-Modus** | öffentliche Instanz: Gäste bekommen Wegwerf-Sandboxen, interne Werkzeuge sind gesperrt, Mengengrenzen greifen | `[demo] enabled = true` oder `XAI_DEMO_MODE=1` | „Demo-Modus" |
| **Bilder ganz vorn** | für Vorführungen bestimmte Bilder ohne Scrollen erreichbar machen | `featured_images.toml` | „Bestimmte Bilder ganz vorn" |
| **Anforderungskatalog** | Vorgabe der prüfbaren Anforderungen für den Prüfbericht | `requirements.toml` bzw. `paths.requirements_file` | — (siehe Kommentar in `config.toml.example`) |
| **Symlinks im Datensatz** | XAI-Renderings liegen z. B. auf einem gemounteten Laufwerk und sind nur verlinkt | `[dev] follow_dataset_symlinks = true` (nur lokal) | „Ersteinrichtung" |
| **Tracking-Modus** | Klickstrecke in begleiteten Usability-Sitzungen aufzeichnen | `[tracking] enabled = true` | „Tracking-Modus" |

**Zugangsarten im Überblick:**

- **Gast** — beliebiger Name, kein Passwort. Außerhalb des Demo-Modus sind Notizen und
  Sammlungen an diesen Namen gebunden; im Demo-Modus bekommt jeder Gast eine private Sandbox,
  die nach 48 h Inaktivität gelöscht wird.
- **Konto** — Name und Passwort, vorab per `tools/manage_accounts.py` angelegt. Kein
  Selbstregistrieren, kein Passwort-Reset.

---

## 9. Betrieb auf einem Server

Das Repo enthält ein Deployment-Skript für einen Uberspace-Account (gunicorn unter
supervisord). Ablauf, Konfiguration (`config-example.toml` im Repo-Wurzelverzeichnis) und
Stolperfallen stehen in [`deployment/README.md`](../../deployment/README.md) und im
[`BETRIEB.md`](../../BETRIEB.md), Abschnitt „Deployment". Die wichtigsten Punkte:

- **Datensätze** werden separat hochgeladen (`deploy.py --upload-datasets`); vorher lokal
  Thumbnails erzeugen.
- **Nutzerdaten** (Notizen, Sammlungen, Labels, eigene Anforderungskataloge) werden nie
  hochgeladen und vor jedem Deploy gesichert.
- **Zugriffsschutz**: interne Instanz → HTTP Basic Auth; öffentliche Demo → kein Basic Auth,
  stattdessen Demo-Modus. Beides zusammen ist nicht vorgesehen.
- **`SECRET_KEY`** setzen, bei HTTPS **`COOKIE_SECURE = true`**.
- **Konten** werden **auf dem Server** angelegt (`accounts.toml` wird nicht übertragen).

---

## 10. Fehlerbehebung

| Symptom | Ursache | Abhilfe |
|---|---|---|
| Seite „Einrichtung erforderlich – Datensatz-Metadaten fehlen" | `metadata.json` des gewählten Datensatzes fehlt | den auf der Seite angezeigten Befehl ausführen (`python tools/build_metadata.py <name>`), Seite neu laden |
| Projektdialog: „Kein Datensatz gefunden" | kein Verzeichnis unter `datasets_root` enthält eine `metadata.json` | `datasets_root` in `config.toml` prüfen, `build_metadata.py` ausführen |
| `build_metadata.py`: „Pflicht-Quelle fehlt" | `originals/imgs/` oder `originals/all_labels.csv` fehlt | Datensatz-Layout prüfen (Abschnitt 4.2) |
| Originale erscheinen, XAI-Bilder antworten mit **403** | XAI-Renderings sind Symlinks auf ein anderes Laufwerk | `[dev] follow_dataset_symlinks = true` in `config.toml` (typisch nach `git worktree add`: `config.toml` fehlt) |
| Überall „kein XAI" | für die gewählte Kombination Modell/Stufe/Methode fehlen Renderings, oder die Dateinamen passen nicht zum Basisnamen | Verzeichnis `xai/<Modell>/<Methode>/<Stufe>/` prüfen; `build_metadata.py` neu laufen lassen |
| Filterwerte scheinen „zufällig" | Filter-Attribute sind gemockt (keine Handlabels) | Handlabels einbinden (Abschnitt 5) |
| Änderungen an Daten, `featured_images.toml` o. Ä. wirken nicht | Werte werden beim Start eingelesen | Anwendung neu starten |
| Galerie lädt beim ersten Mal sehr langsam | Thumbnails werden erst beim Aufruf erzeugt | `tools/make_thumbnails.py` vorab ausführen |
| Hinweis „Bildschirm zu schmal" | Fensterbreite < 768 px | Fenster vergrößern / Desktop-Rechner verwenden |
| Nach Deploy antwortet jede Aktion mit 403 „Cross-site request rejected" | Proxy reicht den Host-Header nicht durch | `ALLOWED_ORIGINS` in der Deployment-`config.toml` setzen (README, Abschnitt „CSRF") |

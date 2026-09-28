# Datenreferenz — Datensätze, Roh-Quellen, `metadata.json`

Referenz für das Datenmodell der App: Layout eines Datensatzes, die Roh-Quellformate,
die **kanonischen Namen** und das generierte `metadata.json`. **Diese Datei ist die maßgebliche Quelle**.

Ergänzend: `BETRIEB.md` (Setup, Pipeline-Skripte).
Bei Widerspruch zwischen Doku und Code gilt der Code — die Skripte `tools/build_metadata.py`
(Reader/Generator) und `tools/make_mock_dataset.py` (Fixture) sind die operative Wahrheit.

## Wo die Daten liegen

Bilder und Metadaten liegen **außerhalb** dieses Repos, je Datensatz unter
`<DATASETS_ROOT>/<name>/` (Default `../datasets/`, via `config.toml` → `datasets_root`
überschreibbar). Der Datensatz ist **projektglobal** (ein Datensatz pro Analyseprojekt).
Die App liest **ausschließlich** die generierte `metadata.json` — nie die Roh-Quellen direkt.

## Datensatz-Layout

```
<DATASETS_ROOT>/<name>/
├── originals/
│   ├── imgs/                 # <basename>.jpg | .png   (die tatsächlichen Bilder)
│   ├── annots/               # <basename>.json         (OSDaR-artig: bbox + class)
│   ├── all_labels.csv        # image_name,label        (maßgeblich für true_class)
│   ├── manual_labels.json    # OPTIONAL: Handlabels (siehe unten)
│   └── scene_attributes.csv  # OPTIONAL, nur synthetisch: echte Szenenfakten
├── xai/
│   └── <Model>/<Method>/<level>/   # <basename><appendix>.{jpg|png}
│       # Ausnahme CRP: kein Ein-Bild-Overlay, sondern concept_heatmaps/ (pro Bild) +
│       # concepts/ (globale Prototypen) + eine concepts.csv je Level (siehe unten).
│       # Ausnahme CRAFT: Per-Bild-Map in concept_attribution_maps/ + globale
│       # Konzept-Prototypen in concepts/ (siehe unten).
│       # Synthetische Datensätze können hier zusätzlich Scene/Mask/Depth führen: keine
│       # Erklärungen, sondern die Renderings des Generators (siehe unten).
├── inference/
│   └── inference_results_<Model>_<level>.csv   # eine CSV je (Modell, Stufe); auch "-<level>"
├── model_meta.csv            # OPTIONAL – Modell-Metadaten, eine Zeile je (Modell, Stufe)
└── metadata.json             # GENERIERT – einziger Lese-Eingang der App
```

Die Mock-Fixture (`tools/make_mock_dataset.py`) folgt exakt demselben Layout unter `datasets/mock/`.

## Kanonische Namen

**Kanonisch = realer Name, überall identisch** (Verzeichnis, JSON-Key, App-State, CSV,
Anzeige). Damit entfällt fast jede Übersetzung.

| Konzept | Kanonischer Name (= überall)                         | Anzeige            |
|---------|------------------------------------------------------|--------------------|
| Modell  | `VGG16`, `ResNet50`, `ConvNeXt-T`                     | identisch          |
| XAI     | `Grad-CAM`, `LRP`, `CRAFT`, `CRP`                     | identisch          |
| Stufe   | `low`, `mid`, `high`                                  | identisch          |
| Klasse  | Token `person` / `not_person`                        | `Person` / `No person` |

- **Stufe heißt `mid` oder `middle`** — beide Schreibweisen kommen in gelieferten Daten vor
  (`mid` synthetisch, `middle` bei RailPer) und stehen beide in `LEVEL_ORDER`; ohne das würde
  `high` vor die mittlere Stufe sortieren.
- **Inference-Dateiname mit Unterstrich *oder* Bindestrich** zwischen Modell und Stufe:
  `inference_results_ConvNeXt-T_mid.csv` bzw. `…-middle.csv` (RailPer liefert so). Der
  Modellname kann selbst einen Bindestrich enthalten (`ConvNeXt-T`) → der Pfad wird
  **vorwärts konstruiert** (beide Trennzeichen probiert), nie per `-`/`_`-Split geparst.
- XAI-Verfügbarkeit wird **aus dem Dateisystem** ermittelt (welche `<Model>/<Method>/<level>/`
  Verzeichnisse mind. ein Bild enthalten — **oder** bei CRP: eine `concepts.csv`), nicht aus
  einer festen Liste. Real vorhanden: `Grad-CAM`, `LRP` (Overlay-Bilder), `CRP` (Konzept-
  Struktur) und `CRAFT` (Attribution-Map + globale Konzepte), letztere beide nur VGG16/high.
  `models`/`levels` werden aus dieser Verfügbarkeit abgeleitet.

## Zweiter Datensatz: `SynthRail-v3` (synthetisch, Clever-Hans)

Neben `RailPer` (echte Fotos) gibt es einen synthetischen Datensatz aus dem (nicht
öffentlichen) Generator `xai-rail-synth`: prozedural gerenderte Bahnszenen mit einer künstlichen
Scheinkorrelation **Gebäude ↔ Person**. Zwei Modelle je Architektur — `trap` (mit
Korrelation P=0,97 trainiert) und `clean` (P=0,5) — machen sichtbar, worauf eine Erklärung
zeigt, wenn ein Modell die Abkürzung gelernt hat.

**Achsen-Belegung (weicht bewusst ab):**

| App-Achse | Werte hier | Bedeutung |
|---|---|---|
| Modell | `ResNet18`, `ResNet50` | Architektur |
| Stufe | `trap`, `clean` | Trainings**regime**, *nicht* Trainingsgüte |
| XAI | `Grad-CAM`, `LRP` | LRP = α2β1-Regel (zennit) |

Ansonsten identisches Layout — die App braucht keine Sonderbehandlung.

### `originals/scene_attributes.csv` (nur synthetische Datensätze)

Die **echten** Szenenfakten, die der Generator kennt. `tools/build_metadata.py` liest die Datei,
wo sie existiert, und leitet daraus Filterattribute ab **statt zu mocken**:

```csv
image_name,subset,person_present,building_present,umgebung,wetter,tageszeit,dist_lateral_m,dist_longitudinal_m,hdri,catenary,parallel_track,person_area,sky_blue,sky_white,sky_grey,green
seed13000054,trap,1,1,ueberland,wolken,tag,2.6,8.71,kloofendal_28d_misty_puresky.hdr,0,1,0.0128,0.0,0.54,0.448,0.271
```

| Spalte | Wird in der App zu | Provenance |
|---|---|---|
| `person_present`, `building_present` | `objekte` = `mensch` / `gebaeude` | `scene` |
| `umgebung`, `wetter`, `tageszeit` | gleichnamige Filter | `scene` (leer ⇒ `mock`) |
| `dist_lateral_m`, `dist_longitudinal_m` | die beiden Distanz-Schieber | `scene` (leer ⇒ *unbekannt*) |
| `subset`, `hdri`, `catenary`, `parallel_track` | — (nur Information) | — |
| `person_area`, `sky_*`, `green` | — (Auswahlhilfe für Screenshots) | — |

- **`wetter`/`tageszeit`** setzt der Erzeuger: aus dem HDRI-Namen, wo dieser eindeutig ist
  (`clear`/`noon` → sonne, `cloudy`/`overcast` → wolken, `misty` → wolken, `sunset` →
  gegenlicht), sonst **gemessen am fertigen Bild** — der Diffusions-Pass
  übermalt den Himmel, der HDRI-Name ist dann nur noch die Absicht. `misty` und `sunset`
  werden bewusst nicht wörtlich übernommen (2026-09-18): der Nebel dieser HDRIs ist am Gleis
  nicht sichtbar, und die tiefstehende Sonne macht Gegenlicht, aber keine Dämmerung.
- Ein Token, das nicht in `filter_options.py` steht, wird verworfen (Warnung) und gemockt —
  so kann eine CSV die Filter-UI nicht mit unbekannten Werten fluten. Die 2026-08 umbenannten
  Wetter-Tokens werden vorher übersetzt (`sonne` → `sonnig`, `wolken` → `bewoelkt`,
  `TOKEN_ALIASES`), damit ältere CSVs ihre echten Werte behalten.
- **Maßgeblich ist die Spalte, nicht ihr Inhalt**: Wo eine CSV `umgebung` oder die Distanzen
  führt, gelten deren Werte; eine leere Distanz-Zelle heißt "keine Person in der Szene" und
  bleibt leer, statt einen Platzhalter zu bekommen — der Distanzfilter lässt ein solches Bild
  passieren. CSVs ohne diese Spalten (alles vor 2026-09) mocken sie wie bisher.
- Fehlt die Datei (RailPer), ändert sich nichts — dann wird wie bisher alles gemockt.

`originals/annots/` wird hier aus den **Seg-Masken** abgeleitet (Konzepte `person_body`,
`hi_vis_vest`, `helmet` → eine Bounding-Box je zusammenhängender Fläche), nicht aus einer
Annotation von Hand.

### `Scene` / `Mask` / `Depth`: die Renderings als Pseudo-XAI-Methoden

Synthetische Datensätze können im `xai/`-Baum drei Verzeichnisse führen, die **keine
Erklärungen** enthalten, sondern das, woraus das Bild entstanden ist:

| Methode | Datei | Inhalt |
|---|---|---|
| `Scene` | `<basename>_scene.png` | der rohe Blender-Render, bevor der Diffusions-Pass ihn zum Foto macht |
| `Mask` | `<basename>_mask.png` | die Segmentmaske, farbcodiert nach `manifest.json` → `seg_colors` |
| `Depth` | `<basename>_depth.png` | die Tiefenkarte (8 bit, einkanalig) |

Sie brauchen keine Sonderbehandlung: die App findet sie über dieselbe Konvention
`<basename>_<methode in kleinbuchstaben>.<ext>` wie Grad-CAM, und `build_metadata.py` nimmt
jedes Verzeichnis mit Bildern als Methode auf. Damit stehen sie im Panel-Menü neben den
echten Verfahren und lassen sich gegen eine Heatmap stellen — die Geometrie, die das Bild
erzeugt hat, gegen das, was das Modell daraus gemacht hat.

Inhaltlich hängen sie **weder am Modell noch am Regime**, die Verzeichnisstruktur verlangt
aber beides. Der Erzeuger schreibt die Datei deshalb einmal (unter dem alphabetisch ersten
Paar, `ResNet18/clean`) und **hard-linkt** sie in die übrigen. Hard Links, nicht Symlinks:
Sync-Clients übertragen Symlinks oft nicht, und beim `rsync` zum Deployment kämen sie ohne
`-L` als tote Verweise an. Ein `rsync` ohne `-H` löst die Hard Links allerdings auf und
vervierfacht den Platzbedarf am Ziel.

**Erzeugt** wird der Datensatz von `poc/export_viewer.py` im Repo `xai-rail-synth` (läuft
dort, wo Modellgewichte und Bildpool liegen). Das Skript ist idempotent und
die Bildauswahl ein stabiles Präfix: ein Lauf mit größerem `--n-per-subset` fügt Bilder
hinzu, ohne die vorhandenen neu zu würfeln. Die Auswahl ist über die vier
(Person, Gebäude)-Zellen balanciert — sonst fehlt der aussagekräftigste Fall
(Gebäude ohne Person, im `trap`-Subset nur 12 von 800 Bildern).

## Roh-Quellformate

**`originals/all_labels.csv`** — maßgeblich für `true_class` (Q7):
```csv
image_name,label
RAWPED_set11_V000_I00001.jpg,not person
RAWPED_set11_V001_I00001.jpg,person
```

**`originals/annots/<basename>.json`** — OSDaR-artig; in der Praxis nur `class: "person"`:
```json
{
  "metadata": {"dataset_name": "OSDaR23", "original_image_name": "...png", "scene_name": "..."},
  "annotations": [{"bbox": {"x_min": 1338.3, "y_min": 863.7, "x_max": 1356.3, "y_max": 911.0},
                   "class": "person"}]
}
```

**`inference/inference_results_<Model>_<level>.csv`**:
```csv
image_name,sigmoid_output,classification,ground_truth
railsem19_no_person_rs01012.jpg,0.000007,not person,no person
railgoerl24_..._00000360.jpg,0.998533,person,person
```
- `sigmoid_output` = P(person) → `confidences.person = sigmoid_output`, `not_person = 1 − …`
- `classification` = vorhergesagte Klasse; `ground_truth` = redundante Wahrheit (Kreuzcheck).

**`model_meta.csv`** (optional) — Modell-Metadaten für den Block über der Konfusionsmatrix,
**eine Zeile je (Modell, Stufe)**:
```csv
model,level,params,epochs,trained_at,algorithm,train_dataset,test_dataset,nominal_accuracy
VGG16,low,138357544,5,2026-05-04,SGD (lr=1e-2 momentum=0.9),RailPer-train,RailPer-test,0.72
VGG16,high,138357544,60,2026-05-19,SGD (lr=1e-2 momentum=0.9),RailPer-train,RailPer-test,0.96
```
- `model`/`level` = kanonische Namen (Join-Schlüssel, s. o.); je Kombination genau eine Zeile.
- **Rohwerte, nicht vorformatiert:** `params`/`epochs` als Ganzzahl, `nominal_accuracy` als Bruch
  `0–1`. Die Anzeige (kompakte Millionen `138.4 M`, Prozent `96.0 %`) macht die App.
- `nominal_accuracy` = **Validierungs-Accuracy** des gewählten Checkpoints (Anzeige-Label
  „Validierungs-Accuracy") — bewusst getrennt von der aus der Konfusionsmatrix berechneten Accuracy
  (der großen Zahl auf der Modelcard). Kann in späteren Epochen wieder sinken (Overfitting), daher
  korreliert `epochs` nicht monoton mit dem Wert.
- Konstante Felder (`params`, `algorithm`, Datensätze) **wiederholen sich** je Stufe; variabel sind
  typischerweise `epochs`, `trained_at`, `nominal_accuracy`.
- Leere Zellen sind erlaubt → das jeweilige Feld erscheint als „N/A". Fehlt die ganze Datei, bleibt
  der Block vollständig „N/A" (keine Pflichtquelle).

### CRP-Konzept-Struktur (Sonderfall)

CRP (Concept Relevance Propagation) liefert **kein** einzelnes Overlay, sondern je Eingabebild
seine relevantesten **Konzepte**. Layout unter `xai/<Model>/CRP/<level>/`:

```
xai/VGG16/CRP/high/
├── concepts.csv                                 # Manifest (Relevanz-Ranking je Bild) – siehe unten
├── concept_heatmaps/                            # PRO BILD: wo ein Konzept im Bild aktiviert
│   └── <basename>_concept<id>_rank<r>_crp.jpg   #   je (Bild, Konzept), Name trägt id + rank
└── concepts/                                    # GLOBAL: wie ein Konzept generell aussieht
    └── concept<id>_3x3.jpg                       #   ein 3×3-Prototyp-Raster je Konzept (geteilt)
```

- **Zwei Bildtypen je Konzept:** die per-Bild-**Heatmap** (`concept_heatmaps/`, zeigt *wo* im
  konkreten Bild) und das **globale Prototyp-Raster** (`concepts/concept<id>_3x3.jpg`, zeigt *was*
  das Konzept ist — eine gelernte Richtung im Modell, bildunabhängig, daher nur **einmal**
  gespeichert und über alle Bilder geteilt).
- Angezeigt werden trotzdem immer nur die Konzepte **dieses** Bildes (aus `concepts.csv`), nicht
  alle globalen. Die App baut die Pfade direkt aus `image_name` + `concept_id` + `rank`.

**`concepts.csv`** — Relevanz-Ranking je Bild (eine Zeile pro Bild×Konzept). Vorhandensein
dieser Datei ist zugleich das **Erkennungssignal**, dass CRP für dieses Modell/diese Stufe
verfügbar ist (der Level-Ordner enthält keine direkten Bilder, nur Unterordner):

```csv
image_name,rank,concept_id,relevance
RAWPED_set11_V000_I00001,1,155,-0.3197
RAWPED_set11_V000_I00001,2,497,-0.2491
RAWPED_set11_V000_I00001,3,103,-0.0798
```

- `image_name` = Basisname (Join wie überall, Endung ignoriert).
- `concept_id` = Ganzzahl; daraus + `rank` baut die App den Heatmap-Pfad
  `concept_heatmaps/<image_name>_concept<concept_id>_rank<rank>_crp.jpg` und das globale Raster
  `concepts/concept<concept_id>_3x3.jpg`.
- `relevance` = **vorzeichenbehaftet** (nicht 0–1): **positiv** = Evidenz *für* die Vorhersage,
  **negativ** = *gegen*. Angezeigt vorzeichenbehaftet (z. B. „−31,97 %").
- `rank` = **1 = größter |relevance|** (einflussreichstes Konzept, unabhängig vom Vorzeichen).
  Die App nutzt die `rank`-Spalte **wie geliefert** (rechnet nichts neu): Kachel = `rank == 1`,
  Explorer = alle Zeilen in Rank-Reihenfolge.

### CRAFT-Struktur (Sonderfall)

CRAFT ist zweigeteilt und **anders als CRP**. Layout unter `xai/<Model>/CRAFT/<level>/`:

```
xai/VGG16/CRAFT/high/
├── concept_attribution_maps/        # ein Ein-Bild-Overlay je Eingabebild
│   └── <basename>_craft.jpg         # farbcodierte Attribution-Map (zeigt ALLE Konzepte)
├── concepts/                        # GLOBALE Konzept-Prototypen (pro Modell/Stufe, ~5–7)
│   └── concept<N>.jpg               # 3×3-Raster typischer Ausschnitte, farbiger Rahmen
└── concepts.csv                     # pro-Bild-Ranking + Importance + Farbe (siehe unten)
```

- **Attribution-Map** = pro Bild **eine** Datei `<basename>_craft.jpg` (immer `.jpg`) — ein
  normales Overlay wie Grad-CAM/LRP, nur im Unterordner `concept_attribution_maps/`. Vorhandensein
  dieses Unterordners (mit Bildern) ist das **Erkennungssignal** für CRAFT. Die App bindet das
  über `METHOD_SUBDIR` in der Pfadauflösung an.
- **Konzepte** = **global** (dieselben ~5–7 Prototypen für **alle** Bilder eines Modells/Stufe,
  nicht pro Bild wie bei CRP). Farbe + Importance je Konzept kommen aus der `concepts.csv`.

**`concepts.csv`** — pro-Bild-Ranking der Konzepte (eine Zeile pro Bild×Konzept), **eine Datei je
Modell/Stufe** unter `xai/<Model>/CRAFT/<level>/`. **Optional**: fehlt sie, zeigt die App die
Konzepte weiter (ohne Aktivierungs-Reihenfolge/Importance/Farbe).

```csv
image_name,rank,concept_id,activation_score,global_importance,concept_color
railsem19_no_person_rs01012,1,0,14.7183,0.9366,#27cdff
railsem19_no_person_rs01012,2,5,0.0455,0.0001,#ffb91b
railsem19_no_person_rs01012,3,2,0.0307,0.0001,#fa0051
```

- `image_name` = Basisname (Join wie überall, Endung ignoriert).
- `rank` = 1…N je Bild, nach `activation_score` absteigend. Die App sortiert die Chips danach.
- `concept_id` = Ganzzahl, **muss exakt** zur Datei `concepts/concept<id>.jpg` passen (IDs dürfen
  Lücken haben, real z. B. 0,1,2,3,5).
- `activation_score` = **rohe** Aktivierung des Konzepts *in diesem Bild* (nicht 0–1, kann > 1
  sein) — nur zur Reihenfolge; in der App im Chip-Tooltip.
- `global_importance` = globale Wichtigkeit je Konzept, **konstant** pro `concept_id`, Bruch 0–1 —
  im Chip-Label als „Imp X %".
- `concept_color` = **Hex `#RRGGBB`**, **konstant** pro `concept_id` — die **gleiche** Farbe wie
  die Konzept-Region in der Attribution-Map / der Prototyp-Rahmen. Die App zeigt daraus einen
  **Farb-Swatch** je Chip, der Konzept ↔ Map-Region verknüpft. (Ältere Spalte `color` wird auch
  akzeptiert.)
- UTF-8, Komma-getrennt, mit Kopfzeile.

## Join- und Normalisierungsregeln

- **Join immer über den Basisnamen ohne Endung** (A8). `all_labels.image_name` (ohne `.jpg`)
  = imgs-Dateiname = annots-`.json`-Name = inference-`image_name`, alle ohne Endung.
  `.jpg`/`.png` koexistieren; der Join ignoriert die Endung.
- **XAI-Zuordnung per Stem-Präfix-Match** (Q3): Der Original-Basisname steht am Anfang des
  XAI-Dateinamens, optionaler Appendix vor der Endung (Mock: `<basename>_gradcam.<ext>`).
- **Klassen-Normalisierung** (A13): Negativklasse erscheint als `not person` **und**
  `no person` → beim Einlesen beide auf Token `not_person`, `person` → `person`.
  Klassifikation ist strikt **binär**.
- **Widersprüche → Warnung, `all_labels` gewinnt** (Q7): Weicht die aus annots abgeleitete
  Klasse oder der `ground_truth` der Inference-CSV vom `all_labels`-Label ab, gilt `all_labels`
  und es wird eine Warnung ausgegeben (nicht abgebrochen).

## `metadata.json` (generiertes Zielschema)

Top-Level-Keys: `dataset`, `classes`, `class_labels`, `models`, `levels`, `xai_availability`,
`images`, `predictions`, `xai_concepts` (nur bei CRP befüllt, sonst leer), `model_meta`.

```json
{
  "dataset": "mock",
  "classes": ["person", "not_person"],
  "class_labels": {"person": "Person", "not_person": "No person"},
  "models": ["ConvNeXt-T", "ResNet50", "VGG16"],
  "levels": ["low", "mid", "high"],

  "xai_availability": {
    "VGG16":      {"Grad-CAM": ["low", "mid", "high"]},
    "ResNet50":   {"Grad-CAM": ["low", "mid", "high"]},
    "ConvNeXt-T": {"Grad-CAM": ["low", "mid", "high"]}
  },

  "images": [
    {
      "id": "mock_0000",
      "filename": "mock_0000.jpg",
      "true_class": "person",
      "attributes": {
        "umgebung": ["bahnhof"], "objekte": ["mensch", "signal"],
        "tageszeit": ["daemmerung"], "wetter": ["gegenlicht"],
        "dist_lateral_m": 6.9, "dist_longitudinal_m": 372.1
      },
      "provenance": {
        "true_class": "all_labels", "objekte": "mock",
        "umgebung": "mock", "tageszeit": "mock", "wetter": "mock",
        "dist_lateral_m": "mock", "dist_longitudinal_m": "mock"
      }
    }
  ],

  "predictions": {
    "ConvNeXt-T": {
      "low": {"mock_0000": {"predicted_class": "not_person",
              "confidences": {"person": 0.249589, "not_person": 0.750411}}}
    }
  }
}
```

- `id` = **Original-Basisname**; `filename` = real vorhandene Datei (jpg/png).
- `true_class` als **Token**, Anzeige über `class_labels`.
- `xai_availability` verschachtelt: `Modell → Methode → [levels]`.
- `predictions`: `Modell → level → basename → {predicted_class, confidences{person, not_person}}`.
- `xai_concepts` (per-Bild-Ranking): `Modell → Methode → level → basename → [ … ]`, je Bild nach
  `rank` aufsteigend sortiert. Leerer Block, wenn keins vorhanden.
  - **CRP:** `[{rank, concept_id, relevance}]`. Kachel = `rank == 1` → Heatmap
    `concept_heatmaps/<basename>_concept<concept_id>_rank<rank>_crp.jpg`.
  - **CRAFT:** `[{rank, concept_id, activation_score}]` (aus `concepts.csv`) — treibt die
    Chip-Reihenfolge in der Einzelansicht.
- `xai_global_concepts` (CRAFT): `Modell → CRAFT → level → [{concept_id, file, color?, global_importance?}]`
  — modellglobale Konzept-Prototypen (nicht pro Bild); `color`/`global_importance` aus der
  `concepts.csv`, fehlen bei älteren Exporten ohne Manifest. Leerer Block, wenn kein CRAFT. (CRPs
  globale Prototypen liegen unter `concepts/concept<id>_3x3.jpg` und werden direkt aus der
  `concept_id` gebaut, ohne eigenen Metadaten-Block.)
- `model_meta`: `Modell → level → {params, epochs, trained_at, algorithm, train_dataset,
  test_dataset, nominal_accuracy}` — aus `model_meta.csv`, Rohwerte (Zahlen als Zahlen). Fehlende
  Felder/Zeilen bzw. eine fehlende Datei ⇒ leerer/teilweiser Block, Anzeige „N/A". Formatierung in
  der App (`_fmt_meta_value`).

### Kategoriewerte sind Listen

**Jede** Kategorie ist mehrwertig: `attributes[<kategorie>]` ist eine **Liste von Tokens**, auch
wo bisher ein einzelner String stand. Die Handlabels haben gezeigt, dass das der Realität
entspricht (semi-urban *und* Bahnübergang, Dämmerung *und* Indoor). Die App liest Attribute über `filter_options.attribute_tokens()`, das ein altes
`metadata.json` mit blanken Strings noch verträgt; neu gebaut wird trotzdem empfohlen.

### Provenance: woher ein Attribut stammt

Drei Quellen, in dieser Vorrangfolge pro Bild:

| Quelle | gilt für | Provenance |
|---|---|---|
| Handlabels (drei mögliche Fundorte, s. u.) | **alle** Kategorien | `manual` |
| `originals/scene_attributes.csv` | jede Spalte, die sie führt | `scene` |
| Fallback | alles Übrige | `mock` |

Was keine Quelle hat, wird **deterministisch pro Basisname gemockt** (Seed = Basisname) und als
`mock` markiert. Bei Handlabels bleiben die **Distanzen immer gemockt** — niemand labelt sie
von Hand; ein Generator, der die Szene kennt, kann sie dagegen mitliefern (s. o.). Der
`provenance`-Block wird von der Filter-/Anzeigelogik **ignoriert**; er kennzeichnet nur, was
echt vs. gemockt ist.

### Handlabels

`tools/build_metadata.py` liest die **erste** dieser Quellen (siehe `--help`):

| # | Fundort | typischer Fall |
|---|---------|----------------|
| 1 | `--labels PFAD` | Export, den jemand geschickt hat |
| 2 | `<dataset>/originals/manual_labels.json` | Kopie, die mit dem Datensatz reist (und mit ihm deployt wird) |

Welche Form vorliegt, wird am **Inhalt** erkannt (`schema` ⇒ Export, `categories_text` ⇒ Store),
nicht am Dateinamen; eine umbenannte Kopie fällt also nicht stillschweigend durch. Der Store
speichert die Kategorien als editierbaren Text, der Export als aufgelösten
Block — beide ergeben nach `parse_categories()` dieselben Tokens.

Das Export-Format:

```jsonc
{
  "schema": "xai-viewer-manual-labels/1",
  "dataset": "RailPer",
  "exported_at": "2026-08-13T16:29:18",
  // Selbstbeschreibend: welche Kategorien galten beim Labeln (sie sind im Werkzeug editierbar).
  "categories": {
    "umgebung": {"label": "Umgebung",
                 "options": [{"token": "bahnhof", "label": "Bahnhof"}, ...]}, ...
  },
  // Alle Bilder des Datensatzes, je Kategorie eine Liste von Tokens.
  "images": {
    "RAWPED_set11_V000_I00001": {
      "umgebung": ["bahnhof"], "objekte": [], "tageszeit": ["tag"], "wetter": ["sonnig", "schnee"],
      "unclear": true,         // „fraglich" — Arbeitsmarkierung, kein Szenenattribut
      "note": "Person hinter dem Mast"   // freie Notiz zum Bild (leer, wenn keine)
    }, ...
  },
  // Bilder ohne eine einzige gesetzte Eigenschaft: die müssen gemockt bleiben.
  "unlabeled": ["RAWPED_set11_V000_I00063", ...]
}
```

Keys und Tokens entsprechen denen in `attributes`; der Default-Kategorientext des Werkzeugs ist
so gewählt, dass `slugify()` sie 1:1 reproduziert (ein Test wacht darüber).

Beim Einlesen gilt (für beide Formen):

- Der `categories`-Block ist **selbstbeschreibend** und wandert als `filter_categories` nach
  `metadata.json` — dieser Datensatz bringt seine Filterkategorien also selbst mit. Fehlt die
  Datei, gelten die Vorgaben aus `filter_options.py` + i18n-Katalog.
- Höchstens **4 Kategorien** (`MAX_FILTER_CATEGORIES`) — mehr zeigt der Filterdialog nicht;
  überzählige werden mit Warnung verworfen.
- Ein Token, das nicht im eigenen `categories`-Block steht, wird mit Warnung verworfen.
- `unclear` und `note` sind Arbeitsmaterial des Werkzeugs und wandern **nicht** in
  `metadata.json`.
- Bilder ohne Eintrag behalten gemockte Attribute (Warnung mit Anzahl).

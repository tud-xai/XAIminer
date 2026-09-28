# Insel-Tests (JS)

Prüft die clientseitigen „Inseln" – das kleine Inline-JS in den Templates – indem der
**tatsächlich ausgelieferte** Script-Block in Node gegen einen handgeschriebenen DOM-Stub
ausgeführt wird.

## Wofür das gut ist

Die Backend-Tests rendern Markup, führen es aber nie aus. Laufzeitfehler fallen deshalb
prinzipiell durch. Realer Fall: eine Deklaration im globalen Scope
brach beim **zweiten** Öffnen des Konfig-Dialogs den kompletten Script-Block (HTMX fügt ihn
bei jedem Öffnen neu ein → `SyntaxError: Identifier ... has already been declared`).
Symptom: „Modellüberblick" gewählt, Bilder-Sektion blieb stehen. Markup korrekt, Verhalten
kaputt, Testsuite grün.

Abgedeckt ist jeweils die **Logik** der Insel:

| Check | Insel | Prüft u. a. |
|---|---|---|
| `check_scroll_sync.js` | Galerie-Scroll-Kopplung (`__xaiScrollSync`) | proportionale Umrechnung, Echo-Schutz, 1px-Schwelle, Restore nach HTMX-Swap, keine NaN ohne Scrollbereich, ein Listener |
| `check_keynav.js` | Pfeiltasten-Stepping (`__xaiKeyNav`) | Richtung prev/next, **kein Stepping bei Fokus in Eingabefeldern**, Gate über `data-page`, defektes JSON, ein Listener |
| `check_link_toggle.js` | Link-Toggle (`xaiToggleLink`) | Button-Zustand/Titel, `data-linked` an der Galerie (Naht zur Scroll-Kopplung), Panel ohne Galerie |
| `check_config_dialog.js` | Konfig-Dialog | mehrfaches Evaluieren, Modus-Umschaltung, XAI-Verfügbarkeit |

## Wann sie übersprungen werden

`tests/test_islands.py` ist mit `@unittest.skipUnless(shutil.which("node"), …)` markiert:
**ohne installiertes `node` werden diese Tests stillschweigend geskippt**, die Suite bleibt
grün (die Insel-Tests erscheinen als *skipped*). Node ist also **keine Voraussetzung**, um am Projekt zu
arbeiten – die Backend-Tests laufen unabhängig davon.

Wer sehen will, ob und warum geskippt wurde:

```bash
python -m pytest tests/ -rs      # zeigt "SKIPPED ... node not installed"
```

Bewusst **nicht** in `pytest.ini` verdrahtet – der Normallauf
bleibt schlank.

## Bewusst ohne Toolchain

Nur Node und sein eingebautes `vm` – **kein npm, kein `package.json`, kein jsdom, kein
Browser, keine Dependencies**. Das passt zum build-freien Stil des Projekts
(Insel-JS klein und pur halten).

**Grenzen** – hier endet die Aussagekraft:

- Kein Rendering, kein Layout, kein CSS.
- Keine echten Events: die Listener werden direkt aufgerufen. Ob der Browser ein
  Scroll-Event tatsächlich in der Capture-Phase liefert, sagt der Test **nicht**.
- Kein HTMX: `htmx.ajax` ist gestubbt, geprüft wird nur, *dass* mit den richtigen Argumenten
  aufgerufen würde.

Dafür bleiben die Playwright-Smokes im Backlog zuständig.

## Warum der Stub `scrollTop` rundet

Nicht kosmetisch, sondern notwendig: Browser rasten `scrollTop` auf ganze Pixel ein. Genau
diese Rundung erzeugt die Rückkopplung, gegen die der Echo-Schutz existiert – ein Panel mit
kleinem Scrollbereich meldet eine **andere** Fraktion zurück, als es gesetzt bekam (1,5px →
2px = 67 % statt 50 %). Ohne Rundung im Stub bleibt der Echo-Schutz unbeobachtbar: Ein
Testlauf mit entferntem `ignoreNext.add(g)` blieb grün, bis der Stub rundete. Bei exakt
aufgehenden Proportionen ist ein Echo harmlos und wird schon von der 1px-Schwelle
absorbiert – deshalb arbeitet der Echo-Check mit **asymmetrischen** Panelhöhen.

Die Checks wurden gegen künstlich eingebaute Fehler in `base.html` geprüft (Echo-Schutz
entfernt, proportional → absolut, Fokus-Gate entfernt, `data-linked` nicht mehr gesetzt) –
alle vier schlagen fehl, wie sie sollen.

## Eine neue Insel ergänzen

1. `check_<insel>.js` anlegen: HTML-Datei als `argv[2]`, Block über `pickScript(file, marker)`
   holen (Marker = stabiles Token wie `__xaiScrollSync`, nicht Position), DOM aus
   `harness.js` stubben, mit `check()` prüfen, `finish()` am Ende.
2. In `tests/test_islands.py` einen Testfall ergänzen, der das passende Markup rendert
   (`self._workspace()` für `base.html`-Inseln).
3. **Gegenprüfen:** die Regel in der Insel absichtlich brechen und schauen, ob der Check rot
   wird. Ein Check, der das nicht tut, ist wertlos.

Der Stub bildet nur ab, was die jeweilige Insel anfasst – bewusst minimal statt „halbes DOM
nachbauen".

## Regel für Insel-JS

Alles in IIFEs kapseln, **nichts im globalen Scope deklarieren** – der Block wird bei jedem
HTMX-Swap neu ausgewertet. `window.foo = …` (Zuweisung) ist in Ordnung, `const foo = …` auf
oberster Ebene nicht.

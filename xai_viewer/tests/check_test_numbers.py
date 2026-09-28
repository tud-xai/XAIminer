"""Reports test numbers (`test_bNNN__…`) that are used more than once.

Two branches that both keep counting hand out the same numbers. Within ONE class that is
dangerous: Python silently overrides the first method, the test disappears from the suite and
pytest still reports green. Across classes it is merely confusing.

Run after merging branches that both added tests:

    python xai_viewer/tests/check_test_numbers.py

Exit code 1 if a number is used twice in the same class, else 0.
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

TEST_FILES = (
    "test_backend.py",
    "test_islands.py",
    "test_deployment.py",
)
CLASS_RE = re.compile(r"class (\w+)")
TEST_RE = re.compile(r"\s+def (test_b[0-9]+[a-z]*)__")


def collect(path: Path) -> dict:
    """(class, number) -> how often it is defined."""
    counts = defaultdict(int)
    current = None
    for line in path.read_text(encoding="utf-8").splitlines():
        match = CLASS_RE.match(line)
        if match:
            current = match.group(1)
            continue
        match = TEST_RE.match(line)
        if match:
            counts[(current, match.group(1))] += 1
    return counts


def main() -> int:
    base = Path(__file__).parent
    counts = defaultdict(int)
    for name in TEST_FILES:
        path = base / name
        if path.exists():
            for key, n in collect(path).items():
                counts[key] += n

    within = sorted(key for key, n in counts.items() if n > 1)
    across = defaultdict(list)
    for cls, number in counts:
        across[number].append(cls)
    across = {n: sorted(cs) for n, cs in across.items() if len(cs) > 1}

    if within:
        print("FEHLER: dieselbe Nummer mehrfach in DERSELBEN Klasse — ein Test wird still "
              "überschrieben:")
        for cls, number in within:
            print(f"  {cls}.{number}")
    else:
        print("OK: keine Nummer doppelt innerhalb einer Klasse.")

    if across:
        print(f"\nHinweis: {len(across)} Nummer(n) in mehreren Klassen vergeben (harmlos, aber als "
              f"Referenz mehrdeutig):")
        for number, classes in sorted(across.items()):
            print(f"  {number}: {', '.join(classes)}")

    return 1 if within else 0


if __name__ == "__main__":
    sys.exit(main())

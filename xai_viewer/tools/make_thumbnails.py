"""
Pre-warm: generates all gallery thumbnails for a dataset upfront under
`<dataset>/thumbs/...`. This avoids first-generation latency for the first user;
the lazy generation in the app remains only a safety net.

Usage (from `xai_viewer/`):
        python tools/make_thumbnails.py [DATASET_NAME]   # Default: RailPer
        python tools/make_thumbnails.py --root /path RailPer
        python tools/make_thumbnails.py RailPer --force  # regenerate existing thumbnails
"""

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
XAI_DIR = HERE.parent
# sys.path bootstrap for the flat app modules — see tools/build_metadata.py.
if str(XAI_DIR) not in sys.path:
    sys.path.insert(0, str(XAI_DIR))

from thumbnails import generate, iter_source_images, thumb_rel   # noqa: E402

DEFAULT_DATASETS_ROOT = XAI_DIR.parent.parent / "datasets"


def warm(dataset_dir: Path, force: bool = False) -> tuple:
    created = skipped = failed = 0
    for src in iter_source_images(dataset_dir):
        rel = src.relative_to(dataset_dir).as_posix()
        dst = dataset_dir / thumb_rel(rel)
        # Skip only when a current thumbnail exists: a stale one (source re-exported in
        # place, newer than the thumbnail) is regenerated even without --force.
        if dst.exists() and not force and dst.stat().st_mtime >= src.stat().st_mtime:
            skipped += 1
            continue
        try:
            generate(src, dst)
            created += 1
            if created % 500 == 0:
                print(f"  … {created} erzeugt")
        except OSError as exc:
            print(f"FEHLER bei {rel}: {exc}")
            failed += 1
    return created, skipped, failed


def main():
    parser = argparse.ArgumentParser(description="Galerie-Thumbnails vorab erzeugen")
    parser.add_argument("dataset", nargs="?", default="RailPer")
    parser.add_argument("--root", type=Path, default=DEFAULT_DATASETS_ROOT)
    parser.add_argument("--force", action="store_true", help="vorhandene Thumbnails neu erzeugen")
    args = parser.parse_args()

    dataset_dir = (Path(args.dataset) if Path(args.dataset).is_absolute()
                   else args.root / args.dataset)
    if not dataset_dir.is_dir():
        raise SystemExit(f"Datensatz-Verzeichnis nicht gefunden: {dataset_dir}")

    created, skipped, failed = warm(dataset_dir, args.force)
    print(f"Thumbnails für '{dataset_dir.name}': {created} erzeugt, {skipped} vorhanden, {failed} Fehler")
    print(f"Ziel: {dataset_dir / 'thumbs'}")


if __name__ == "__main__":
    main()

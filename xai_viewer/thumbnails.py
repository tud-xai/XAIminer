"""
Thumbnail generation for the gallery.

Gallery images are served as small 16:9 centre-crop WebP thumbnails
(matching `object-fit: cover` in CSS); full resolution is reserved for the single-image view.
Thumbnails are stored **transparently** under `<dataset>/thumbs/...` (mirroring
the source structure). They are created upfront by the pre-warm script (`tools/make_thumbnails.py`)
or lazily on the first request – and then served directly from the filesystem.
"""

import os
from pathlib import Path, PurePosixPath

from PIL import Image, ImageOps

THUMB_W, THUMB_H = 320, 180          # 16:9, ~2x the display size (crisp on HiDPI)
THUMB_QUALITY = 80
THUMB_SUBDIR = "thumbs"

# Cache-buster: changes whenever size/quality is changed → new URL.
THUMB_TAG = f"{THUMB_W}x{THUMB_H}q{THUMB_QUALITY}"

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
IGNORE_NAMES = {".DS_Store", "Thumbs.db"}


def thumb_rel(src_rel: str) -> str:
    """Source-relative path → thumbnail-relative path: originals/imgs/x.jpg → thumbs/originals/imgs/x.webp."""
    p = PurePosixPath(src_rel)
    return str(PurePosixPath(THUMB_SUBDIR) / p.with_suffix(".webp"))


def generate(src_path: Path, dst_path: Path) -> None:
    """Generates a 16:9 centre-crop WebP thumbnail (written atomically)."""
    with Image.open(src_path) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        thumb = ImageOps.fit(img, (THUMB_W, THUMB_H), method=Image.Resampling.LANCZOS)
    dst_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst_path.with_name(f".{dst_path.name}.{os.getpid()}.tmp")
    thumb.save(tmp, format="WEBP", quality=THUMB_QUALITY, method=6)
    os.replace(tmp, dst_path)


def iter_source_images(dataset_dir: Path):
    """All source images of a dataset (originals + XAI leaves) for the pre-warm.

    Walks with ``followlinks=True``: a dataset directory may stitch renderings together from
    symlinks (an export run on a mounted drive, too large to copy). ``Path.rglob`` silently
    skips those before Python 3.13 – and a silently empty pre-warm is exactly the failure that
    only shows up as an empty gallery much later.
    """
    imgs_dir = dataset_dir / "originals" / "imgs"
    xai_dir = dataset_dir / "xai"
    roots = [d for d in (imgs_dir, xai_dir) if d.is_dir()]
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root, followlinks=True):
            for name in sorted(filenames):
                p = Path(dirpath) / name
                if p.suffix.lower() in IMAGE_SUFFIXES and name not in IGNORE_NAMES and p.is_file():
                    yield p

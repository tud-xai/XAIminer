"""Generate the overview figure shown on /docs/overview, one SVG per UI language.

The figure explains what XAIminer is: not the AI models, not the XAI methods, but the test bench
on which a combination of both is examined on selected images.

The SVGs are self-contained (logo glyph, thumbnails, screenshot and emojis are embedded) and are
checked in under `static/docs/`, so the server needs neither the dataset nor this script. Re-run
after changing layout, texts or the screenshot, from `xai_viewer/`:

    python tools/make_overview_figure.py [--dataset ../../datasets/RailPer]

Development-only dependency: fontTools (`uv pip install fonttools brotli`, brotli for woff2).
"""

import argparse
import base64
import io
from pathlib import Path

from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.ttLib import TTFont
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
XAI_DIR = HERE.parent
ICON_FONT = XAI_DIR / "static/vendor/bootstrap-icons-1.11.3/fonts/bootstrap-icons.woff2"
TRAIN_FRONT = 0xF81D  # bi-train-front, the navbar logo
OUT_DIR = XAI_DIR / "static/docs"
ASSETS = HERE / "overview_assets"

# Thumbnails for the stack; the last one is on top.
THUMBS = [
    "railsem19_no_person_rs00566.webp",
    "railsem19_no_person_rs00804.webp",
    "RAWPED_set12_V000_I00001.webp",
]
MODELS = ["ConvNeXt", "ResNet", "VGG"]
XAI_METHODS = ["Grad-CAM", "LRP", "CRP", "CRAFT"]

# Figure texts per language. Kept here rather than in i18n/*.toml: they are baked into the SVG at
# build time and never rendered by the app.
TEXTS = {
    "de": {
        "models": ("KI-Modelle", "Motor"),
        "images": ("Bilddaten", "Kraftstoff"),
        "xai": ("XAI-Methoden", "Sensoren"),
        "bench": "Motorprüfstand",
        "outcomes": [("💡", "Erkenntnis / Verständnis"), ("📝", "Befund / Protokoll")],
        "placeholder": "Screenshot: zwei Panels",
    },
    "en": {
        "models": ("AI models", "engine"),
        "images": ("Image data", "fuel"),
        "xai": ("XAI methods", "sensors"),
        "bench": "engine test bench",
        "outcomes": [("💡", "Insight / Understanding"), ("📝", "Findings / Report")],
        "placeholder": "Screenshot: two panels",
    },
}

W = 1600
FONT = "'Segoe UI', 'Helvetica Neue', Arial, 'DejaVu Sans', sans-serif"
# Emojis are embedded as bitmaps: Inkscape cannot render color emoji fonts, and the font may be
# missing on the viewer's machine anyway. Needs a CBDT font such as Noto Color Emoji.
EMOJI_FONT_CANDIDATES = [
    Path("/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"),
    Path("/usr/share/fonts/noto/NotoColorEmoji.ttf"),
    Path("/usr/share/fonts/google-noto-emoji/NotoColorEmoji.ttf"),
]

# Colors: external components are neutral gray, XAIminer carries the accent.
EXT_FILL = "#f1f3f5"
EXT_STROKE = "#adb5bd"
TEXT = "#212529"
DIM = "#868e96"
ACCENT = "#0d6efd"
NAVBAR = "#212529"


def icon_path(size: float, x: float, y: float) -> str:
    """Return an SVG <path> of the train-front glyph, `size` px high, top-left at (x, y)."""
    font = TTFont(ICON_FONT)
    glyph_name = font.getBestCmap()[TRAIN_FRONT]
    glyph_set = font.getGlyphSet()
    pen = SVGPathPen(glyph_set)
    glyph_set[glyph_name].draw(pen)
    s = size / font["head"].unitsPerEm
    ascent = font["hhea"].ascent
    # Font coordinates are y-up; flip and move the ascender line to y.
    return (
        f'<path transform="translate({x},{y + ascent * s:.2f}) scale({s:.5f},{-s:.5f})" '
        f'd="{pen.getCommands()}" fill="#ffffff"/>'
    )


def thumb_data_uri(path: Path) -> str:
    buf = io.BytesIO()
    Image.open(path).convert("RGB").save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def png_data_uri(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def emoji_data_uri(emoji: str, font_path: Path) -> str:
    font = ImageFont.truetype(font_path, 109)  # the only size of the CBDT bitmaps
    img = Image.new("RGBA", (136, 128), (0, 0, 0, 0))
    ImageDraw.Draw(img).text((0, 0), emoji, font=font, embedded_color=True)
    buf = io.BytesIO()
    img.crop(img.getbbox()).save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def text(x, y, s, size, weight="normal", fill=TEXT, anchor="middle", extra=""):
    return (
        f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" '
        f'text-anchor="{anchor}" {extra}>{s}</text>'
    )


def pills(labels, cx, y, pad=20, pill_h=46, gap=14):
    """Horizontally centered row of labelled rounded boxes, width estimated from the label."""
    widths = [len(label) * 14 + 2 * pad for label in labels]
    total = sum(widths) + (len(labels) - 1) * gap
    x = cx - total / 2 - gap
    out = []
    for label, pill_w in zip(labels, widths):
        x += gap
        out.append(
            f'<rect x="{x}" y="{y}" width="{pill_w}" height="{pill_h}" rx="8" fill="#ffffff" '
            f'stroke="{EXT_STROKE}" stroke-width="1.5"/>'
        )
        out.append(text(x + pill_w / 2, y + pill_h / 2 + 8, label, 22, "600"))
        x += pill_w
    return out


def ext_box(x, y, w, h, title, metaphor):
    cx = x + w / 2
    return [
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{EXT_FILL}" '
        f'stroke="{EXT_STROKE}" stroke-width="2"/>',
        text(cx, y + 48, title, 32, "700"),
        text(cx, y + 80, f"({metaphor})", 22, fill=DIM, extra='font-style="italic"'),
    ]


def arrow(x, y1, y2, color=EXT_STROKE):
    return (
        f'<line x1="{x}" y1="{y1}" x2="{x}" y2="{y2 - 14}" stroke="{color}" stroke-width="4"/>'
        f'<polygon points="{x - 12},{y2 - 16} {x + 12},{y2 - 16} {x},{y2}" fill="{color}"/>'
    )


def build(lang: str, dataset: Path, emoji_font: Path) -> str:
    tx = TEXTS[lang]
    thumb_dir = dataset / "thumbs/originals/imgs"
    parts = []

    # --- top row: the three external ingredients -------------------------------------------
    gap = 50
    box_w, box_h, box_y = (W - 120 - 2 * gap) / 3, 270, 40
    xs = [60 + i * (box_w + gap) for i in range(3)]

    parts += ext_box(xs[0], box_y, box_w, box_h, *tx["models"])
    parts += pills(MODELS, xs[0] + box_w / 2, box_y + 150, pad=24, pill_h=56, gap=18)

    parts += ext_box(xs[1], box_y, box_w, box_h, *tx["images"])
    tw, th = 208, 117  # 16:9 thumbnails
    tcx, tcy = xs[1] + box_w / 2 - 8, box_y + 190
    for i, name in enumerate(THUMBS):
        k = i - (len(THUMBS) - 1)  # 0 for the top image, negative behind it
        dx, dy, rot = -k * 16, k * 12, (-6, 5, 0)[i]
        parts.append(
            f'<g transform="translate({tcx + dx},{tcy + dy}) rotate({rot})">'
            f'<rect x="{-tw / 2 - 4}" y="{-th / 2 - 4}" width="{tw + 8}" height="{th + 8}" '
            f'fill="#ffffff" stroke="{EXT_STROKE}" stroke-width="1"/>'
            f'<image x="{-tw / 2}" y="{-th / 2}" width="{tw}" height="{th}" '
            f'preserveAspectRatio="xMidYMid slice" xlink:href="{thumb_data_uri(thumb_dir / name)}"/>'
            f"</g>"
        )

    parts += ext_box(xs[2], box_y, box_w, box_h, *tx["xai"])
    parts += pills(XAI_METHODS, xs[2] + box_w / 2, box_y + 150, pad=16, pill_h=56, gap=12)

    # --- arrows into the test bench --------------------------------------------------------
    main_y = box_y + box_h + 70
    for x in xs:
        parts.append(arrow(x + box_w / 2, box_y + box_h + 8, main_y - 6))

    # --- XAIminer: the test bench -----------------------------------------------------------
    main_x, main_w = 60, W - 120
    header_h = 76
    slot_margin = 30
    slot_w = main_w - 2 * slot_margin
    slot_h = 640
    main_h = header_h + slot_h + 2 * slot_margin
    parts.append(
        f'<rect x="{main_x}" y="{main_y}" width="{main_w}" height="{main_h}" rx="14" '
        f'fill="#ffffff" stroke="{ACCENT}" stroke-width="5"/>'
    )
    # Header strip styled like the app's dark navbar, with the logo.
    parts.append(
        f'<path d="M{main_x + 2.5},{main_y + header_h} V{main_y + 14} '
        f"a11.5,11.5 0 0 1 11.5,-11.5 H{main_x + main_w - 14} "
        f'a11.5,11.5 0 0 1 11.5,11.5 V{main_y + header_h} Z" fill="{NAVBAR}"/>'
    )
    parts.append(icon_path(40, main_x + 30, main_y + 16))
    parts.append(text(main_x + 84, main_y + 51, "XAIminer", 36, "700", "#ffffff", "start"))
    parts.append(
        text(main_x + 262, main_y + 50, f"({tx['bench']})", 24, fill="#adb5bd", anchor="start",
             extra='font-style="italic"')
    )

    # Screenshot slot: the language's screenshot, else the German one, else a placeholder.
    sx, sy = main_x + slot_margin, main_y + header_h + slot_margin
    parts.append(f"<!-- screenshot slot: x={sx} y={sy} width={slot_w} height={slot_h} -->")
    shot = next((p for p in (ASSETS / f"screenshot-two-panels-{lang}.png",
                             ASSETS / "screenshot-two-panels-de.png") if p.exists()), None)
    if shot:
        parts.append(
            f'<image id="screenshot" x="{sx}" y="{sy}" width="{slot_w}" height="{slot_h}" '
            f'preserveAspectRatio="xMidYMid meet" xlink:href="{png_data_uri(shot)}"/>'
        )
    else:
        parts.append(
            f'<g id="screenshot-placeholder">'
            f'<rect x="{sx}" y="{sy}" width="{slot_w}" height="{slot_h}" fill="#f8f9fa" '
            f'stroke="{EXT_STROKE}" stroke-width="2" stroke-dasharray="12 8"/>'
            + text(sx + slot_w / 2, sy + slot_h / 2 - 6, tx["placeholder"], 34, fill=DIM)
            + text(sx + slot_w / 2, sy + slot_h / 2 + 34, f"{slot_w} × {slot_h}", 24, fill=DIM)
            + "</g>"
        )

    # --- outcomes: two boxes side by side, each fed by its own arrow ---------------------------
    out_y = main_y + main_h + 60
    out_w, out_h, out_gap = 560, 70, 80
    out_x0 = W / 2 - out_w - out_gap / 2
    for i, (emoji, label) in enumerate(tx["outcomes"]):
        ox = out_x0 + i * (out_w + out_gap)
        parts.append(arrow(ox + out_w / 2, main_y + main_h + 8, out_y - 6, ACCENT))
        parts.append(
            f'<rect x="{ox}" y="{out_y}" width="{out_w}" height="{out_h}" rx="35" fill="#ffffff" '
            f'stroke="{TEXT}" stroke-width="2"/>'
        )
        # Emoji left of the label; the pair is centered by estimating the label width.
        em, label_w = 36, len(label) * 15.5
        lx = ox + (out_w - em - 14 - label_w) / 2
        parts.append(
            f'<image x="{lx}" y="{out_y + (out_h - em) / 2}" width="{em}" height="{em}" '
            f'preserveAspectRatio="xMidYMid meet" xlink:href="{emoji_data_uri(emoji, emoji_font)}"/>'
        )
        parts.append(text(lx + em + 14, out_y + 45, label, 28, "600", anchor="start"))

    height = out_y + out_h + 40
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'viewBox="0 0 {W} {height}" width="{W}" height="{height}" font-family="{FONT}">\n'
        f'<rect width="100%" height="100%" fill="#ffffff"/>\n' + "\n".join(parts) + "\n</svg>\n"
    )


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", type=Path, default=XAI_DIR.parent.parent / "datasets/RailPer",
                    help="dataset providing the stack thumbnails (default: ../../datasets/RailPer)")
    ap.add_argument("--emoji-font", type=Path,
                    default=next((p for p in EMOJI_FONT_CANDIDATES if p.exists()), None),
                    help="color emoji font in CBDT format, e.g. NotoColorEmoji.ttf")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args()
    if args.emoji_font is None:
        ap.error("no emoji font found, pass --emoji-font")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    for lang in TEXTS:
        out = args.out_dir / f"overview-{lang}.svg"
        out.write_text(build(lang, args.dataset, args.emoji_font), encoding="utf-8")
        print(f"wrote {out}")


if __name__ == "__main__":
    main()

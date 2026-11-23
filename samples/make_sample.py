"""Draw a filled-in Line Stop Log and make it look like a phone photo of the paper.

    python samples/make_sample.py            # writes samples/line3-shiftB.jpg + .truth.json

The rows deliberately include the hard cases a real sheet has: a 12-hour time with no PM,
a tally row (x5), a Hinglish reason, written minutes that disagree with the clock times,
and a stop marked BD that is really a minor stop by the plant's rule. The truth file holds
what a perfect reader should return, so a live run can be scored against it.
"""

from __future__ import annotations

import json
import random
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from stoplog.form import load_font, render_form  # noqa: E402

HERE = Path(__file__).resolve().parent

HEADER = {"date": "24/11/2026", "shift": "B", "line": "Line 3", "sku": "Soap 100g", "incharge": "R. Kumar"}
# machine, stop, restart, minutes, type, reason, action, initials
ROWS = [
    ("Wrapper", "14:20", "14:26", "6", "MS", "film jam at sealing jaw", "cleared film", "RK"),
    ("Wrapper", "14:52", "15:30", "38", "BD", "sealing jaw heater not heating", "replaced heater", "SM"),
    ("Stamper", "15:41", "15:44", "3", "MS", "bar stuck in die", "removed bar", "RK"),
    ("Wrapper", "", "", "10", "MS", "wrapper jam x5", "reset each time", "RK"),
    ("Cartoner", "4:10", "4:22", "12", "BD", "carton feed jam", "adjusted guide", "RK"),
    ("Line", "17:00", "17:30", "30", "PM", "tea break + cleaning", "", "RK"),
    ("Wrapper", "18:05", "18:25", "15", "MAT", "film khatam, roll late from store", "new roll fitted", "RK"),
    ("Conveyor", "19:10", "19:18", "8", "BD", "belt slipping", "tightened belt", "SM"),
    ("Wrapper", "20:00", "20:45", "45", "CO", "SKU change 100g to 125g", "", "RK"),
    ("Stamper", "21:15", "21:21", "6", "MS", "die jam", "cleaned die", "RK"),
]

HAND_FONTS = ["Inkfree.ttf", "C:/Windows/Fonts/Inkfree.ttf", "segoepr.ttf", "C:/Windows/Fonts/segoepr.ttf",
              "/usr/share/fonts/truetype/comic-neue/ComicNeue-Regular.ttf"]


def hand_font(size: int) -> ImageFont.ImageFont:
    for name in HAND_FONTS:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return load_font(size)


def scrawl(d: ImageDraw.ImageDraw, box, text: str, rng: random.Random, size: int) -> None:
    """Write `text` in a cell with a little per-word wobble, like a pen."""
    if not text:
        return
    x0, y0, x1, y1 = box
    font = hand_font(size)
    while d.textlength(text, font=font) > (x1 - x0) and size > 14:
        size -= 2
        font = hand_font(size)
    x = x0 + rng.randint(2, 10)
    base = (y0 + y1) // 2
    ink = (rng.randint(15, 35), rng.randint(25, 45), rng.randint(90, 120))  # blue ballpoint
    for word in text.split(" "):
        d.text((x, base + rng.randint(-3, 3)), word, font=font, fill=ink, anchor="lm")
        x += d.textlength(word + " ", font=font) + rng.randint(-1, 3)


def fill(seed: int = 7) -> Image.Image:
    rng = random.Random(seed)
    img, lay = render_form()
    d = ImageDraw.Draw(img)
    for key in ("date", "line", "sku", "incharge"):
        scrawl(d, lay.header[key], HEADER[key], rng, 40)
    cx, cy, r = lay.shift_marks[HEADER["shift"]]
    d.ellipse([cx - r, cy - r * 0.9, cx + r, cy + r * 0.9], outline=(25, 35, 110), width=4)
    keys = ["machine", "start", "end", "minutes", "type", "reason", "action", "initials"]
    for row, values in zip(lay.cells, ROWS):
        for key, value in zip(keys, values):
            scrawl(d, row[key], value, rng, 36)
    scrawl(d, lay.footer["total"], "173", rng, 36)
    return img


def as_photo(sheet: Image.Image, seed: int = 7) -> Image.Image:
    """Desk background, slight tilt/perspective, uneven light, sensor noise, soft focus."""
    rng = random.Random(seed)
    W, H = sheet.size
    pad = int(W * 0.06)
    canvas = Image.new("RGB", (W + 2 * pad, H + 2 * pad), (118, 96, 72))
    canvas.paste(sheet, (pad, pad))
    cw, ch = canvas.size
    j = lambda a: rng.randint(-a, a)  # noqa: E731
    quad = (j(30), j(20), j(20), ch + j(30), cw + j(30), ch + j(20), cw + j(20), j(30))
    photo = canvas.transform((cw, ch), Image.Transform.QUAD, quad, Image.Resampling.BICUBIC,
                             fillcolor=(110, 90, 68))
    photo = photo.rotate(rng.uniform(-1.2, 1.2), Image.Resampling.BICUBIC, expand=False, fillcolor=(110, 90, 68))
    # smooth light falloff from the top-left corner (a lamp or window), no hard edges
    vert = Image.linear_gradient("L").resize(photo.size)
    horiz = vert.transpose(Image.Transpose.ROTATE_90).transpose(Image.Transpose.FLIP_LEFT_RIGHT).resize(photo.size)
    shade = ImageChops.add(vert.point(lambda v: v // 2), horiz.point(lambda v: v // 2))
    shade = shade.point(lambda v: 255 - v * 60 // 255)
    photo = Image.composite(photo, Image.new("RGB", photo.size, (0, 0, 0)), shade)
    photo = Image.blend(photo, Image.merge("RGB", [Image.effect_noise(photo.size, 22)] * 3), 0.06)
    photo = photo.filter(ImageFilter.GaussianBlur(0.9))
    return photo.resize((3000, int(3000 * photo.height / photo.width)), Image.Resampling.LANCZOS)


def truth() -> dict:
    return {
        "header": {"sheet_date": "2026-11-24", **{k: v for k, v in HEADER.items() if k != "date"}},
        "rows": [dict(zip(["machine", "start_time", "end_time", "duration_min", "category_as_written",
                           "reason_as_written", "action_taken", "recorded_by"], r)) for r in ROWS],
        "expected_after_normalising": {
            "events": 14,  # 9 timed rows + one tally row of 5
            "row5_start": "16:10",  # "4:10" on shift B
            "row7_review": "written 15 min but 18:05-18:25 is 20 min",
            "row8_category": "minor_stop",  # marked BD, but 8 min < 10 min rule
            "row4_tally": "5 stops of 2 min",
        },
    }


if __name__ == "__main__":
    out = HERE / "line3-shiftB.jpg"
    as_photo(fill()).save(out, "JPEG", quality=84)
    (HERE / "line3-shiftB.truth.json").write_text(json.dumps(truth(), indent=2), encoding="utf-8")
    print(out)

"""Load a phone/camera photo, fix its orientation, shrink it, and hash it."""

from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}
MAX_SIDE = 2048  # long edge sent to the model; enough for handwriting, small enough to be cheap

_EXIF_IFD = 0x8769
_DATETIME_ORIGINAL = 36867
_DATETIME = 306

try:  # iPhone photos are HEIC; support them when the optional extra is installed
    from pillow_heif import register_heif_opener  # type: ignore

    register_heif_opener()
    HEIC_SUPPORTED = True
except ImportError:  # pragma: no cover - depends on the optional extra
    HEIC_SUPPORTED = False


class ImageError(Exception):
    """The file could not be read as an image."""


@dataclass
class PreparedImage:
    jpeg: bytes
    width: int
    height: int
    taken_at: datetime | None  # from EXIF, else None
    media_type: str = "image/jpeg"


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def is_image_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_EXTENSIONS


def _exif_datetime(img: Image.Image) -> datetime | None:
    try:
        exif = img.getexif()
    except Exception:
        return None
    raw = None
    try:
        raw = exif.get_ifd(_EXIF_IFD).get(_DATETIME_ORIGINAL)
    except Exception:
        raw = None
    raw = raw or exif.get(_DATETIME)
    if not raw:
        return None
    try:
        return datetime.strptime(str(raw).strip("\x00 ").strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def prepare_image(path: str | Path, max_side: int = MAX_SIDE) -> PreparedImage:
    path = Path(path)
    if path.suffix.lower() in {".heic", ".heif"} and not HEIC_SUPPORTED:
        raise ImageError(f"{path.name}: HEIC photos need the optional extra: pip install 'stoplog[heic]'")
    try:
        with Image.open(path) as opened:
            taken_at = _exif_datetime(opened)
            img = ImageOps.exif_transpose(opened)  # phone photos are often stored sideways
            img = img.convert("RGB")
    except Exception as exc:  # Pillow raises many types for bad files
        raise ImageError(f"{path.name}: not a readable image ({exc})") from exc

    if max(img.size) > max_side:
        img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90, optimize=True)
    return PreparedImage(jpeg=buf.getvalue(), width=img.width, height=img.height, taken_at=taken_at)

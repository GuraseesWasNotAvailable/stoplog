"""Take photos on a Raspberry Pi and keep only the ones where the sheet changed.

Backends (picked automatically, or set STOPLOG_CAMERA):
  rpicam   Pi camera module via `rpicam-still` (Raspberry Pi OS Bookworm+) or `libcamera-still`
  usb      USB webcam via `fswebcam` (sudo apt install fswebcam)
  command  any command; set STOPLOG_CAPTURE_COMMAND with an {out} placeholder
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps


class CameraError(RuntimeError):
    pass


class Camera:
    DEFAULT_SIZE = {"rpicam": (2304, 1728), "usb": (1920, 1080)}  # USB webcams rarely exceed 1080p

    def __init__(self, backend: str = "auto", command: str | None = None,
                 width: int | None = None, height: int | None = None, timeout: float = 30.0):
        self.timeout = timeout
        self.command = command
        self.backend = self._pick(backend.lower(), command)
        dw, dh = self.DEFAULT_SIZE.get(self.backend, (2304, 1728))
        self.width, self.height = width or dw, height or dh

    @staticmethod
    def _pick(backend: str, command: str | None) -> str:
        if command:
            return "command"
        if backend != "auto":
            return backend
        if shutil.which("rpicam-still") or shutil.which("libcamera-still"):
            return "rpicam"
        if shutil.which("fswebcam"):
            return "usb"
        raise CameraError(
            "No camera tool found. Install rpicam-apps (Pi camera) or fswebcam (USB webcam), "
            "or set STOPLOG_CAPTURE_COMMAND."
        )

    def argv(self, out: Path) -> list[str]:
        if self.backend == "command":
            return shlex.split(self.command.format(out=str(out)))  # type: ignore[union-attr]
        if self.backend == "rpicam":
            exe = shutil.which("rpicam-still") or shutil.which("libcamera-still") or "rpicam-still"
            return [exe, "-n", "-t", "2000", "--width", str(self.width), "--height", str(self.height),
                    "-q", "92", "-o", str(out)]
        if self.backend == "usb":
            return ["fswebcam", "-q", "--no-banner", "-S", "10", "-r", f"{self.width}x{self.height}",
                    "--jpeg", "92", str(out)]
        raise CameraError(f"unknown camera backend {self.backend!r}")

    def capture(self, dest: Path) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.stem + ".part.jpg")
        try:
            proc = subprocess.run(self.argv(tmp), capture_output=True, text=True, timeout=self.timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise CameraError(f"camera command failed: {exc}") from exc
        if proc.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            raise CameraError(f"camera command failed ({proc.returncode}): {proc.stderr.strip()[:300]}")
        tmp.replace(dest)
        return dest


class ChangeDetector:
    """Is this frame different enough from the last one we kept to be worth reading?

    Compares a small, blurred, contrast-normalised greyscale copy cell by cell and takes the
    largest cell change, so one new handwritten row stands out while sensor noise averages away.
    """

    # Measured on simulated fixed-camera frames: same sheet with new sensor noise and a 6%
    # light change scores ~1.5; one newly written row scores ~7.8.
    def __init__(self, threshold: float = 4.0, size: tuple[int, int] = (192, 144), cell: int = 12):
        self.threshold, self.size, self.cell = threshold, size, cell
        self._last: bytes | None = None
        self._pending: bytes | None = None

    def signature(self, path: Path) -> bytes:
        with Image.open(path) as im:
            g = ImageOps.exif_transpose(im).convert("L").resize(self.size, Image.Resampling.BILINEAR)
        g = ImageOps.autocontrast(g.filter(ImageFilter.GaussianBlur(1)), cutoff=1)
        return g.tobytes()  # one byte per pixel, row-major

    def score(self, a: bytes, b: bytes) -> float:
        w, h = self.size
        c = self.cell
        worst = 0.0
        for y0 in range(0, h, c):
            for x0 in range(0, w, c):
                total = n = 0
                for y in range(y0, min(y0 + c, h)):
                    row = y * w
                    for x in range(x0, min(x0 + c, w)):
                        total += abs(a[row + x] - b[row + x])
                        n += 1
                worst = max(worst, total / n)
        return worst

    def check(self, path: Path) -> tuple[bool, float]:
        sig = self.signature(path)
        self._pending = sig
        if self._last is None:
            return True, float("inf")
        s = self.score(sig, self._last)
        return s >= self.threshold, s

    def accept(self) -> None:
        """Call after the checked frame was kept, so the next frame is compared against it."""
        if self._pending is not None:
            self._last = self._pending

"""Produce upload-ready derivatives for formats eBay Picture Services rejects.

Why this exists: eBay's Media API documentation lists HEIC, AVIF and WEBP as
supported, but EPS rejects HEIC with error 190203. The Trading API's own EPS
documentation lists only JPG, GIF, PNG, BMP and TIF, and sellers report HEIC
failing in eBay's first-party Seller Hub too. The Media API docs overstate what
the backend accepts.

The original file is never modified and never discarded -- it stays the source of
truth, so a future Facebook Marketplace or Mercari adapter can send the original
or its own preferred representation. This module only produces an additional
JPEG for eBay, cached next to the database and keyed by the original's content
hash so conversion happens once per photo.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from resell.images import inspect

# Formats EPS is documented to accept and that we have no reason to re-encode.
# BMP and TIFF are nominally accepted but eBay converts them to JPG server-side
# anyway, so converting locally costs nothing and keeps quality under our control.
DIRECT_UPLOAD_FORMATS = frozenset({"jpeg", "png", "gif"})

# Quality ladder. Start high; step down only if the result exceeds eBay's 12 MB
# limit, which a 48 MP photo re-encoded at 95 can do.
JPEG_QUALITY_LADDER = (92, 85, 78, 70)
MAX_DERIVATIVE_BYTES = 12 * 1024 * 1024


class ConversionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Derivative:
    """What to actually upload, and where it came from."""

    source: Path
    upload_path: Path
    converted: bool
    note: str = ""

    @property
    def is_original(self) -> bool:
        return not self.converted


def source_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _convert_with_sips(source: Path, destination: Path, quality: int) -> None:
    """Convert via macOS `sips`.

    Chosen over pillow-heif because it is already present on every Mac, adds no
    dependency, and is Apple's own decoder -- so it handles every iPhone HEIC
    variant, including the 10-bit HDR and Live Photo containers that trip up
    third-party decoders. The tradeoff is macOS-only, which is acceptable for a
    tool specified to run on an M4 Max; `_convert_with_pillow` covers other
    platforms if one is ever needed.
    """
    result = subprocess.run(
        [
            "sips",
            "-s", "format", "jpeg",
            "-s", "formatOptions", str(quality),
            str(source),
            "--out", str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not destination.exists():
        raise ConversionError(
            f"sips failed on {source.name} (exit {result.returncode}): "
            f"{(result.stderr or result.stdout or '').strip()[:300]}"
        )


def _convert_with_pillow(source: Path, destination: Path, quality: int) -> None:
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:
        raise ConversionError(
            f"Cannot convert {source.name}: neither `sips` (macOS) nor Pillow is "
            "available. Install Pillow (plus pillow-heif for HEIC) or convert the "
            "file manually."
        ) from exc

    with Image.open(source) as image:
        # HEIC and PNG can carry alpha or non-RGB modes that JPEG cannot encode.
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.save(destination, format="JPEG", quality=quality, optimize=True)


def _convert(source: Path, destination: Path, quality: int) -> None:
    if shutil.which("sips"):
        _convert_with_sips(source, destination, quality)
    else:
        _convert_with_pillow(source, destination, quality)


def ensure_uploadable(
    source: str | Path, cache_dir: str | Path, *, digest: str | None = None
) -> Derivative:
    """Return the file eBay should receive, converting to JPEG only if needed.

    Idempotent: a cached derivative for the same source content is reused rather
    than re-encoded, so repeated runs cost nothing.
    """
    source = Path(source)
    facts = inspect(source)
    if not facts.path.exists():
        raise ConversionError(f"{source} does not exist")
    if facts.image_format is None:
        raise ConversionError(
            f"{source.name}: unrecognised image format, cannot convert or upload"
        )
    if facts.image_format in DIRECT_UPLOAD_FORMATS and not facts.animated:
        return Derivative(source, source, converted=False)

    digest = digest or source_digest(source)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{digest[:16]}.jpg"

    if destination.exists() and destination.stat().st_size > 0:
        return Derivative(
            source,
            destination,
            converted=True,
            note=f"{facts.image_format} -> jpeg (cached derivative)",
        )

    last_size = 0
    for quality in JPEG_QUALITY_LADDER:
        _convert(source, destination, quality)
        last_size = destination.stat().st_size
        if last_size <= MAX_DERIVATIVE_BYTES:
            note = f"{facts.image_format} -> jpeg q{quality}, {last_size / 1024 / 1024:.1f} MB"
            derivative_facts = inspect(destination)
            if not derivative_facts.ok:
                raise ConversionError(
                    f"{source.name}: derivative failed validation: "
                    + "; ".join(derivative_facts.errors)
                )
            return Derivative(source, destination, converted=True, note=note)

    raise ConversionError(
        f"{source.name}: even at quality {JPEG_QUALITY_LADDER[-1]} the JPEG is "
        f"{last_size / 1024 / 1024:.1f} MB, over eBay's 12 MB limit. "
        "The source may need downscaling."
    )


# --- derivatives for model input ---------------------------------------------

# The Anthropic API accepts jpeg, png, gif and webp -- not HEIC -- so a phone photo
# needs converting for the model exactly as it does for eBay. It is also billed by
# image area, and resolution beyond roughly 1568px on the long edge buys nothing,
# so the model derivative is downscaled as well as converted. That is the only
# difference from the upload derivative, and the reason it is cached separately.
MODEL_MAX_EDGE = 1568
MODEL_JPEG_QUALITY = 85


def _downscale_with_sips(source: Path, destination: Path,
                         max_edge: int = MODEL_MAX_EDGE,
                         quality: int = MODEL_JPEG_QUALITY) -> None:
    result = subprocess.run(
        [
            "sips",
            "-Z", str(max_edge),
            "-s", "format", "jpeg",
            "-s", "formatOptions", str(quality),
            str(source),
            "--out", str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0 or not destination.exists():
        raise ConversionError(
            f"sips failed preparing {source.name} at {max_edge}px "
            f"(exit {result.returncode}): {(result.stderr or result.stdout or '').strip()[:300]}"
        )


def _downscale_with_pillow(source: Path, destination: Path,
                           max_edge: int = MODEL_MAX_EDGE,
                           quality: int = MODEL_JPEG_QUALITY) -> None:
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:
        raise ConversionError(
            f"Cannot prepare {source.name}: neither `sips` (macOS) nor Pillow is "
            "available."
        ) from exc

    with Image.open(source) as image:
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        image.thumbnail((max_edge, max_edge))
        image.save(destination, format="JPEG", quality=quality, optimize=True)


# --- what the browser gets ----------------------------------------------------
#
# The model derivative is the wrong size for a screen. It exists to keep detail a
# vision model can read, so it is around 600 KB -- barely half an original, and
# the phone is not looking for a serial number.
#
# The shelf renders one card-sized crop per item and there were 34 of them: 36.3
# MB and 35 requests in a single page load, against twelve Waitress threads. That
# is where the queue depth of 37 in the log came from. The workspace shows one
# photograph large, which is a different size and a different judgement.
#
# Measured on a real 1.9 MB capture: 400px/q70 is 53 KB, 1000px/q70 is about 350
# KB. So the shelf drops roughly twentyfold and stays sharp on a phone, and the
# workspace still shows something worth looking at.
#
# Originals are never touched. Everything that reasons about or sells the object
# -- the vision stage, eBay, the integrity check at publish -- keeps reading the
# bytes the camera produced, because a resampled image is evidence of a different
# thing.
UI_SIZES: dict[str, tuple[int, int]] = {
    "thumb": (400, 70),
    "view": (1000, 72),
}


def for_ui(source: str | Path, cache_dir: str | Path, size: str = "thumb", *,
           digest: str | None = None) -> Path:
    """A screen-sized JPEG, cached by source content and size.

    Same cache-by-digest shape as `for_model`, so a re-upload of a file the item
    already has lands on the same path rather than converting twice.
    """
    if size not in UI_SIZES:
        raise ConversionError(f"unknown UI size {size!r}; have {sorted(UI_SIZES)}")
    max_edge, quality = UI_SIZES[size]

    source = Path(source)
    if not source.exists():
        raise ConversionError(f"{source} does not exist")

    digest = digest or source_digest(source)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{digest[:16]}-{size}.jpg"
    if destination.exists() and destination.stat().st_size > 0:
        return destination

    if shutil.which("sips"):
        _downscale_with_sips(source, destination, max_edge, quality)
    else:
        _downscale_with_pillow(source, destination, max_edge, quality)
    return destination


def for_model(source: str | Path, cache_dir: str | Path, *, digest: str | None = None) -> Path:
    """A JPEG suitable for sending to a vision model, cached by source content."""
    source = Path(source)
    if not source.exists():
        raise ConversionError(f"{source} does not exist")

    digest = digest or source_digest(source)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / f"{digest[:16]}-model.jpg"
    if destination.exists() and destination.stat().st_size > 0:
        return destination

    if shutil.which("sips"):
        _downscale_with_sips(source, destination)
    else:
        _downscale_with_pillow(source, destination)
    return destination

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

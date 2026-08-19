"""Local pre-flight validation of listing photos. No network, no dependencies.

Every check here mirrors a documented eBay limit, so a photo that fails locally
would have failed remotely -- the point is to fail in microseconds instead of
spending an API call and a rate-limit slot to be told the same thing.

Dimensions are read by parsing file headers directly rather than via Pillow.
Photos come off an iPhone as HEIC, and Pillow needs pillow-heif for that; header
parsing covers every format eBay accepts with zero dependencies. Where a format
resists parsing, the result is "unknown" and the dimension check is skipped --
eBay remains the authority, and guessing would be worse than deferring.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from pathlib import Path

# eBay's documented limits.
MAX_FILE_BYTES = 12 * 1024 * 1024  # error 190201
MAX_DIMENSION_SUM = 15_000  # error 190202: height + width
MAX_IMAGES_PER_LISTING = 24
RECOMMENDED_MIN_LONGEST_SIDE = 500  # below this, eBay may reject or degrade
RECOMMENDED_ZOOM_LONGEST_SIDE = 1600  # below this, no zoom on the listing page

# error 190203 is returned for anything outside this set
SUPPORTED_FORMATS = frozenset({"jpeg", "gif", "png", "bmp", "tiff", "avif", "heic", "webp"})

# eBay's Media API docs list all eight formats above as supported, but EPS rejects
# HEIC with error 190203, and the Trading API's own EPS documentation lists only
# JPG, GIF, PNG, BMP and TIF. These are converted to JPEG locally before upload.
NEEDS_LOCAL_CONVERSION = frozenset({"bmp", "tiff", "avif", "heic", "webp"})


@dataclass
class ImageFacts:
    path: Path
    size_bytes: int
    image_format: str | None  # None when unrecognised
    width: int | None
    height: int | None
    animated: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def dimensions(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "unknown"


# --- format sniffing ---------------------------------------------------------


def sniff_format(header: bytes) -> str | None:
    """Identify format from magic bytes. Extensions lie; file contents do not."""
    if header[:3] == b"\xff\xd8\xff":
        return "jpeg"
    if header[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if header[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if header[:2] == b"BM":
        return "bmp"
    if header[:4] in (b"II*\x00", b"MM\x00*"):
        return "tiff"
    if header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "webp"
    if header[4:8] == b"ftyp":
        brand = header[8:12]
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"hevm", b"hevs", b"mif1", b"msf1"):
            return "heic"
        if brand in (b"avif", b"avis"):
            return "avif"
    return None


# --- dimension parsing ------------------------------------------------------


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24:
        return None
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 10:
        return None
    width, height = struct.unpack("<HH", data[6:10])
    return width, height


def _bmp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 26:
        return None
    width, height = struct.unpack("<ii", data[18:26])
    return abs(width), abs(height)


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Walk the marker segments to the frame header.

    JPEG puts dimensions in a Start Of Frame marker whose position depends on how
    much EXIF the camera wrote, so there is no fixed offset to read.
    """
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment_length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        # SOF0..SOF15, excluding DHT (C4), JPG (C8) and DAC (CC)
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return width, height
        index += 2 + segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8 " and len(data) >= 30:
        width, height = struct.unpack("<HH", data[26:30])
        return width & 0x3FFF, height & 0x3FFF
    if chunk == b"VP8L" and len(data) >= 25:
        bits = struct.unpack("<I", data[21:25])[0]
        return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if chunk == b"VP8X" and len(data) >= 30:
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return width, height
    return None


def _tiff_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 8:
        return None
    endian = "<" if data[:2] == b"II" else ">"
    offset = struct.unpack(endian + "I", data[4:8])[0]
    if offset + 2 > len(data):
        return None
    count = struct.unpack(endian + "H", data[offset : offset + 2])[0]
    width = height = None
    for entry in range(count):
        base = offset + 2 + entry * 12
        if base + 12 > len(data):
            break
        tag, field_type = struct.unpack(endian + "HH", data[base : base + 4])
        if tag not in (256, 257):
            continue
        raw = data[base + 8 : base + 12]
        value = (
            struct.unpack(endian + "H", raw[:2])[0]
            if field_type == 3
            else struct.unpack(endian + "I", raw)[0]
        )
        if tag == 256:
            width = value
        else:
            height = value
    return (width, height) if width and height else None


def _iso_bmff_dimensions(data: bytes) -> None:
    """Deliberately does not parse HEIC/AVIF dimensions. Returns None.

    An earlier version scanned for the first `ispe` (ImageSpatialExtents) box and
    read the two uint32s after it. That is wrong for real iPhone photos: HEIC
    stores the main image as a *grid of tiles*, each tile carrying its own ispe,
    so the scan returned a 512x512 tile size for a 12 MP photo. Confidently wrong
    dimensions are worse than none -- they produced fabricated resolution warnings
    and would have let a genuinely oversized image past the 15,000 px check.

    Reading it correctly means walking meta > iprp > ipco > ispe and resolving the
    primary item via pitm and ipma associations. That is a real parser, and it is
    not worth writing here: every HEIC is converted to a JPEG derivative before
    upload anyway, and the derivative's dimensions are both trivially parseable
    and the ones that actually reach eBay.
    """
    return None


_PARSERS = {
    "png": _png_dimensions,
    "gif": _gif_dimensions,
    "bmp": _bmp_dimensions,
    "jpeg": _jpeg_dimensions,
    "webp": _webp_dimensions,
    "tiff": _tiff_dimensions,
    "heic": _iso_bmff_dimensions,
    "avif": _iso_bmff_dimensions,
}


# Above this, claimed dimensions are not credible for a compressed format and are
# treated as unparsed. A 512x512 image cannot occupy 3 MB; that ratio is exactly
# what exposed the HEIC tile-parsing bug, so it is now checked rather than trusted.
MAX_CREDIBLE_BYTES_PER_PIXEL = 8.0
MIN_PIXELS_FOR_CREDIBILITY_CHECK = 10_000  # skip tiny images, where overhead dominates


def _dimensions_are_credible(width: int, height: int, size_bytes: int) -> bool:
    pixels = width * height
    if pixels < MIN_PIXELS_FOR_CREDIBILITY_CHECK:
        return True
    return (size_bytes / pixels) <= MAX_CREDIBLE_BYTES_PER_PIXEL


def _is_animated_gif(data: bytes) -> bool:
    """More than one Graphic Control Extension implies multiple frames."""
    return data.count(b"\x21\xf9\x04") > 1


# --- the validator -----------------------------------------------------------


def inspect(path: str | Path, *, read_bytes: int = 512 * 1024) -> ImageFacts:
    """Read one image's facts and check them against eBay's limits.

    Only the head of the file is read; every format above stores dimensions near
    the start. HEIC is the exception where ispe can sit further in, which is why
    the window is generous rather than a few hundred bytes.
    """
    path = Path(path)
    if not path.exists():
        return ImageFacts(path, 0, None, None, None, errors=["file does not exist"])
    if path.is_dir():
        return ImageFacts(path, 0, None, None, None, errors=["path is a directory"])

    size = path.stat().st_size
    with path.open("rb") as handle:
        head = handle.read(read_bytes)

    image_format = sniff_format(head)
    dimensions = _PARSERS.get(image_format, lambda _: None)(head) if image_format else None
    width, height = dimensions if dimensions else (None, None)

    # Discard dimensions that cannot be true for the file's size. A parser reading
    # the wrong header field fails this check, which keeps a subtle parsing bug
    # from becoming a silently wrong validation result.
    implausible = False
    if width and height and not _dimensions_are_credible(width, height, size):
        implausible = True
        width = height = None

    animated = image_format == "gif" and _is_animated_gif(head)

    facts = ImageFacts(path, size, image_format, width, height, animated)

    if size == 0:
        facts.errors.append("file is empty")
    elif size > MAX_FILE_BYTES:
        facts.errors.append(
            f"{size / 1024 / 1024:.1f} MB exceeds eBay's 12 MB limit (would be error 190201)"
        )

    if image_format is None:
        facts.errors.append(
            "unrecognised image format -- eBay accepts "
            f"{', '.join(sorted(SUPPORTED_FORMATS))} (would be error 190203)"
        )
    elif image_format not in SUPPORTED_FORMATS:
        facts.errors.append(f"{image_format} is not accepted by eBay (would be error 190203)")

    if animated:
        facts.errors.append("animated GIFs are not supported; animation is lost on upload")

    if width and height:
        if width + height > MAX_DIMENSION_SUM:
            facts.errors.append(
                f"{width}+{height}={width + height} exceeds the 15,000 px "
                "height+width limit (would be error 190202)"
            )
        longest = max(width, height)
        if longest < RECOMMENDED_MIN_LONGEST_SIDE:
            facts.warnings.append(
                f"longest side {longest}px is below eBay's {RECOMMENDED_MIN_LONGEST_SIDE}px minimum"
            )
        elif longest < RECOMMENDED_ZOOM_LONGEST_SIDE:
            facts.warnings.append(
                f"longest side {longest}px is below {RECOMMENDED_ZOOM_LONGEST_SIDE}px, "
                "so the listing will not offer zoom"
            )
    elif image_format:
        if implausible:
            facts.warnings.append(
                "parsed dimensions were implausible for the file size and were "
                "discarded; the limit will be checked on the converted derivative"
            )
        elif image_format in ("heic", "avif"):
            facts.warnings.append(
                "dimensions not read for HEIC/AVIF; they are checked on the JPEG "
                "derivative that is actually uploaded"
            )
        else:
            facts.warnings.append(
                "could not read dimensions locally; eBay will enforce the limit server-side"
            )

    if image_format in NEEDS_LOCAL_CONVERSION:
        facts.warnings.append(
            f"{image_format} is rejected by EPS (error 190203); a JPEG derivative "
            "will be created and uploaded instead"
        )

    return facts


def inspect_all(paths: list[str | Path]) -> tuple[list[ImageFacts], list[str]]:
    """Validate a listing's photo set. Returns per-file facts and set-level errors."""
    results = [inspect(path) for path in paths]
    set_errors: list[str] = []

    if not results:
        set_errors.append("no images supplied; a listing needs at least one")
    if len(results) > MAX_IMAGES_PER_LISTING:
        set_errors.append(
            f"{len(results)} images exceeds the {MAX_IMAGES_PER_LISTING} per listing limit"
        )

    seen: dict[Path, int] = {}
    for facts in results:
        resolved = facts.path.resolve()
        seen[resolved] = seen.get(resolved, 0) + 1
    duplicates = [str(p) for p, n in seen.items() if n > 1]
    if duplicates:
        set_errors.append(f"the same file was supplied more than once: {', '.join(duplicates)}")

    return results, set_errors

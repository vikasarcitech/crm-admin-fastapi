"""Upload validation and image derivatives.

Two jobs:

1. **Validate** what arrives. The declared Content-Type is a claim, not
   evidence, so the magic bytes are checked and images are additionally
   decoded — a file that Pillow cannot open is rejected regardless of
   what it calls itself. Pixel count is capped before the full decode so
   a 200 KB "decompression bomb" cannot exhaust memory.

2. **Optimize**. Every raster upload produces a set of WebP (and, when
   the encoder is present, AVIF) derivatives at responsive widths, plus
   an optimized copy in the original format as a fallback. EXIF is
   dropped — it carries GPS coordinates and camera serials that have no
   business on a public site.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from datetime import datetime, timezone

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("crm.imaging")

# Pillow's own bomb guard; ~80 MP is far above any legitimate web asset.
Image.MAX_IMAGE_PIXELS = 80_000_000

MAX_UPLOAD_BYTES = 25 * 1024 * 1024        # 25 MB
MAX_IMAGE_PIXELS = 50_000_000              # 50 MP after orientation fix
MAX_DIMENSION = 8000

# Responsive widths. An original narrower than a width is never upscaled.
VARIANT_WIDTHS: tuple[tuple[str, int], ...] = (
    ("sm", 320), ("md", 640), ("lg", 1024), ("xl", 1600), ("xxl", 2400),
)

WEBP_QUALITY = 82
AVIF_QUALITY = 62
JPEG_QUALITY = 82

# mime → (extensions, magic prefixes). Anything absent is refused.
ALLOWED_TYPES: dict[str, tuple[tuple[str, ...], tuple[bytes, ...]]] = {
    "image/jpeg": ((".jpg", ".jpeg"), (b"\xff\xd8\xff",)),
    "image/png": ((".png",), (b"\x89PNG\r\n\x1a\n",)),
    "image/gif": ((".gif",), (b"GIF87a", b"GIF89a")),
    "image/webp": ((".webp",), (b"RIFF",)),
    "image/avif": ((".avif",), ()),          # brand checked below
    "image/svg+xml": ((".svg",), ()),        # sanitized, never decoded
    "application/pdf": ((".pdf",), (b"%PDF-",)),
    "video/mp4": ((".mp4", ".m4v"), ()),     # ftyp brand checked below
    "video/webm": ((".webm",), (b"\x1a\x45\xdf\xa3",)),
    "audio/mpeg": ((".mp3",), (b"ID3", b"\xff\xfb", b"\xff\xf3")),
    "text/plain": ((".txt",), ()),
    "text/csv": ((".csv",), ()),
}

RASTER_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/avif"}

_SAFE_STEM = re.compile(r"[^a-z0-9]+")


class UploadRejected(ValueError):
    """Validation failure with a message safe to show the uploader."""


# ------------------------------------------------------------- validation
def _sniff(data: bytes) -> str | None:
    """Best-guess mime from magic bytes."""
    for mime, (_, magics) in ALLOWED_TYPES.items():
        if magics and data.startswith(magics):
            # RIFF is also WAV/AVI; require the WEBP fourcc.
            if mime == "image/webp" and data[8:12] != b"WEBP":
                continue
            return mime
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in {b"avif", b"avis"}:
            return "image/avif"
        if brand in {b"isom", b"iso2", b"mp41", b"mp42", b"M4V ", b"avc1"}:
            return "video/mp4"
    head = data[:512].lstrip()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:2048]):
        return "image/svg+xml"
    return None


def validate_upload(filename: str, declared_mime: str | None, data: bytes) -> str:
    """Return the trusted mime type, or raise UploadRejected.

    The returned type comes from the bytes where they are conclusive,
    never from the client's Content-Type.
    """
    if not data:
        raise UploadRejected("That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadRejected(
            f"That file is {len(data) / 1048576:.1f} MB. The limit is "
            f"{MAX_UPLOAD_BYTES // 1048576} MB."
        )

    extension = ("." + filename.rsplit(".", 1)[-1].lower()) if "." in filename else ""
    sniffed = _sniff(data)
    declared = (declared_mime or "").split(";")[0].strip().lower()

    mime = sniffed or (declared if declared in ALLOWED_TYPES else None)
    if mime is None:
        raise UploadRejected("That file type is not allowed.")
    if mime not in ALLOWED_TYPES:
        raise UploadRejected(f"{mime} files are not allowed.")

    extensions, _ = ALLOWED_TYPES[mime]
    if extension and extension not in extensions:
        # A .php named image/jpeg is the classic double-extension upload.
        raise UploadRejected(f"A {mime} file cannot have a {extension} extension.")

    if mime in RASTER_TYPES:
        _probe_image(data)
    if mime == "image/svg+xml":
        _reject_active_svg(data)

    return mime


def _probe_image(data: bytes) -> tuple[int, int]:
    """Verify it really decodes, and that its size is sane."""
    try:
        with Image.open(io.BytesIO(data)) as probe:
            width, height = probe.size
            if width * height > MAX_IMAGE_PIXELS:
                raise UploadRejected(
                    f"That image is {width}×{height}. The limit is "
                    f"{MAX_IMAGE_PIXELS // 1_000_000} megapixels."
                )
            # verify() walks the whole stream and catches truncation.
            probe.verify()
    except UploadRejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise UploadRejected("That file is not a readable image.") from exc
    return width, height


_SVG_ACTIVE = re.compile(
    rb"<\s*script|on[a-z]+\s*=|javascript:|<\s*foreignObject|xlink:href\s*=\s*['\"]\s*(?!#)",
    re.IGNORECASE,
)


def _reject_active_svg(data: bytes) -> None:
    """SVG is XML that can carry script. Refuse the active constructs
    outright rather than trying to rewrite them."""
    if _SVG_ACTIVE.search(data):
        raise UploadRejected(
            "That SVG contains script or external references. "
            "Export a flattened SVG, or upload a PNG."
        )


# ---------------------------------------------------------------- naming
def storage_key(tenant_id: int, filename: str, data: bytes, extension: str) -> str:
    """Content-addressed, date-partitioned key.

    The digest in the name makes every derivative immutable, so the CDN
    can cache it for a year and a re-upload of the same bytes reuses it.
    """
    stem = _SAFE_STEM.sub("-", filename.rsplit(".", 1)[0].lower()).strip("-")[:60] or "file"
    digest = hashlib.sha256(data).hexdigest()[:10]
    now = datetime.now(timezone.utc)
    return f"t{tenant_id}/{now:%Y/%m}/{stem}-{digest}{extension}"


def checksum(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ----------------------------------------------------------- derivatives
def _load(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    # Honour the EXIF orientation flag, then discard EXIF entirely.
    image = ImageOps.exif_transpose(image) or image
    if image.mode in {"P", "LA", "RGBA"}:
        image = image.convert("RGBA")
    elif image.mode != "RGB":
        image = image.convert("RGB")
    return image


def _encode(image: Image.Image, fmt: str) -> bytes | None:
    buffer = io.BytesIO()
    try:
        if fmt == "webp":
            image.save(buffer, "WEBP", quality=WEBP_QUALITY, method=5)
        elif fmt == "avif":
            image.save(buffer, "AVIF", quality=AVIF_QUALITY)
        elif fmt == "jpeg":
            image.convert("RGB").save(
                buffer, "JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True
            )
        elif fmt == "png":
            image.save(buffer, "PNG", optimize=True)
        else:
            return None
    except (OSError, ValueError, KeyError) as exc:
        # A missing AVIF encoder must not fail the whole upload.
        log.warning("%s encode failed: %s", fmt, exc)
        return None
    return buffer.getvalue()


def _avif_available() -> bool:
    try:
        from PIL import features  # noqa: PLC0415
        return bool(features.check("avif"))
    except Exception:  # pragma: no cover
        return False


def build_variants(
    tenant_id: int, filename: str, data: bytes, mime: str
) -> tuple[dict, list[dict]]:
    """Optimize the original and render responsive derivatives.

    Returns ``(primary, variants)``. ``primary`` is what the media row
    points at; each variant carries the metadata the frontend needs to
    build a ``srcset``. Non-raster files come back with no variants.
    """
    extensions, _ = ALLOWED_TYPES[mime]
    default_ext = extensions[0]

    if mime not in RASTER_TYPES:
        return (
            {
                "key": storage_key(tenant_id, filename, data, default_ext),
                "data": data,
                "mime": mime,
                "width": None,
                "height": None,
                "bytes": len(data),
            },
            [],
        )

    try:
        image = _load(data)
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UploadRejected("That image could not be processed.") from exc

    width, height = image.size
    if max(width, height) > MAX_DIMENSION:
        # Cap the stored original too: nothing on a web page needs 12k px.
        image.thumbnail((MAX_DIMENSION, MAX_DIMENSION), Image.LANCZOS)
        width, height = image.size

    # Animated GIFs lose their animation through this pipeline, so they
    # are stored as-is and simply not given derivatives.
    if mime == "image/gif" and getattr(image, "n_frames", 1) > 1:
        return (
            {
                "key": storage_key(tenant_id, filename, data, ".gif"),
                "data": data,
                "mime": "image/gif",
                "width": width,
                "height": height,
                "bytes": len(data),
            },
            [],
        )

    has_alpha = image.mode == "RGBA"
    fallback_fmt = "png" if has_alpha else "jpeg"
    fallback_ext = ".png" if has_alpha else ".jpg"

    optimized = _encode(image, fallback_fmt) or data
    # Never ship a "optimized" file that is larger than what came in.
    if len(optimized) > len(data) and mime in {"image/jpeg", "image/png"}:
        optimized, fallback_ext = data, extensions[0]

    primary = {
        "key": storage_key(tenant_id, filename, optimized, fallback_ext),
        "data": optimized,
        "mime": f"image/{'png' if fallback_ext == '.png' else 'jpeg'}",
        "width": width,
        "height": height,
        "bytes": len(optimized),
    }

    formats = ["webp"] + (["avif"] if _avif_available() else [])
    variants: list[dict] = []

    for label, target in VARIANT_WIDTHS:
        if target > width:
            continue  # never upscale
        scaled = image.copy()
        scaled.thumbnail((target, target * 10), Image.LANCZOS)
        for fmt in formats:
            encoded = _encode(scaled, fmt)
            if not encoded:
                continue
            variants.append(
                {
                    "label": f"{label}-{fmt}",
                    "key": storage_key(tenant_id, filename, encoded, f".{fmt}"),
                    "data": encoded,
                    "mime": f"image/{fmt}",
                    "width": scaled.width,
                    "height": scaled.height,
                    "bytes": len(encoded),
                }
            )

    # A small image gets no width-based variant; still offer one WebP so
    # every raster asset has a modern-format option.
    if not variants:
        for fmt in formats:
            encoded = _encode(image, fmt)
            if encoded:
                variants.append(
                    {
                        "label": f"orig-{fmt}",
                        "key": storage_key(tenant_id, filename, encoded, f".{fmt}"),
                        "data": encoded,
                        "mime": f"image/{fmt}",
                        "width": width,
                        "height": height,
                        "bytes": len(encoded),
                    }
                )

    return primary, variants

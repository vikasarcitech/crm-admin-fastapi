"""Object storage for the media library.

Two backends, chosen with ``MEDIA_STORAGE``:

  local   files under MEDIA_ROOT, served by the app at /media/{key}.
          Fine for a single box; on Fargate the container filesystem is
          ephemeral, so this is a development default only.
  s3      the production path: an S3 bucket, ideally fronted by
          CloudFront (set MEDIA_PUBLIC_BASE_URL to the distribution
          domain so URLs are CDN-served and the bucket stays private).

Cost note: S3 storage is negligible next to request and egress cost, so
the variant set in app/imaging.py is what actually drives the bill —
serving a 2 MB original where a 40 KB WebP would do is the expensive
mistake, not keeping four extra derivatives.

boto3 is imported lazily so an install that never uses S3 does not pay
for it at startup.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .config import settings

log = logging.getLogger("crm.storage")

# Keys are built by this module, but never trust one that round-tripped
# through the database: '..' in a key would escape MEDIA_ROOT.
SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,300}$")

_s3_client = None


class StorageError(RuntimeError):
    """Backend failure. Handlers turn this into a 502, never a 500 trace."""


def validate_key(key: str) -> str:
    if not SAFE_KEY.match(key or "") or ".." in key or "//" in key:
        raise StorageError("Rejected an unsafe storage key.")
    return key


def local_root() -> Path:
    root = Path(settings.media_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _s3():
    global _s3_client
    if _s3_client is None:
        try:
            import boto3  # noqa: PLC0415 — optional dependency, loaded on demand
        except ImportError as exc:  # pragma: no cover
            raise StorageError(
                "MEDIA_STORAGE=s3 needs boto3. Add it to requirements.txt."
            ) from exc
        if not settings.media_s3_bucket:
            raise StorageError("MEDIA_STORAGE=s3 but MEDIA_S3_BUCKET is not set.")
        _s3_client = boto3.client("s3", region_name=settings.aws_region or None)
    return _s3_client


def _s3_key(key: str) -> str:
    prefix = settings.media_s3_prefix.strip("/")
    return f"{prefix}/{key}" if prefix else key


# ------------------------------------------------------------------- write
def put(key: str, data: bytes, content_type: str) -> None:
    validate_key(key)
    if settings.media_storage == "s3":
        try:
            _s3().put_object(
                Bucket=settings.media_s3_bucket,
                Key=_s3_key(key),
                Body=data,
                ContentType=content_type,
                # Derivatives are immutable: the key carries a content
                # hash, so a long TTL is safe and keeps CDN cost down.
                CacheControl="public, max-age=31536000, immutable",
            )
        except StorageError:
            raise
        except Exception as exc:
            log.error("s3 put failed for %s: %s", key, exc)
            raise StorageError("Could not store the file.") from exc
        return

    path = local_root() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        # Write-then-rename: a crash mid-write must not leave a truncated
        # file that later reads as a valid-looking image.
        temp = path.with_suffix(f"{path.suffix}.part")
        temp.write_bytes(data)
        temp.replace(path)
    except OSError as exc:
        log.error("local put failed for %s: %s", key, exc)
        raise StorageError("Could not store the file.") from exc


# -------------------------------------------------------------------- read
def get(key: str) -> bytes:
    validate_key(key)
    if settings.media_storage == "s3":
        try:
            response = _s3().get_object(Bucket=settings.media_s3_bucket, Key=_s3_key(key))
            return response["Body"].read()
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("Could not read the file.") from exc

    path = local_root() / key
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StorageError("Could not read the file.") from exc


def exists(key: str) -> bool:
    validate_key(key)
    if settings.media_storage == "s3":
        try:
            _s3().head_object(Bucket=settings.media_s3_bucket, Key=_s3_key(key))
            return True
        except Exception:
            return False
    return (local_root() / key).is_file()


# ------------------------------------------------------------------ delete
def delete(key: str) -> None:
    """Best effort: a missing object is success, not an error."""
    validate_key(key)
    if settings.media_storage == "s3":
        try:
            _s3().delete_object(Bucket=settings.media_s3_bucket, Key=_s3_key(key))
        except Exception as exc:
            log.error("s3 delete failed for %s: %s", key, exc)
        return
    try:
        (local_root() / key).unlink(missing_ok=True)
    except OSError as exc:
        log.error("local delete failed for %s: %s", key, exc)


def delete_many(keys: list[str]) -> None:
    for key in keys:
        try:
            delete(key)
        except StorageError as exc:
            log.error("delete skipped for %s: %s", key, exc)


# --------------------------------------------------------------------- URL
def public_url(key: str) -> str:
    """Browser-facing URL. With a CDN in front, this is the CDN's."""
    validate_key(key)
    base = settings.media_public_base_url.rstrip("/")
    if base:
        return f"{base}/{key}"
    if settings.media_storage == "s3":
        region = settings.aws_region or "us-east-1"
        return f"https://{settings.media_s3_bucket}.s3.{region}.amazonaws.com/{_s3_key(key)}"
    return f"/media/{key}"


def describe() -> dict:
    """Backend summary for the admin settings screen."""
    return {
        "backend": settings.media_storage,
        "bucket": settings.media_s3_bucket or None,
        "prefix": settings.media_s3_prefix or None,
        "publicBaseUrl": settings.media_public_base_url or None,
        "localRoot": str(local_root()) if settings.media_storage == "local" else None,
    }

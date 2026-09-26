"""Immutable image bytes and Ark provenance used by the video-input gate."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse


IMAGE_FORMATS = {
    "jpeg": (".jpg", "image/jpeg"),
    "png": (".png", "image/png"),
    "webp": (".webp", "image/webp"),
    "gif": (".gif", "image/gif"),
    "bmp": (".bmp", "image/bmp"),
    "tiff": (".tiff", "image/tiff"),
    "heic": (".heic", "image/heic"),
}


def image_format(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if data.startswith(b"BM"):
        return "bmp"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1"):
        return "heic"
    return ""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ark_account_from_url(url: str) -> str:
    match = re.search(r"/Image/(\d+)/", urlparse(url).path)
    return match.group(1) if match else ""


def signed_url_expiry(url: str) -> datetime | None:
    params = parse_qs(urlparse(url).query)
    try:
        signed_at = datetime.strptime(params["X-Tos-Date"][0], "%Y%m%dT%H%M%SZ")
        return signed_at.replace(tzinfo=timezone.utc) + timedelta(
            seconds=int(params["X-Tos-Expires"][0])
        )
    except (KeyError, IndexError, ValueError, OverflowError):
        return None


def contains_person(item: dict) -> bool:
    provenance = item.get("provenance") or {}
    if provenance.get("contains_person") is True:
        return True
    if provenance.get("contains_person") is False:
        return False
    key = str(item.get("canonical_key") or item.get("reference_key") or "").lower()
    source_kind = str(item.get("source_kind") or "").lower()
    return source_kind in {"character", "characters"} or key.startswith("char_") or (
        key.startswith("combo__") and "char_" in key
    )

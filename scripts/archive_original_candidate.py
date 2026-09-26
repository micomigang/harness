"""Archive one verified original candidate to configured same-account TOS.

Usage: python scripts/archive_original_candidate.py CANDIDATE_ID
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import settings
from app.db import Database
from app.image_provenance import image_format, sha256_file
from app.tos_archive import TosArchive


def main(candidate_id: str) -> None:
    if not settings.ark_account_id or settings.tos_account_id != settings.ark_account_id:
        raise RuntimeError("TOS_ACCOUNT_ID must equal ARK_ACCOUNT_ID")
    archive = TosArchive(
        settings.tos_access_key, settings.tos_secret_key, settings.tos_endpoint,
        settings.tos_region, settings.tos_bucket,
    )
    if not archive.enabled:
        raise RuntimeError("TOS original archive is not fully configured")
    db = Database(settings.data_dir / "harness.db")
    candidate = db.get_asset_candidate(candidate_id)
    if not candidate:
        raise RuntimeError("Candidate not found")
    provenance = dict(candidate.get("provenance") or {})
    path = Path(str(candidate.get("local_path") or ""))
    if path.is_file():
        with path.open("rb") as original:
            detected_format = image_format(original.read(16))
    else:
        detected_format = ""
    if not (
        provenance.get("provider") == "volcengine-ark"
        and provenance.get("account_id") == settings.ark_account_id
        and provenance.get("original_bytes") is True
        and provenance.get("format_verified") is True
        and path.is_file()
        and sha256_file(path) == provenance.get("sha256")
        and detected_format == provenance.get("media_format")
    ):
        raise RuntimeError("Candidate lacks verified, unchanged Ark original bytes")
    uri = archive.upload_original(path, candidate["workspace_id"], provenance["sha256"])
    provenance["tos_uri"] = uri
    provenance["archive_status"] = "archived"
    provenance.pop("archive_error", None)
    with db._lock, db.connect() as conn:
        conn.execute(
            "UPDATE asset_candidates SET provenance_json=? WHERE id=?",
            (json.dumps(provenance, ensure_ascii=False), candidate_id),
        )
    db.add_event(candidate["workspace_id"], "asset_candidate.original_archived", {
        "candidate_id": candidate_id, "tos_uri": uri,
    })
    print(json.dumps({"candidate_id": candidate_id, "tos_uri": uri}))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python scripts/archive_original_candidate.py CANDIDATE_ID")
    main(sys.argv[1])

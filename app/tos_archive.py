"""Optional same-account TOS storage for byte-identical Ark image originals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class TosArchive:
    access_key: str = ""
    secret_key: str = ""
    endpoint: str = ""
    region: str = ""
    bucket: str = ""

    @property
    def enabled(self) -> bool:
        return all((self.access_key, self.secret_key, self.endpoint, self.region, self.bucket))

    def _client(self):
        if not self.enabled:
            raise RuntimeError("TOS original archive is not configured")
        try:
            import tos
        except ImportError as exc:
            raise RuntimeError("TOS original archive requires the optional 'tos' dependency") from exc
        return tos.TosClientV2(
            self.access_key, self.secret_key, self.endpoint, self.region, enable_crc=True
        )

    def upload_original(self, path: Path, workspace_id: str, sha256: str) -> str:
        """Upload the downloaded file unchanged; return a durable TOS object URI."""
        key = f"harness-originals/{workspace_id}/{sha256[:2]}/{sha256}{path.suffix.lower()}"
        client = self._client()
        client.put_object_from_file(self.bucket, key, str(path))
        head = client.head_object(self.bucket, key)
        if int(head.content_length) != path.stat().st_size:
            raise RuntimeError("TOS original archive size verification failed")
        return f"tos://{self.bucket}/{key}"

    def signed_get_url(self, uri: str, *, expires: int = 3600) -> str:
        try:
            import tos
            from tos.enum import HttpMethodType
        except ImportError as exc:
            raise RuntimeError("TOS original archive requires the optional 'tos' dependency") from exc
        prefix = f"tos://{self.bucket}/"
        if not uri.startswith(prefix):
            raise RuntimeError("TOS original is outside the configured bucket")
        key = uri[len(prefix):]
        return self._client().pre_signed_url(
            HttpMethodType.Http_Method_Get, self.bucket, key, expires=expires
        ).signed_url

"""Raw XML evidence storage and verification."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .oai_errors import ArchiveError
from .util import atomic_write_bytes, atomic_write_text, ensure_directory, sha256_bytes


@dataclass(frozen=True)
class ArchivedResponse:
    relative_path: str
    absolute_path: Path
    sha256: str
    size_bytes: int
    metadata_path: str


class ArchiveStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        ensure_directory(self.root)

    def save(
        self,
        *,
        run_id: int,
        page_number: int,
        content: bytes,
        request_url: str,
        params: Mapping[str, str] | None = None,
        fetched_at: str = "",
    ) -> ArchivedResponse:
        if run_id <= 0 or page_number <= 0:
            raise ArchiveError("run_id 和 page_number 必须为正整数")
        run_dir = ensure_directory(self.root / f"run-{run_id:06d}")
        relative_path = f"run-{run_id:06d}/page-{page_number:04d}.xml"
        metadata_relative = f"run-{run_id:06d}/page-{page_number:04d}.meta.json"
        absolute_path = run_dir / f"page-{page_number:04d}.xml"
        metadata_path = run_dir / f"page-{page_number:04d}.meta.json"
        digest = sha256_bytes(content)
        atomic_write_bytes(absolute_path, content)
        metadata = {
            "run_id": run_id,
            "page_number": page_number,
            "request_url": request_url,
            "host": urlsplit(request_url).hostname,
            "params": dict(params or {}),
            "fetched_at": fetched_at,
            "sha256": digest,
            "size_bytes": len(content),
            "content_type": "text/xml",
        }
        atomic_write_text(
            metadata_path,
            json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        return ArchivedResponse(
            relative_path=relative_path,
            absolute_path=absolute_path,
            sha256=digest,
            size_bytes=len(content),
            metadata_path=metadata_relative,
        )

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise ArchiveError(f"归档路径越界: {relative_path}") from exc
        return candidate

    def verify(self, relative_path: str, expected_sha256: str) -> tuple[bool, str]:
        path = self.resolve(relative_path)
        if not path.is_file():
            return False, "missing"
        digest = sha256_bytes(path.read_bytes())
        if digest != expected_sha256:
            return False, "sha256_mismatch"
        return True, "ok"

"""Backup and recovery for files Trace modifies.

Before the *first* change to any file during a task, its original bytes are copied into
``<checkpoint_root>/<task_id>/`` and recorded in ``manifest.json``. Later changes to the same
file don't overwrite that backup, so a restore always returns the state from before the task.

After every change, the hash of the content Trace wrote is recorded. ``restore()`` only puts a
file back when its current content still matches what Trace wrote. If the user edited it since,
the file is skipped (unless ``force=True``), so undo can never destroy work made after Trace.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from trace_agent.errors import TraceError

MANIFEST = "manifest.json"


class CheckpointError(TraceError):
    pass


def _sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class RestoreReport:
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # files Trace created, now removed
    skipped: list[str] = field(default_factory=list)  # changed after Trace; left untouched
    errors: list[str] = field(default_factory=list)


@dataclass
class FileRecord:
    path: str  # project-relative, posix
    existed: bool
    backup: str | None  # file name inside the checkpoint dir
    original_sha: str | None
    current_sha: str | None  # hash of what Trace last wrote (None = Trace deleted it)


class CheckpointStore:
    def __init__(self, checkpoint_root: Path, task_id: str, project_root: Path) -> None:
        self.directory = checkpoint_root / task_id
        self.task_id = task_id
        self.project_root = project_root.resolve()
        self._files: dict[str, FileRecord] = {}
        self._created_at = datetime.now(UTC).isoformat()
        if (self.directory / MANIFEST).is_file():
            self._load()

    # ---- recording ---------------------------------------------------------------------

    def before_change(self, rel_path: str) -> None:
        """Back up ``rel_path`` if this is the first change to it in this task."""
        if rel_path in self._files:
            return
        source = self.project_root / rel_path
        backup_name: str | None = None
        existed = source.is_file()
        if existed:
            (self.directory / "files").mkdir(parents=True, exist_ok=True)
            backup_name = f"files/{len(self._files):04d}_{Path(rel_path).name}"
            shutil.copy2(source, self.directory / backup_name)
        self._files[rel_path] = FileRecord(
            path=rel_path,
            existed=existed,
            backup=backup_name,
            original_sha=_sha256(source) if existed else None,
            current_sha=_sha256(source) if existed else None,
        )
        self._save()

    def after_change(self, rel_path: str) -> None:
        record = self._files.get(rel_path)
        if record is None:
            raise CheckpointError(f"after_change without before_change for {rel_path}")
        record.current_sha = _sha256(self.project_root / rel_path)
        self._save()

    # ---- queries -------------------------------------------------------------------------

    @property
    def records(self) -> list[FileRecord]:
        return list(self._files.values())

    def changed_files(self) -> list[dict[str, str]]:
        """Files whose content differs from the original, with the kind of change."""
        out = []
        for r in self._files.values():
            if r.original_sha == r.current_sha:
                continue
            kind = "created" if not r.existed else ("deleted" if r.current_sha is None else "modified")
            out.append({"path": r.path, "change": kind})
        return out

    # ---- restore ---------------------------------------------------------------------------

    def restore(self, *, force: bool = False) -> RestoreReport:
        report = RestoreReport()
        for record in self._files.values():
            target = self.project_root / record.path
            current = _sha256(target)
            if current == record.original_sha:
                continue  # already in the original state
            if not force and current != record.current_sha:
                report.skipped.append(record.path)
                continue
            try:
                if record.existed and record.backup:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self.directory / record.backup, target)
                    report.restored.append(record.path)
                elif target.exists():
                    target.unlink()
                    report.deleted.append(record.path)
                record.current_sha = _sha256(target)
            except OSError as exc:
                report.errors.append(f"{record.path}: {exc}")
        self._save()
        return report

    # ---- persistence ----------------------------------------------------------------------

    def _save(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        data = {
            "task_id": self.task_id,
            "project_root": str(self.project_root),
            "created_at": self._created_at,
            "files": [r.__dict__ for r in self._files.values()],
        }
        fd, tmp = tempfile.mkstemp(dir=self.directory, prefix=".manifest-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        os.replace(tmp, self.directory / MANIFEST)

    def _load(self) -> None:
        try:
            data = json.loads((self.directory / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"corrupt checkpoint manifest in {self.directory}: {exc}") from None
        self._created_at = data.get("created_at", self._created_at)
        self._files = {f["path"]: FileRecord(**f) for f in data.get("files", [])}

    @classmethod
    def open(cls, directory: Path) -> CheckpointStore:
        """Re-open an existing checkpoint (e.g. for ``trace undo``)."""
        try:
            data = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"no readable checkpoint at {directory}: {exc}") from None
        return cls(directory.parent, data["task_id"], Path(data["project_root"]))

    @staticmethod
    def list_all(checkpoint_root: Path) -> list[Path]:
        """Checkpoint directories, newest first."""
        if not checkpoint_root.is_dir():
            return []
        dirs = [d for d in checkpoint_root.iterdir() if (d / MANIFEST).is_file()]
        return sorted(dirs, key=lambda d: (d / MANIFEST).stat().st_mtime, reverse=True)

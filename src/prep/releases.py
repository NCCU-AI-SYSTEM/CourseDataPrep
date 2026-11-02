"""The releases/ folder: one directory per build, `current` and `previous` symlinks.

    releases/
      115-1_20261102-0300/    data.sql.gz, contract.yaml, manifest.json, prepare_report.md,
                              course_struct.db
      115-1_20261103-0300.partial/    a build that has not finished (resume with --from)
      current  -> 115-1_20261102-0300
      previous -> 115-1_20261101-0300

Directories are never modified after the build renames them from .partial, so `current`
always points at a complete, checked artifact. The location comes from RELEASE_DIR (a
property of the machine), default <repo>/releases.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from .contract import REPO_ROOT

PARTIAL = ".partial"


def root() -> Path:
    return Path(os.environ.get("RELEASE_DIR", os.path.join(REPO_ROOT, "releases")))


def release_name(semester: str, now: datetime) -> str:
    return f"{semester}_{now:%Y%m%d-%H%M}"


def _link(base: Path, name: str) -> str | None:
    p = base / name
    return os.readlink(p) if p.is_symlink() else None


def current(base: Path) -> str | None:
    return _link(base, "current")


def previous(base: Path) -> str | None:
    return _link(base, "previous")


def _point(base: Path, link: str, target: str) -> None:
    """Atomically repoint base/link at target (relative, so the folder can move)."""
    tmp = base / f".{link}.tmp"
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, base / link)


def promote(base: Path, name: str) -> None:
    d = base / name
    if name.endswith(PARTIAL) or not (d / "manifest.json").exists():
        raise SystemExit(f"[release] {name} is not a finished release (no manifest, or still .partial)")
    old = current(base)
    if old == name:
        return
    _point(base, "current", name)
    if old:
        _point(base, "previous", old)


def rollback(base: Path) -> str:
    old, prev = current(base), previous(base)
    if not prev or not (base / prev).is_dir():
        raise SystemExit("[release] no previous release to roll back to")
    _point(base, "current", prev)
    if old:
        _point(base, "previous", old)
    return prev


def listing(base: Path) -> list[Path]:
    """Release directories, newest first (finished and partial)."""
    if not base.is_dir():
        return []
    dirs = [p for p in base.iterdir() if p.is_dir() and not p.is_symlink() and "_" in p.name]
    return sorted(dirs, key=lambda p: p.name.removesuffix(PARTIAL).split("_", 1)[1], reverse=True)


def latest_partial(base: Path, semester: str) -> Path | None:
    for p in listing(base):
        if p.name.endswith(PARTIAL) and p.name.startswith(f"{semester}_"):
            return p
    return None


def file_entry(path: Path) -> dict:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return {"bytes": path.stat().st_size, "sha256": h.hexdigest()}


def write_manifest(d: Path, manifest: dict) -> None:
    (d / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

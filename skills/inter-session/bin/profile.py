"""Per-project identity profile: persists a session's display label so it is
reused across restarts without re-passing --label.

Scope (this feature): label only. The profile is a small JSON file in the data
dir, keyed by the *project root* — the nearest ancestor directory containing a
`.git` entry, else the working directory. Keying by repo root (rather than raw
cwd) means `cd`-ing into a subdirectory of the same repo resolves the same
profile. The stored dict carries a `path`/`updated_at` and room for future
fields, so adding e.g. `team` later needs no migration.

Values are re-validated on load, so a hand-edited or older-format file can
never surface a label the live validation path would reject.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bin import shared


def profiles_dir() -> Path:
    return shared.data_dir() / "profiles"


def project_root(cwd: Optional[str] = None) -> str:
    """Stable per-project key: the nearest ancestor directory containing a
    `.git` entry (a dir for a normal repo, a file for a worktree/submodule),
    else the starting directory. Symlink-resolved so equivalent paths collapse
    to one profile."""
    start = Path(cwd) if cwd else Path.cwd()
    try:
        start = start.resolve()
    except OSError:
        start = start.absolute()
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return str(d)
    return str(start)


def profile_path(cwd: Optional[str] = None) -> Path:
    """Data-dir path of the profile for the current project. The filename is a
    hash we generate from the project root, so no part of it is caller-supplied
    (no path-traversal surface)."""
    digest = hashlib.sha256(project_root(cwd).encode("utf-8")).hexdigest()[:32]
    return profiles_dir() / f"{digest}.json"


def _read(cwd: Optional[str] = None) -> dict:
    try:
        data = json.loads(profile_path(cwd).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def load_label(cwd: Optional[str] = None) -> str:
    """The persisted label for this project, or '' if none/invalid/corrupt."""
    label = _read(cwd).get("label", "")
    if isinstance(label, str) and shared.validate_label(label):
        return label
    return ""


def save_label(label: str, cwd: Optional[str] = None) -> None:
    """Persist `label` for this project; an empty string clears it. Other keys
    in the profile are preserved (forward-compat). An invalid non-empty label
    is a no-op (the live path rejects it separately)."""
    if label and not shared.validate_label(label):
        return
    shared.secure_dir(profiles_dir())
    data = _read(cwd)
    data["path"] = project_root(cwd)
    if label:
        data["label"] = label
    else:
        data.pop("label", None)
    data["updated_at"] = datetime.now(tz=timezone.utc).isoformat()
    shared.atomic_write_text(profile_path(cwd), json.dumps(data))


def resolve_label(explicit: Optional[str], cwd: Optional[str] = None) -> str:
    """Resolve the label to connect with.

    `explicit` is None when the caller passed no explicit label (→ load and
    return the persisted per-project label, or ''), or a string (possibly '')
    when the caller passed one explicitly (→ persist it, '' clearing, and
    return it). The caller is responsible for validating a non-empty explicit
    label first; save_label is a defensive no-op on an invalid one."""
    if explicit is None:
        return load_label(cwd)
    save_label(explicit, cwd)
    return explicit

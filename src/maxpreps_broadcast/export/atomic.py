"""Atomic file writes.  Non-negotiable for broadcast.

Write to a temp file in the *same directory* (same filesystem → ``os.replace``
is atomic), fsync, then swap.  After Effects therefore only ever reads the old
complete file or the new complete file — never a half-written one, which on
air means a corrupt frame or a template error mid-scorebug.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_bytes(path: Path | str, data: bytes) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    return target


def atomic_write_text(path: Path | str, text: str, *, encoding: str = "utf-8") -> Path:
    return atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: Path | str, payload: Any, *, indent: int | None = 2) -> Path:
    text = json.dumps(payload, indent=indent, ensure_ascii=False, default=str)
    return atomic_write_text(path, text + "\n")

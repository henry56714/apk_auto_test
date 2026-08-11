"""Atomic JSON file writes: temp file + flush + fsync + os.replace."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    fsync_fn: Callable[[int], None] = os.fsync,
) -> Path:
    """Atomically replace `path` with `data` serialized as JSON.

    Writes to a sibling temp file, flushes + fsyncs, then `os.replace`s it over
    the target. If anything fails before the replace, the previous file is left
    intact and the temp file is cleaned up.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
            fh.flush()
            fsync_fn(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return path

"""Small file helpers shared by the token store and the snapshot store."""

from __future__ import annotations

import os
from pathlib import Path


def atomic_write(path: Path, text: str, mode: int = 0o644, dir_mode: int = 0o755) -> None:
    """Write via a temp file then rename, so a crash never leaves a half-written file."""
    path.parent.mkdir(mode=dir_mode, parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    tmp.replace(path)

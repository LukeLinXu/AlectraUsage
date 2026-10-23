"""Local disk cache for fetched monthly usage JSON.

Layout: ~/.cache/alectra-plan-compare/<account>_<meter>/<YYYY-MM>.json
Files are written 0600 (usage data is personal); the directory is 0700.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

CACHE_ROOT = Path(os.environ.get("ALECTRA_CACHE_DIR", Path.home() / ".cache" / "alectra-plan-compare"))


class UsageCache:
    def __init__(self, root: Path = CACHE_ROOT):
        self.root = Path(root)

    def _dir(self, account: str, meter: str) -> Path:
        d = self.root / f"{account}_{meter}"
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
        return d

    def _path(self, account: str, meter: str, month: str) -> Path:
        return self._dir(account, meter) / f"{month}.json"

    def load(self, account: str, meter: str, month: str) -> list[dict] | None:
        path = self._path(account, meter, month)
        try:
            data = json.loads(path.read_text())
            return data if isinstance(data, list) else None
        except (OSError, json.JSONDecodeError):
            return None

    def store(self, account: str, meter: str, month: str, entries: list[dict]) -> None:
        path = self._path(account, meter, month)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(entries, fh)
                os.fchmod(fh.fileno(), 0o600)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def months(self, account: str, meter: str) -> list[str]:
        d = self.root / f"{account}_{meter}"
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.json"))

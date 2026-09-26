from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


JsonObject = dict[str, Any]


@dataclass(frozen=True)
class LedgerEntry:
    repo: str
    category: str
    disposition: str
    mode: str
    command: list[str]
    before: JsonObject
    after: JsonObject
    exit_code: int | None
    status: str
    rollback_hint: str
    detail: str = ""


class AuditLedger:
    def __init__(
        self,
        path: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = Path(path).expanduser()
        self._clock = clock or _utc_now

    def record(self, entry: LedgerEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": self._timestamp(), **asdict(entry)}
        with self.path.open("a", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")

    def _timestamp(self) -> str:
        current = self._clock()
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        return current.astimezone(timezone.utc).isoformat(timespec="seconds")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

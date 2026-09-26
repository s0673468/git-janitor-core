"""Read-only logical comparisons of independently supplied SQLite databases."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any


RESULT_SCHEMA = "sqlite-logical-comparison/v2"
HELPER_VERSION = 3


def _finalize(result: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "schema": RESULT_SCHEMA,
        "helper_version": HELPER_VERSION,
        "operation": "logical-sqlite-comparison",
        "restore_performed": False,
        **result,
    }
    payload["evidence_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def _unicase(left: object, right: object) -> int:
    left_text = "" if left is None else str(left)
    right_text = "" if right is None else str(right)
    left_text = left_text.casefold()
    right_text = right_text.casefold()
    return (left_text > right_text) - (left_text < right_text)


def _sqlite_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def _identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _value_for_digest(value: Any) -> list[Any]:
    if isinstance(value, bytes):
        return ["bytes", len(value), hashlib.sha256(value).hexdigest()]
    return [type(value).__name__, value]


def _state(path: Path) -> dict[str, Any]:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(_sqlite_uri(path), uri=True)
        connection.create_collation("unicase", _unicase)
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")

        integrity_ok = connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        metadata = {
            "application_id": connection.execute("PRAGMA application_id").fetchone()[0],
            "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
        }
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name COLLATE BINARY"
            )
        ]
        row_counts: dict[str, int] = {}
        rows_digest = hashlib.sha256()
        for table in tables:
            rows = list(connection.execute(f"SELECT * FROM {_identifier(table)} NOT INDEXED"))
            row_counts[table] = len(rows)
            encoded_rows = [
                json.dumps(
                    [_value_for_digest(value) for value in row],
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for row in rows
            ]
            for encoded in sorted(encoded_rows):
                rows_digest.update(table.encode("utf-8"))
                rows_digest.update(b"\0")
                rows_digest.update(encoded.encode("utf-8"))
                rows_digest.update(b"\n")

        schema_rows = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view') "
            "ORDER BY type COLLATE BINARY, name COLLATE BINARY"
        ).fetchall()
        schema_digest = hashlib.sha256(
            json.dumps(schema_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "integrity_ok": integrity_ok,
            "metadata": metadata,
            "tables": tables,
            "row_counts": row_counts,
            "schema_sha256": schema_digest,
            "logical_sha256": rows_digest.hexdigest(),
        }
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {"error_type": type(exc).__name__}
    finally:
        if connection is not None:
            connection.close()


def compare_sqlite_databases(left: Path, right: Path) -> dict[str, Any]:
    """Compare two SQLite databases without writing to either one.

    This function does not locate a backup, copy an artifact, or restore data.
    Callers must establish the provenance of both inputs separately. A ``MATCH``
    proves only that the two supplied databases are logically equivalent.

    The result intentionally contains only validation facts and hashes, never row
    values. The Anki ``unicase`` collation is registered so its schema and indexed
    tables can be read by the standard-library SQLite driver.
    """

    left = Path(left)
    right = Path(right)
    if left.resolve() == right.resolve():
        return _finalize({
            "status": "UNVERIFIABLE",
            "reason": "independent_inputs_required",
        })
    try:
        if left.exists() and right.exists() and os.path.samefile(left, right):
            return _finalize({
                "status": "UNVERIFIABLE",
                "reason": "independent_inputs_required",
            })
    except OSError:
        pass

    left_state = _state(left)
    right_state = _state(right)
    if "error_type" in left_state or "error_type" in right_state:
        return _finalize({
            "status": "UNVERIFIABLE",
            "reason": "read_only_validation_error",
            "left_error_type": left_state.get("error_type"),
            "right_error_type": right_state.get("error_type"),
        })

    metadata_equal = left_state["metadata"] == right_state["metadata"]
    schema_equal = left_state["schema_sha256"] == right_state["schema_sha256"]
    rows_equal = left_state["logical_sha256"] == right_state["logical_sha256"]
    counts_equal = left_state["row_counts"] == right_state["row_counts"]
    integrity_ok = left_state["integrity_ok"] and right_state["integrity_ok"]
    return _finalize({
        "status": "MATCH"
        if integrity_ok and metadata_equal and schema_equal and rows_equal and counts_equal
        else "MISMATCH",
        "integrity_check": {
            "left": "ok" if left_state["integrity_ok"] else "failed",
            "right": "ok" if right_state["integrity_ok"] else "failed",
        },
        "sqlite_metadata_equal": metadata_equal,
        "schema_equal": schema_equal,
        "logical_rows_equal": rows_equal,
        "row_counts_equal": counts_equal,
        "table_count": len(left_state["tables"]),
    })

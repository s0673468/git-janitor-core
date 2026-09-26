from __future__ import annotations

import sqlite3
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from git_janitor.backup_restore import compare_sqlite_databases


def _unicase(left: object, right: object) -> int:
    left_text = "" if left is None else str(left)
    right_text = "" if right is None else str(right)
    left_text = left_text.casefold()
    right_text = right_text.casefold()
    return (left_text > right_text) - (left_text < right_text)


def _create_anki_like_database(path: Path, reverse: bool = False) -> None:
    connection = sqlite3.connect(path)
    connection.create_collation("unicase", _unicase)
    connection.executescript(
        """
        CREATE TABLE notes (id INTEGER PRIMARY KEY, value TEXT COLLATE unicase);
        CREATE INDEX notes_value_idx ON notes(value COLLATE unicase);
        CREATE TABLE cards (id INTEGER PRIMARY KEY, note_id INTEGER, value BLOB);
        """
    )
    rows = [(index, f"Note {index:03d}") for index in range(1, 80)]
    if reverse:
        rows.reverse()
    connection.executemany("INSERT INTO notes(id, value) VALUES (?, ?)", rows)
    connection.executemany(
        "INSERT INTO cards(id, note_id, value) VALUES (?, ?, ?)",
        [(index, index, f"card-{index}".encode()) for index in range(1, 80)],
    )
    connection.commit()
    connection.close()


class BackupRestoreTests(unittest.TestCase):
    def test_equal_logical_databases_match_with_unicase_collation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            left = Path(temp_dir) / "left.sqlite"
            right = Path(temp_dir) / "right.sqlite"
            _create_anki_like_database(left)
            _create_anki_like_database(right, reverse=True)

            result = compare_sqlite_databases(left, right)

        self.assertEqual(result["status"], "MATCH")
        self.assertEqual(result["schema"], "sqlite-logical-comparison/v2")
        self.assertEqual(result["helper_version"], 3)
        self.assertEqual(result["operation"], "logical-sqlite-comparison")
        self.assertFalse(result["restore_performed"])
        self.assertRegex(result["evidence_sha256"], r"^[0-9a-f]{64}$")
        self.assertTrue(result["schema_equal"])
        self.assertTrue(result["logical_rows_equal"])
        self.assertTrue(result["row_counts_equal"])
        self.assertEqual(result["integrity_check"], {"left": "ok", "right": "ok"})

    def test_same_path_and_hard_link_are_unverifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            left = Path(temp_dir) / "left.sqlite"
            hard_link = Path(temp_dir) / "hard-link.sqlite"
            _create_anki_like_database(left)
            hard_link.hardlink_to(left)

            same_path = compare_sqlite_databases(left, left)
            hard_link_result = compare_sqlite_databases(left, hard_link)

            self.assertEqual(same_path["status"], "UNVERIFIABLE")
            self.assertEqual(same_path["reason"], "independent_inputs_required")
            self.assertEqual(hard_link_result["status"], "UNVERIFIABLE")
            self.assertEqual(hard_link_result["reason"], "independent_inputs_required")

    def test_sqlite_metadata_change_is_a_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            left = Path(temp_dir) / "left.sqlite"
            right = Path(temp_dir) / "right.sqlite"
            _create_anki_like_database(left)
            _create_anki_like_database(right)
            connection = sqlite3.connect(right)
            connection.execute("PRAGMA application_id = 1234")
            connection.execute("PRAGMA user_version = 7")
            connection.commit()
            connection.close()

            result = compare_sqlite_databases(left, right)

        self.assertEqual(result["status"], "MISMATCH")
        self.assertFalse(result["sqlite_metadata_equal"])
        self.assertTrue(result["logical_rows_equal"])

    def test_integrity_failure_is_a_mismatch_even_when_rows_match(self) -> None:
        import git_janitor.backup_restore as backup_restore

        with tempfile.TemporaryDirectory() as temp_dir:
            left = Path(temp_dir) / "left.sqlite"
            right = Path(temp_dir) / "right.sqlite"
            _create_anki_like_database(left)
            _create_anki_like_database(right)
            real_connect = sqlite3.connect

            class FailedIntegrityCursor:
                def fetchone(self) -> tuple[str]:
                    return ("row 1 missing from index",)

            class FailedIntegrityConnection:
                def __init__(self, connection: sqlite3.Connection) -> None:
                    self.connection = connection

                def create_collation(self, *args: object) -> None:
                    self.connection.create_collation(*args)

                def execute(self, sql: str, *args: object):
                    if sql == "PRAGMA integrity_check":
                        return FailedIntegrityCursor()
                    return self.connection.execute(sql, *args)

                def close(self) -> None:
                    self.connection.close()

            def connect(*args: object, **kwargs: object) -> FailedIntegrityConnection:
                return FailedIntegrityConnection(real_connect(*args, **kwargs))

            with mock.patch.object(backup_restore.sqlite3, "connect", connect):
                result = compare_sqlite_databases(left, right)

        self.assertEqual(result["status"], "MISMATCH")
        self.assertEqual(result["integrity_check"], {"left": "failed", "right": "failed"})
        self.assertTrue(result["sqlite_metadata_equal"])
        self.assertTrue(result["schema_equal"])
        self.assertTrue(result["logical_rows_equal"])
        self.assertTrue(result["row_counts_equal"])

    def test_state_scan_starts_one_read_transaction(self) -> None:
        import git_janitor.backup_restore as backup_restore

        with tempfile.TemporaryDirectory() as temp_dir:
            left = Path(temp_dir) / "left.sqlite"
            right = Path(temp_dir) / "right.sqlite"
            _create_anki_like_database(left)
            _create_anki_like_database(right)
            events: list[str] = []
            real_connect = sqlite3.connect

            class SpyConnection:
                def __init__(self, connection: sqlite3.Connection) -> None:
                    self.connection = connection

                def create_collation(self, *args: object) -> None:
                    self.connection.create_collation(*args)

                def execute(self, sql: str, *args: object):
                    events.append(sql)
                    return self.connection.execute(sql, *args)

                def close(self) -> None:
                    self.connection.close()

            def connect(*args: object, **kwargs: object) -> SpyConnection:
                return SpyConnection(real_connect(*args, **kwargs))

            with mock.patch.object(backup_restore.sqlite3, "connect", connect):
                result = compare_sqlite_databases(left, right)

        self.assertEqual(result["status"], "MATCH")
        self.assertIn("BEGIN", events)
        self.assertLess(
            events.index("BEGIN"),
            next(i for i, event in enumerate(events) if event.startswith("PRAGMA integrity_check")),
        )

    def test_concurrent_commit_does_not_create_a_torn_read(self) -> None:
        import git_janitor.backup_restore as backup_restore

        with tempfile.TemporaryDirectory() as temp_dir:
            left = Path(temp_dir) / "left.sqlite"
            right = Path(temp_dir) / "right.sqlite"
            _create_anki_like_database(left)
            _create_anki_like_database(right)
            real_connect = sqlite3.connect
            injected = False
            for path in (left, right):
                connection = real_connect(path)
                connection.execute("PRAGMA journal_mode=WAL")
                connection.close()

            class SnapshotCursor:
                def __init__(self, cursor: sqlite3.Cursor, on_first_row) -> None:
                    self.cursor = cursor
                    self.on_first_row = on_first_row

                def fetchone(self):
                    row = self.cursor.fetchone()
                    self.on_first_row()
                    return row

            class SnapshotConnection:
                def __init__(self, connection: sqlite3.Connection, path: Path) -> None:
                    self.connection = connection
                    self.path = path

                def create_collation(self, *args: object) -> None:
                    self.connection.create_collation(*args)

                def execute(self, sql: str, *args: object):
                    cursor = self.connection.execute(sql, *args)
                    if sql != "PRAGMA integrity_check" or self.path.resolve() != left.resolve():
                        return cursor

                    def commit_concurrent_change() -> None:
                        nonlocal injected
                        if injected:
                            return
                        injected = True
                        writer = real_connect(left)
                        writer.create_collation("unicase", _unicase)
                        writer.execute(
                            "INSERT INTO notes(id, value) VALUES (?, ?)",
                            (999, "committed during scan"),
                        )
                        writer.commit()
                        writer.close()

                    return SnapshotCursor(cursor, commit_concurrent_change)

                def close(self) -> None:
                    self.connection.close()

            def connect(database: str, *args: object, **kwargs: object) -> SnapshotConnection:
                path = Path(database.removeprefix("file:").split("?", 1)[0])
                return SnapshotConnection(real_connect(database, *args, **kwargs), path)

            with mock.patch.object(backup_restore.sqlite3, "connect", connect):
                result = compare_sqlite_databases(left, right)

        self.assertTrue(injected)
        self.assertEqual(result["status"], "MATCH")

    def test_logical_change_is_a_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            left = Path(temp_dir) / "left.sqlite"
            right = Path(temp_dir) / "right.sqlite"
            _create_anki_like_database(left)
            _create_anki_like_database(right)
            connection = sqlite3.connect(right)
            connection.create_collation("unicase", _unicase)
            connection.execute("UPDATE notes SET value = 'changed' WHERE id = 1")
            connection.commit()
            connection.close()

            result = compare_sqlite_databases(left, right)

        self.assertEqual(result["status"], "MISMATCH")
        self.assertTrue(result["schema_equal"])
        self.assertFalse(result["logical_rows_equal"])

    def test_invalid_database_is_unverifiable(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            left = Path(temp_dir) / "left.sqlite"
            right = Path(temp_dir) / "right.sqlite"
            left.write_bytes(b"not a database")
            right.write_bytes(b"not a database")

            result = compare_sqlite_databases(left, right)

        self.assertEqual(result["status"], "UNVERIFIABLE")
        self.assertEqual(result["reason"], "read_only_validation_error")

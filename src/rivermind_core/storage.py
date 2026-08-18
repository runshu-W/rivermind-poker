from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from rivermind_core.importer import (
    ImportBatchReport,
    ImportItemResult,
    ImportItemStatus,
)
from rivermind_core.models import HandHistory
from rivermind_core.serialization import hand_from_json, hand_to_json


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS import_batches (
    batch_id TEXT PRIMARY KEY,
    source_name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    detected_count INTEGER NOT NULL DEFAULT 0,
    imported_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    unsupported_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS hands (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    site TEXT NOT NULL,
    hand_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL UNIQUE,
    parser_name TEXT NOT NULL,
    source_name TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    raw_text TEXT NOT NULL,
    UNIQUE(site, hand_id)
);

CREATE INDEX IF NOT EXISTS idx_hands_site_hand_id ON hands(site, hand_id);

CREATE TABLE IF NOT EXISTS import_items (
    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES import_batches(batch_id),
    item_index INTEGER NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    status TEXT NOT NULL,
    source_site TEXT,
    parser_name TEXT,
    hand_id TEXT,
    fingerprint TEXT,
    error_code TEXT,
    message TEXT,
    raw_text TEXT NOT NULL,
    UNIQUE(batch_id, item_index)
);

CREATE INDEX IF NOT EXISTS idx_import_items_batch ON import_items(batch_id, item_index);
PRAGMA user_version = 1;
"""


class SQLiteHandStore:
    """Local Beta store with durable import audit history and hand replay."""

    def __init__(self, database_path: str | Path = "data/rivermind.db") -> None:
        path_text = str(database_path)
        if path_text != ":memory:":
            Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path_text)
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript(SCHEMA)
        self._active_batch: str | None = None

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> SQLiteHandStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def begin_batch(
        self, batch_id: str, source_name: str, started_at: datetime
    ) -> None:
        if self._active_batch is not None:
            raise RuntimeError(f"Import batch already active: {self._active_batch}")
        self._connection.execute("BEGIN")
        self._connection.execute(
            """
            INSERT INTO import_batches(batch_id, source_name, started_at)
            VALUES (?, ?, ?)
            """,
            (batch_id, source_name, started_at.isoformat()),
        )
        self._active_batch = batch_id

    def store_hand(
        self,
        hand: HandHistory,
        *,
        fingerprint: str,
        parser_name: str,
        source_name: str,
        imported_at: datetime,
    ) -> bool:
        cursor = self._connection.execute(
            """
            INSERT OR IGNORE INTO hands(
                site, hand_id, fingerprint, parser_name, source_name,
                imported_at, payload_json, raw_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hand.site,
                hand.hand_id,
                fingerprint,
                parser_name,
                source_name,
                imported_at.isoformat(),
                hand_to_json(hand),
                hand.raw_text,
            ),
        )
        return cursor.rowcount == 1

    def record_item(
        self,
        batch_id: str,
        item: ImportItemResult,
        raw_text: str,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO import_items(
                batch_id, item_index, start_line, end_line, status,
                source_site, parser_name, hand_id, fingerprint,
                error_code, message, raw_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                item.index,
                item.start_line,
                item.end_line,
                item.status.value,
                item.source_site,
                item.parser_name,
                item.hand_id,
                item.fingerprint,
                item.error_code,
                item.message,
                raw_text,
            ),
        )

    def complete_batch(self, report: ImportBatchReport) -> None:
        if self._active_batch != report.batch_id:
            raise RuntimeError(f"Import batch is not active: {report.batch_id}")
        self._connection.execute(
            """
            UPDATE import_batches
            SET completed_at = ?, detected_count = ?, imported_count = ?,
                duplicate_count = ?, failed_count = ?, unsupported_count = ?
            WHERE batch_id = ?
            """,
            (
                report.completed_at.isoformat(),
                report.detected,
                report.imported,
                report.duplicates,
                report.failed,
                report.unsupported,
                report.batch_id,
            ),
        )
        self._connection.commit()
        self._active_batch = None

    def abort_batch(self, batch_id: str) -> None:
        if self._active_batch == batch_id:
            self._connection.rollback()
            self._active_batch = None

    def load_hand(self, site: str, hand_id: str) -> HandHistory | None:
        row = self._connection.execute(
            "SELECT payload_json, raw_text FROM hands WHERE site = ? AND hand_id = ?",
            (site, hand_id),
        ).fetchone()
        if row is None:
            return None
        return hand_from_json(row["payload_json"], raw_text=row["raw_text"])

    def get_batch_report(self, batch_id: str) -> ImportBatchReport | None:
        batch = self._connection.execute(
            "SELECT * FROM import_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
        if batch is None or batch["completed_at"] is None:
            return None
        rows = self._connection.execute(
            """
            SELECT * FROM import_items
            WHERE batch_id = ? ORDER BY item_index
            """,
            (batch_id,),
        ).fetchall()
        items = tuple(
            ImportItemResult(
                index=row["item_index"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                status=ImportItemStatus(row["status"]),
                source_site=row["source_site"],
                parser_name=row["parser_name"],
                hand_id=row["hand_id"],
                fingerprint=row["fingerprint"],
                error_code=row["error_code"],
                message=row["message"],
            )
            for row in rows
        )
        return ImportBatchReport(
            batch_id=batch["batch_id"],
            source_name=batch["source_name"],
            started_at=datetime.fromisoformat(batch["started_at"]),
            completed_at=datetime.fromisoformat(batch["completed_at"]),
            items=items,
        )

    def load_import_item_raw(self, batch_id: str, item_index: int) -> str | None:
        row = self._connection.execute(
            """
            SELECT raw_text FROM import_items
            WHERE batch_id = ? AND item_index = ?
            """,
            (batch_id, item_index),
        ).fetchone()
        return None if row is None else str(row["raw_text"])

    def hand_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS count FROM hands").fetchone()
        assert row is not None
        return int(row["count"])

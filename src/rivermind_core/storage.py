from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from rivermind_core.accounting import HandLedger, calculate_hand_ledger
from rivermind_core.importer import (
    ImportBatchReport,
    ImportItemResult,
    ImportItemStatus,
)
from rivermind_core.models import GameType, HandHistory, PlayerPosition
from rivermind_core.replay import HandReplay, build_hand_replay
from rivermind_core.reports import (
    HandQuery,
    METRICS_WITH_OPPORTUNITIES,
    PlayerHandReport,
)
from rivermind_core.serialization import hand_from_json, hand_to_json
from rivermind_core.sessions import (
    PlayerHandOutcome,
    SessionSummary,
    parse_played_at,
    summarize_sessions,
)
from rivermind_core.stats import (
    PlayerHandStatRow,
    PlayerStats,
    StatsFilter,
    aggregate_player_stat_rows,
    build_player_hand_stat_rows,
)


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

CREATE TABLE IF NOT EXISTS player_hand_stats (
    hand_row_id INTEGER NOT NULL REFERENCES hands(row_id) ON DELETE CASCADE,
    player_name TEXT NOT NULL,
    is_hero INTEGER NOT NULL,
    game_type TEXT NOT NULL,
    tournament_id TEXT,
    position TEXT NOT NULL,
    starting_stack_bb REAL NOT NULL,
    effective_stack_bb REAL NOT NULL,
    vpip INTEGER NOT NULL,
    pfr INTEGER NOT NULL,
    rfi_opportunity INTEGER NOT NULL,
    rfi INTEGER NOT NULL,
    three_bet_opportunity INTEGER NOT NULL,
    three_bet INTEGER NOT NULL,
    call_open_opportunity INTEGER NOT NULL,
    call_open INTEGER NOT NULL,
    cold_call_opportunity INTEGER NOT NULL,
    cold_call INTEGER NOT NULL,
    fold_to_three_bet_opportunity INTEGER NOT NULL,
    fold_to_three_bet INTEGER NOT NULL,
    flop_cbet_opportunity INTEGER NOT NULL,
    flop_cbet INTEGER NOT NULL,
    fold_to_flop_cbet_opportunity INTEGER NOT NULL,
    fold_to_flop_cbet INTEGER NOT NULL,
    PRIMARY KEY(hand_row_id, player_name)
);

CREATE INDEX IF NOT EXISTS idx_player_stats_player ON player_hand_stats(player_name);
CREATE INDEX IF NOT EXISTS idx_player_stats_hero ON player_hand_stats(is_hero);
CREATE INDEX IF NOT EXISTS idx_player_stats_dimensions
    ON player_hand_stats(game_type, position, effective_stack_bb);

CREATE TABLE IF NOT EXISTS player_hand_results (
    hand_row_id INTEGER NOT NULL REFERENCES hands(row_id) ON DELETE CASCADE,
    player_name TEXT NOT NULL,
    played_at TEXT,
    currency TEXT,
    table_name TEXT NOT NULL,
    invested TEXT NOT NULL,
    returned TEXT NOT NULL,
    collected TEXT NOT NULL,
    net_result TEXT NOT NULL,
    net_result_bb TEXT NOT NULL,
    accounting_balanced INTEGER NOT NULL,
    PRIMARY KEY(hand_row_id, player_name)
);

CREATE INDEX IF NOT EXISTS idx_player_results_player_time
    ON player_hand_results(player_name, played_at);
CREATE INDEX IF NOT EXISTS idx_player_results_time
    ON player_hand_results(played_at);
"""


class SQLiteHandStore:
    """Local Beta store with durable import audit history and hand replay."""

    def __init__(self, database_path: str | Path = "data/rivermind.db") -> None:
        path_text = str(database_path)
        if path_text != ":memory:":
            Path(database_path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(path_text)
        self._connection.row_factory = sqlite3.Row
        previous_version = int(
            self._connection.execute("PRAGMA user_version").fetchone()[0]
        )
        if previous_version > 3:
            self._connection.close()
            raise RuntimeError(
                f"Database schema v{previous_version} is newer than supported v3"
            )
        self._connection.executescript(SCHEMA)
        self._active_batch: str | None = None
        if previous_version < 2:
            self._backfill_player_hand_stats()
        if previous_version < 3:
            self._backfill_player_hand_results()
            self._connection.execute("PRAGMA user_version = 3")

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
        imported = cursor.rowcount == 1
        if imported:
            self._insert_player_hand_stats(
                int(cursor.lastrowid), build_player_hand_stat_rows(hand)
            )
            self._insert_player_hand_results(
                int(cursor.lastrowid), hand, calculate_hand_ledger(hand)
            )
        return imported

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

    def iter_hands(
        self, *, batch_size: int = 1000, include_raw: bool = False
    ) -> Iterator[HandHistory]:
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        columns = "payload_json, raw_text" if include_raw else "payload_json"
        cursor = self._connection.execute(
            f"SELECT {columns} FROM hands ORDER BY row_id"
        )
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                raw_text = row["raw_text"] if include_raw else ""
                yield hand_from_json(row["payload_json"], raw_text=raw_text)

    def query_player_stats(
        self,
        *,
        player_name: str | None = None,
        heroes_only: bool = False,
        stat_filter: StatsFilter | None = None,
    ) -> tuple[PlayerStats, ...]:
        rows = self.iter_player_hand_stat_rows(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=stat_filter,
        )
        return aggregate_player_stat_rows(rows)

    def iter_player_hand_stat_rows(
        self,
        *,
        player_name: str | None = None,
        heroes_only: bool = False,
        stat_filter: StatsFilter | None = None,
        batch_size: int = 1000,
    ) -> Iterator[PlayerHandStatRow]:
        if player_name is not None and heroes_only:
            raise ValueError("player_name and heroes_only cannot be combined")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        conditions, parameters = self._stat_conditions(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=stat_filter or StatsFilter(),
        )
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cursor = self._connection.execute(
            f"""
            SELECT h.site, h.hand_id, s.*
            FROM player_hand_stats AS s
            JOIN hands AS h ON h.row_id = s.hand_row_id
            {where_clause}
            ORDER BY h.row_id, s.player_name
            """,
            parameters,
        )
        while rows := cursor.fetchmany(batch_size):
            for row in rows:
                yield self._stat_row_from_sqlite(row)

    def query_hands(
        self,
        *,
        player_name: str | None = None,
        heroes_only: bool = False,
        query: HandQuery | None = None,
    ) -> tuple[PlayerHandReport, ...]:
        active_query = query or HandQuery()
        conditions, parameters = self._stat_conditions(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=active_query.stat_filter,
        )
        if active_query.started_at is not None:
            conditions.append("r.played_at >= ?")
            parameters.append(active_query.started_at.isoformat())
        if active_query.ended_at is not None:
            conditions.append("r.played_at <= ?")
            parameters.append(active_query.ended_at.isoformat())
        if active_query.metric is not None:
            metric_column = active_query.metric.value
            if active_query.metric in METRICS_WITH_OPPORTUNITIES:
                conditions.append(f"s.{metric_column}_opportunity = 1")
            if active_query.occurred is not None:
                conditions.append(f"s.{metric_column} = ?")
                parameters.append(int(active_query.occurred))
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        parameters.extend([active_query.limit, active_query.offset])
        rows = self._connection.execute(
            f"""
            SELECT h.site, h.hand_id, s.*, r.played_at, r.currency,
                   r.table_name, r.invested, r.returned, r.collected,
                   r.net_result, r.net_result_bb, r.accounting_balanced
            FROM player_hand_stats AS s
            JOIN hands AS h ON h.row_id = s.hand_row_id
            JOIN player_hand_results AS r
              ON r.hand_row_id = s.hand_row_id
             AND r.player_name = s.player_name
            {where_clause}
            ORDER BY r.played_at IS NULL, r.played_at DESC, h.row_id DESC
            LIMIT ? OFFSET ?
            """,
            parameters,
        ).fetchall()
        return tuple(self._report_from_sqlite(row) for row in rows)

    def query_sessions(
        self,
        *,
        player_name: str | None = None,
        heroes_only: bool = False,
        stat_filter: StatsFilter | None = None,
        cash_gap: timedelta = timedelta(minutes=30),
    ) -> tuple[SessionSummary, ...]:
        conditions, parameters = self._stat_conditions(
            player_name=player_name,
            heroes_only=heroes_only,
            stat_filter=stat_filter or StatsFilter(),
        )
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._connection.execute(
            f"""
            SELECT h.site, h.hand_id, s.player_name, s.game_type,
                   s.tournament_id, r.currency, r.table_name, r.played_at,
                   r.net_result, r.net_result_bb, r.accounting_balanced
            FROM player_hand_stats AS s
            JOIN hands AS h ON h.row_id = s.hand_row_id
            JOIN player_hand_results AS r
              ON r.hand_row_id = s.hand_row_id
             AND r.player_name = s.player_name
            {where_clause}
            ORDER BY r.played_at, h.row_id
            """,
            parameters,
        ).fetchall()
        outcomes = tuple(
            PlayerHandOutcome(
                site=row["site"],
                hand_id=row["hand_id"],
                player_name=row["player_name"],
                game_type=GameType(row["game_type"]),
                currency=row["currency"],
                tournament_id=row["tournament_id"],
                table_name=row["table_name"],
                played_at=(
                    None
                    if row["played_at"] is None
                    else datetime.fromisoformat(row["played_at"])
                ),
                net_result=Decimal(row["net_result"]),
                net_result_bb=Decimal(row["net_result_bb"]),
                accounting_balanced=bool(row["accounting_balanced"]),
            )
            for row in rows
        )
        return summarize_sessions(outcomes, cash_gap=cash_gap)

    def load_replay(self, site: str, hand_id: str) -> HandReplay | None:
        hand = self.load_hand(site, hand_id)
        return None if hand is None else build_hand_replay(hand)

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

    def stat_row_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM player_hand_stats"
        ).fetchone()
        assert row is not None
        return int(row["count"])

    def result_row_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS count FROM player_hand_results"
        ).fetchone()
        assert row is not None
        return int(row["count"])

    @staticmethod
    def _stat_conditions(
        *,
        player_name: str | None,
        heroes_only: bool,
        stat_filter: StatsFilter,
    ) -> tuple[list[str], list[object]]:
        if player_name is not None and heroes_only:
            raise ValueError("player_name and heroes_only cannot be combined")
        conditions: list[str] = []
        parameters: list[object] = []
        if player_name is not None:
            conditions.append("s.player_name = ?")
            parameters.append(player_name)
        if heroes_only:
            conditions.append("s.is_hero = 1")
        if stat_filter.game_types:
            values = sorted(item.value for item in stat_filter.game_types)
            conditions.append(f"s.game_type IN ({','.join('?' for _ in values)})")
            parameters.extend(values)
        if stat_filter.positions:
            values = sorted(item.value for item in stat_filter.positions)
            conditions.append(f"s.position IN ({','.join('?' for _ in values)})")
            parameters.extend(values)
        if stat_filter.min_effective_stack_bb is not None:
            conditions.append("s.effective_stack_bb >= ?")
            parameters.append(float(stat_filter.min_effective_stack_bb))
        if stat_filter.max_effective_stack_bb is not None:
            conditions.append("s.effective_stack_bb <= ?")
            parameters.append(float(stat_filter.max_effective_stack_bb))
        return conditions, parameters

    def _backfill_player_hand_stats(self) -> None:
        cursor = self._connection.execute(
            """
            SELECT h.row_id, h.payload_json
            FROM hands AS h
            WHERE NOT EXISTS (
                SELECT 1 FROM player_hand_stats AS s
                WHERE s.hand_row_id = h.row_id
            )
            ORDER BY h.row_id
            """
        )
        with self._connection:
            while rows := cursor.fetchmany(1000):
                for row in rows:
                    hand = hand_from_json(row["payload_json"])
                    self._insert_player_hand_stats(
                        int(row["row_id"]), build_player_hand_stat_rows(hand)
                    )

    def _backfill_player_hand_results(self) -> None:
        cursor = self._connection.execute(
            """
            SELECT h.row_id, h.payload_json
            FROM hands AS h
            WHERE NOT EXISTS (
                SELECT 1 FROM player_hand_results AS r
                WHERE r.hand_row_id = h.row_id
            )
            ORDER BY h.row_id
            """
        )
        with self._connection:
            while rows := cursor.fetchmany(1000):
                for row in rows:
                    hand = hand_from_json(row["payload_json"])
                    self._insert_player_hand_results(
                        int(row["row_id"]), hand, calculate_hand_ledger(hand)
                    )

    def _insert_player_hand_stats(
        self, hand_row_id: int, rows: tuple[PlayerHandStatRow, ...]
    ) -> None:
        self._connection.executemany(
            """
            INSERT OR IGNORE INTO player_hand_stats(
                hand_row_id, player_name, is_hero, game_type, tournament_id,
                position, starting_stack_bb, effective_stack_bb,
                vpip, pfr, rfi_opportunity, rfi,
                three_bet_opportunity, three_bet,
                call_open_opportunity, call_open,
                cold_call_opportunity, cold_call,
                fold_to_three_bet_opportunity, fold_to_three_bet,
                flop_cbet_opportunity, flop_cbet,
                fold_to_flop_cbet_opportunity, fold_to_flop_cbet
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?
            )
            """,
            [
                (
                    hand_row_id,
                    row.player_name,
                    int(row.is_hero),
                    row.game_type.value,
                    row.tournament_id,
                    row.position.value,
                    float(row.starting_stack_bb),
                    float(row.effective_stack_bb),
                    int(row.vpip),
                    int(row.pfr),
                    int(row.rfi_opportunity),
                    int(row.rfi),
                    int(row.three_bet_opportunity),
                    int(row.three_bet),
                    int(row.call_open_opportunity),
                    int(row.call_open),
                    int(row.cold_call_opportunity),
                    int(row.cold_call),
                    int(row.fold_to_three_bet_opportunity),
                    int(row.fold_to_three_bet),
                    int(row.flop_cbet_opportunity),
                    int(row.flop_cbet),
                    int(row.fold_to_flop_cbet_opportunity),
                    int(row.fold_to_flop_cbet),
                )
                for row in rows
            ],
        )

    def _insert_player_hand_results(
        self, hand_row_id: int, hand: HandHistory, ledger: HandLedger
    ) -> None:
        played_at = parse_played_at(hand.played_at_raw)
        self._connection.executemany(
            """
            INSERT OR IGNORE INTO player_hand_results(
                hand_row_id, player_name, played_at, currency, table_name,
                invested, returned, collected, net_result, net_result_bb,
                accounting_balanced
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    hand_row_id,
                    result.player_name,
                    None if played_at is None else played_at.isoformat(),
                    hand.currency,
                    hand.table_name,
                    str(result.invested),
                    str(result.returned),
                    str(result.collected),
                    str(result.net_result),
                    str(result.net_result_bb),
                    int(ledger.is_balanced),
                )
                for result in ledger.results
            ],
        )

    @staticmethod
    def _stat_row_from_sqlite(row: sqlite3.Row) -> PlayerHandStatRow:
        return PlayerHandStatRow(
            site=row["site"],
            hand_id=row["hand_id"],
            game_type=GameType(row["game_type"]),
            tournament_id=row["tournament_id"],
            player_name=row["player_name"],
            is_hero=bool(row["is_hero"]),
            position=PlayerPosition(row["position"]),
            starting_stack_bb=Decimal(str(row["starting_stack_bb"])),
            effective_stack_bb=Decimal(str(row["effective_stack_bb"])),
            vpip=bool(row["vpip"]),
            pfr=bool(row["pfr"]),
            rfi_opportunity=bool(row["rfi_opportunity"]),
            rfi=bool(row["rfi"]),
            three_bet_opportunity=bool(row["three_bet_opportunity"]),
            three_bet=bool(row["three_bet"]),
            call_open_opportunity=bool(row["call_open_opportunity"]),
            call_open=bool(row["call_open"]),
            cold_call_opportunity=bool(row["cold_call_opportunity"]),
            cold_call=bool(row["cold_call"]),
            fold_to_three_bet_opportunity=bool(
                row["fold_to_three_bet_opportunity"]
            ),
            fold_to_three_bet=bool(row["fold_to_three_bet"]),
            flop_cbet_opportunity=bool(row["flop_cbet_opportunity"]),
            flop_cbet=bool(row["flop_cbet"]),
            fold_to_flop_cbet_opportunity=bool(
                row["fold_to_flop_cbet_opportunity"]
            ),
            fold_to_flop_cbet=bool(row["fold_to_flop_cbet"]),
        )

    @classmethod
    def _report_from_sqlite(cls, row: sqlite3.Row) -> PlayerHandReport:
        return PlayerHandReport(
            stats=cls._stat_row_from_sqlite(row),
            played_at=(
                None
                if row["played_at"] is None
                else datetime.fromisoformat(row["played_at"])
            ),
            currency=row["currency"],
            table_name=row["table_name"],
            invested=Decimal(row["invested"]),
            returned=Decimal(row["returned"]),
            collected=Decimal(row["collected"]),
            net_result=Decimal(row["net_result"]),
            net_result_bb=Decimal(row["net_result_bb"]),
            accounting_balanced=bool(row["accounting_balanced"]),
        )

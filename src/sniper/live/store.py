"""SQLite persistence for paper trading. Its own file (default data/paper.sqlite), never the research database."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable
from dataclasses import fields
from pathlib import Path
from typing import Any

from sniper.live.signals import LeaderOrder
from sniper.metrics import Lot, lot_from_row

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_ms  INTEGER NOT NULL,
    ended_ms    INTEGER,
    config_json TEXT,
    stats_json  TEXT
);
-- Leader orders (fills aggregated per tx, wallet, token, side) from tracked wallets.
CREATE TABLE IF NOT EXISTS orders (
    id              INTEGER PRIMARY KEY,
    tx_hash         TEXT NOT NULL,
    wallet          TEXT NOT NULL,
    token_id        TEXT NOT NULL,
    side            TEXT NOT NULL,
    condition_id    TEXT,
    outcome         TEXT,
    outcome_index   INTEGER,
    title           TEXT,
    slug            TEXT,
    event_slug      TEXT,
    name            TEXT,
    size            REAL,
    notional        REAL,
    vwap            REAL,
    worst_price     REAL,
    fee             REAL,
    n_fills         INTEGER,
    leader_ts       INTEGER,
    feed_ts_ms      INTEGER,
    received_ms     INTEGER,
    detected_ms     INTEGER,
    tier            TEXT,
    role            TEXT,
    holdings_before REAL,
    exit_fraction   REAL,
    late_fills      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (tx_hash, wallet, token_id, side)
);
CREATE INDEX IF NOT EXISTS orders_wallet ON orders (wallet, token_id);
-- Order-book snapshots: summary columns plus the top levels as JSON.
CREATE TABLE IF NOT EXISTS snapshots (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER,
    variant          TEXT,
    purpose          TEXT NOT NULL,
    token_id         TEXT NOT NULL,
    scheduled_ms     INTEGER,
    req_start_ms     INTEGER,
    req_end_ms       INTEGER,
    book_ts_ms       INTEGER,
    leader_ts        INTEGER,
    best_bid         REAL,
    best_ask         REAL,
    mid              REAL,
    spread           REAL,
    bid_depth_usdc   REAL,
    ask_depth_usdc   REAL,
    vwap             REAL,
    vwap_shares      REAL,
    vwap_usdc        REAL,
    vwap_complete    INTEGER,
    min_order_size   REAL,
    tick_size        REAL,
    last_trade_price REAL,
    levels_json      TEXT,
    error            TEXT
);
CREATE INDEX IF NOT EXISTS snapshots_order ON snapshots (order_id);
-- One row per (leader BUY order, variant): a metrics.Lot (lot_json) plus the open state needed to resume.
CREATE TABLE IF NOT EXISTS lots (
    id           INTEGER PRIMARY KEY,
    order_id     INTEGER,
    snapshot_id  INTEGER,
    variant      TEXT NOT NULL,
    strategy     TEXT NOT NULL,
    latency_s    INTEGER NOT NULL,
    wallet       TEXT NOT NULL,
    tier         TEXT,
    category     TEXT,
    event_key    TEXT,
    condition_id TEXT,
    token_id     TEXT NOT NULL,
    status       TEXT NOT NULL,
    signal_ts    INTEGER,
    exec_ts      INTEGER,
    leader_price REAL,
    exec_price   REAL,
    shares       REAL,
    cost         REAL,
    entry_fee    REAL,
    realized_pnl REAL,
    open_shares  REAL NOT NULL DEFAULT 0,
    open_basis   REAL NOT NULL DEFAULT 0,
    bench_shares REAL NOT NULL DEFAULT 0,
    close_ts     INTEGER,
    lot_json     TEXT NOT NULL,
    diag_json    TEXT,
    created_ms   INTEGER,
    updated_ms   INTEGER
);
CREATE INDEX IF NOT EXISTS lots_strategy ON lots (strategy, variant);
CREATE INDEX IF NOT EXISTS lots_token ON lots (token_id);
CREATE TABLE IF NOT EXISTS exits (
    id          INTEGER PRIMARY KEY,
    lot_id      INTEGER NOT NULL,
    order_id    INTEGER,
    snapshot_id INTEGER,
    variant     TEXT NOT NULL,
    kind        TEXT NOT NULL,
    ts          INTEGER NOT NULL,
    fraction    REAL,
    shares      REAL,
    price       REAL,
    proceeds    REAL,
    fee         REAL,
    basis       REAL,
    pnl         REAL,
    status      TEXT
);
CREATE TABLE IF NOT EXISTS settlements (
    token_id     TEXT PRIMARY KEY,
    condition_id TEXT,
    final_price  REAL NOT NULL,
    ts           INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS equity (
    id         INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    variant    TEXT NOT NULL,
    bankroll   REAL,
    realized   REAL,
    unrealized REAL,
    equity     REAL,
    deployed   REAL,
    open_lots  INTEGER,
    unmarked   INTEGER,
    killed     INTEGER
);
CREATE TABLE IF NOT EXISTS alerts (
    id       INTEGER PRIMARY KEY,
    ts_ms    INTEGER NOT NULL,
    kind     TEXT NOT NULL,
    order_id INTEGER,
    sink     TEXT,
    text     TEXT
);
"""

LOT_FIELDS = tuple(f.name for f in fields(Lot))


def _ms() -> int:
    return int(time.time() * 1000)


def lot_to_json(lot: Lot) -> str:
    return json.dumps({name: getattr(lot, name) for name in LOT_FIELDS})


def lot_from_json(text: str) -> Lot:
    lot = lot_from_row(json.loads(text))
    lot.flows = [(int(ts), float(dp), float(dc)) for ts, dp, dc in lot.flows]
    return lot


class PaperStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        research = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('activity', 'universe')"
        ).fetchone()
        if research is not None:
            self.conn.close()
            raise ValueError(f"{self.path} is a research database (has table {research[0]!r}); use a separate file")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def _insert(self, table: str, row: dict[str, Any]) -> int:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.conn:
            cur = self.conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
        return int(cur.lastrowid or 0)

    # --- meta and runs ------------------------------------------------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def start_run(self, config: dict[str, Any]) -> int:
        return self._insert("runs", {"started_ms": _ms(), "config_json": json.dumps(config, default=str)})

    def finish_run(self, run_id: int, stats: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE runs SET ended_ms = ?, stats_json = ? WHERE id = ?",
                (_ms(), json.dumps(stats, default=str), run_id),
            )

    # --- orders -------------------------------------------------------------------------------------------------------

    def insert_order(
        self,
        order: LeaderOrder,
        *,
        tier: str,
        role: str,
        holdings_before: float,
        exit_fraction: float | None,
    ) -> int | None:
        """None if the order key is already stored (fills that arrived after the order was emitted)."""
        row = {
            "tx_hash": order.tx_hash,
            "wallet": order.wallet,
            "token_id": order.token_id,
            "side": order.side,
            "condition_id": order.condition_id,
            "outcome": order.outcome,
            "outcome_index": order.outcome_index,
            "title": order.title,
            "slug": order.slug,
            "event_slug": order.event_slug,
            "name": order.name,
            "size": order.size,
            "notional": order.notional,
            "vwap": order.vwap,
            "worst_price": order.worst_price,
            "fee": order.fee,
            "n_fills": order.n_fills,
            "leader_ts": order.first_ts,
            "feed_ts_ms": order.feed_ts_ms,
            "received_ms": int(order.first_received * 1000),
            "detected_ms": int(order.detected_at * 1000),
            "tier": tier,
            "role": role,
            "holdings_before": holdings_before,
            "exit_fraction": exit_fraction,
        }
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        with self.conn:
            cur = self.conn.execute(f"INSERT OR IGNORE INTO orders ({cols}) VALUES ({marks})", tuple(row.values()))
        return int(cur.lastrowid) if cur.rowcount else None

    def add_late_fills(self, order: LeaderOrder) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE orders SET size = size + ?, notional = notional + ?, n_fills = n_fills + ?, "
                "late_fills = late_fills + ? WHERE tx_hash = ? AND wallet = ? AND token_id = ? AND side = ?",
                (order.size, order.notional, order.n_fills, order.n_fills, *order.key),
            )

    # --- snapshots, lots, exits ---------------------------------------------------------------------------------------

    def insert_snapshot(self, row: dict[str, Any]) -> int:
        return self._insert("snapshots", row)

    def insert_lot(
        self,
        lot: Lot,
        *,
        variant: str,
        order_id: int | None,
        snapshot_id: int | None,
        open_shares: float,
        open_basis: float,
        bench_shares: float,
        diag: dict[str, Any] | None = None,
    ) -> int:
        now = _ms()
        row = {
            "order_id": order_id,
            "snapshot_id": snapshot_id,
            "variant": variant,
            "strategy": lot.strategy,
            "latency_s": lot.latency_s,
            "wallet": lot.wallet,
            "tier": lot.tier,
            "category": lot.category,
            "event_key": lot.event_key,
            "condition_id": lot.condition_id,
            "token_id": lot.token_id,
            "status": lot.status,
            "signal_ts": lot.signal_ts,
            "exec_ts": lot.exec_ts,
            "leader_price": lot.leader_price,
            "exec_price": lot.exec_price,
            "shares": lot.shares,
            "cost": lot.cost,
            "entry_fee": lot.entry_fee,
            "realized_pnl": sum(dp for _, dp, _ in lot.flows),
            "open_shares": open_shares,
            "open_basis": open_basis,
            "bench_shares": bench_shares,
            "close_ts": lot.close_ts,
            "lot_json": lot_to_json(lot),
            "diag_json": json.dumps(diag or {}, default=str),
            "created_ms": now,
            "updated_ms": now,
        }
        return self._insert("lots", row)

    def update_lot(self, lot_id: int, lot: Lot, *, open_shares: float, open_basis: float, bench_shares: float) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE lots SET status = ?, realized_pnl = ?, open_shares = ?, open_basis = ?, bench_shares = ?, "
                "close_ts = ?, lot_json = ?, updated_ms = ? WHERE id = ?",
                (
                    lot.status,
                    sum(dp for _, dp, _ in lot.flows),
                    open_shares,
                    open_basis,
                    bench_shares,
                    lot.close_ts,
                    lot_to_json(lot),
                    _ms(),
                    lot_id,
                ),
            )

    def insert_exit(self, row: dict[str, Any]) -> int:
        return self._insert("exits", row)

    def upsert_settlement(self, token_id: str, condition_id: str, final_price: float, ts: int) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO settlements (token_id, condition_id, final_price, ts) VALUES (?, ?, ?, ?)",
                (token_id, condition_id, final_price, ts),
            )

    def settlements(self) -> dict[str, float]:
        return {r["token_id"]: r["final_price"] for r in self.conn.execute("SELECT * FROM settlements")}

    def insert_equity(self, row: dict[str, Any]) -> int:
        return self._insert("equity", row)

    def insert_alert(self, kind: str, text: str, *, sink: str, order_id: int | None = None) -> int:
        return self._insert("alerts", {"ts_ms": _ms(), "kind": kind, "order_id": order_id, "sink": sink, "text": text})

    # --- reads --------------------------------------------------------------------------------------------------------

    def lot_rows(self, strategy: str | None = None) -> list[sqlite3.Row]:
        if strategy is None:
            return list(self.conn.execute("SELECT * FROM lots ORDER BY id"))
        return list(self.conn.execute("SELECT * FROM lots WHERE strategy = ? ORDER BY id", (strategy,)))

    def rows(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return list(self.conn.execute(sql, tuple(params)))

"""SQLite storage. One file, WAL mode; all writes are idempotent upserts so backfills can be re-run."""

from __future__ import annotations

import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    address       TEXT PRIMARY KEY,
    user_name     TEXT,
    first_seen_at INTEGER NOT NULL
);

-- Candidate universe snapshots. `source` records how a wallet entered (leaderboard:month:politics, market:<id>...).
CREATE TABLE IF NOT EXISTS universe (
    snapshot TEXT NOT NULL,
    address  TEXT NOT NULL,
    source   TEXT NOT NULL,
    rank     INTEGER,
    pnl      REAL,
    volume   REAL,
    taken_at INTEGER NOT NULL,
    PRIMARY KEY (snapshot, address, source)
);

-- Raw /v2/activity rows. Text key columns use '' instead of NULL so the primary key deduplicates.
-- `seq` disambiguates identical fills inside one transaction.
CREATE TABLE IF NOT EXISTS activity (
    wallet        TEXT NOT NULL,
    ts            INTEGER NOT NULL,
    type          TEXT NOT NULL,
    side          TEXT NOT NULL DEFAULT '',
    token_id      TEXT NOT NULL DEFAULT '',
    condition_id  TEXT NOT NULL DEFAULT '',
    outcome_index INTEGER,
    price         REAL NOT NULL DEFAULT 0,
    size          REAL NOT NULL DEFAULT 0,
    usdc_size     REAL NOT NULL DEFAULT 0,
    tx_hash       TEXT NOT NULL,
    slug          TEXT,
    event_slug    TEXT,
    title         TEXT,
    seq           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (wallet, tx_hash, type, token_id, side, price, size, seq)
);
CREATE INDEX IF NOT EXISTS activity_wallet_ts ON activity (wallet, ts);
CREATE INDEX IF NOT EXISTS activity_token ON activity (token_id);

CREATE TABLE IF NOT EXISTS sync_state (
    wallet    TEXT PRIMARY KEY,
    last_ts   INTEGER,
    synced_at INTEGER NOT NULL,
    n_rows    INTEGER NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0
);

-- Activity intervals known to be complete per wallet, and windows where the per-window row cap was hit.
CREATE TABLE IF NOT EXISTS coverage (
    wallet   TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER NOT NULL,
    PRIMARY KEY (wallet, start_ts)
);
CREATE TABLE IF NOT EXISTS capped (
    wallet   TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER NOT NULL,
    low_ts   INTEGER NOT NULL,
    PRIMARY KEY (wallet, start_ts, end_ts)
);

CREATE TABLE IF NOT EXISTS tokens (
    token_id           TEXT PRIMARY KEY,
    condition_id       TEXT NOT NULL,
    outcome_index      INTEGER,
    outcome            TEXT,
    opposite_token_id  TEXT,
    resolved           INTEGER NOT NULL DEFAULT 0,
    final_price        REAL,
    closed             INTEGER NOT NULL DEFAULT 0,
    neg_risk           INTEGER NOT NULL DEFAULT 0,
    neg_risk_market_id TEXT,
    event_id           TEXT,
    event_slug         TEXT,
    market_slug        TEXT,
    title              TEXT,
    updated_at         INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_condition ON tokens (condition_id);

CREATE TABLE IF NOT EXISTS markets (
    condition_id TEXT PRIMARY KEY,
    slug         TEXT,
    question     TEXT,
    category     TEXT,
    fee_type     TEXT,
    fee_rate     REAL,
    end_date     TEXT,
    closed_time  INTEGER,
    closed       INTEGER NOT NULL DEFAULT 0,
    neg_risk     INTEGER NOT NULL DEFAULT 0,
    updated_at   INTEGER NOT NULL
);

-- Minute-level CLOB price history and the windows already fetched (so empty windows are not refetched).
CREATE TABLE IF NOT EXISTS prices (
    token_id TEXT NOT NULL,
    ts       INTEGER NOT NULL,
    price    REAL NOT NULL,
    PRIMARY KEY (token_id, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS price_windows (
    token_id TEXT NOT NULL,
    start_ts INTEGER NOT NULL,
    end_ts   INTEGER NOT NULL,
    PRIMARY KEY (token_id, start_ts, end_ts)
) WITHOUT ROWID;
"""

ACTIVITY_COLUMNS = (
    "wallet", "ts", "type", "side", "token_id", "condition_id", "outcome_index",
    "price", "size", "usdc_size", "tx_hash", "slug", "event_slug", "title", "seq",
)  # fmt: skip


def _now() -> int:
    return int(time.time())


def merge_intervals(intervals: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    out: list[list[int]] = []
    for a, b in sorted(intervals):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def subtract_intervals(window: tuple[int, int], covered: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Parts of `window` not covered by `covered`."""
    start, end = window
    gaps = []
    cursor = start
    for a, b in merge_intervals(covered):
        if b <= cursor or a >= end:
            continue
        if a > cursor:
            gaps.append((cursor, a))
        cursor = max(cursor, b)
        if cursor >= end:
            break
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def activity_record(wallet: str, row: dict[str, Any], *, slim: bool = False) -> dict[str, Any]:
    """Normalize a /v2/activity (or /v2/trades) row into the activity table shape.

    slim=True drops display-only text (title, event slug), which is most of the row size; token metadata covers it.
    """
    size = float(row.get("size") or 0)
    price = float(row.get("price") or 0)
    usdc = row.get("usdc_size")
    return {
        "wallet": wallet.lower(),
        "ts": int(row["timestamp"]),
        "type": row.get("type") or "TRADE",
        "side": row.get("side") or "",
        "token_id": row.get("token_id") or "",
        "condition_id": row.get("condition_id") or "",
        "outcome_index": row.get("outcome_index"),
        "price": price,
        "size": size,
        "usdc_size": float(usdc) if usdc is not None else price * size,
        "tx_hash": row.get("transaction_hash") or "",
        "slug": row.get("slug"),
        "event_slug": None if slim else row.get("event_slug"),
        "title": None if slim else row.get("title"),
    }


def assign_seq(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Number identical records so that repeated identical fills in one transaction survive deduplication."""
    seen: Counter[tuple] = Counter()
    for rec in records:
        key = (rec["wallet"], rec["tx_hash"], rec["type"], rec["token_id"], rec["side"], rec["price"], rec["size"])
        rec["seq"] = seen[key]
        seen[key] += 1
    return records


def category_from_fee_type(fee_type: str | None) -> str:
    # Gamma's `category` is usually null; fee types look like "sports_fees_v3", "crypto_fees_v2".
    if not fee_type:
        return "unknown"
    return fee_type.split("_", 1)[0].lower()


@dataclass
class SyncState:
    wallet: str
    last_ts: int | None
    synced_at: int
    n_rows: int
    truncated: bool


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self.conn:
            yield self.conn

    # --- wallets / universe -------------------------------------------------------------------------------------

    def add_universe(self, snapshot: str, rows: Iterable[dict[str, Any]]) -> int:
        now = _now()
        n = 0
        with self.tx() as c:
            for r in rows:
                address = r["address"].lower()
                c.execute(
                    "INSERT INTO wallets(address, user_name, first_seen_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(address) DO UPDATE SET user_name = COALESCE(excluded.user_name, wallets.user_name)",
                    (address, r.get("user_name"), now),
                )
                c.execute(
                    "INSERT OR REPLACE INTO universe(snapshot, address, source, rank, pnl, volume, taken_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (snapshot, address, r["source"], r.get("rank"), r.get("pnl"), r.get("volume"), now),
                )
                n += 1
        return n

    def universe(self, snapshot: str | None = None) -> list[str]:
        if snapshot is None:
            row = self.conn.execute("SELECT snapshot FROM universe ORDER BY taken_at DESC LIMIT 1").fetchone()
            if row is None:
                return []
            snapshot = row["snapshot"]
        rows = self.conn.execute(
            "SELECT DISTINCT address FROM universe WHERE snapshot = ? ORDER BY address", (snapshot,)
        )
        return [r["address"] for r in rows]

    def wallet_names(self) -> dict[str, str | None]:
        return {r["address"]: r["user_name"] for r in self.conn.execute("SELECT address, user_name FROM wallets")}

    # --- activity -------------------------------------------------------------------------------------------------

    def upsert_activity(self, records: list[dict[str, Any]]) -> int:
        if not records:
            return 0
        assign_seq(records)
        cols = ", ".join(ACTIVITY_COLUMNS)
        marks = ", ".join("?" for _ in ACTIVITY_COLUMNS)
        with self.tx() as c:
            before = c.total_changes
            c.executemany(
                f"INSERT OR IGNORE INTO activity({cols}) VALUES ({marks})",
                [tuple(r[k] for k in ACTIVITY_COLUMNS) for r in records],
            )
            return c.total_changes - before

    def activity(self, wallet: str, *, until: int | None = None, since: int | None = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM activity WHERE wallet = ?"
        args: list[Any] = [wallet.lower()]
        if since is not None:
            q += " AND ts >= ?"
            args.append(since)
        if until is not None:
            q += " AND ts < ?"
            args.append(until)
        return self.conn.execute(q + " ORDER BY ts, tx_hash, seq", args).fetchall()

    def wallets_with_activity(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT wallet FROM activity ORDER BY wallet")]

    def activity_count(self, wallet: str, start: int | None = None, end: int | None = None) -> int:
        q, args = "SELECT COUNT(*) FROM activity WHERE wallet = ?", [wallet.lower()]
        if start is not None:
            q += " AND ts >= ?"
            args.append(start)
        if end is not None:
            q += " AND ts < ?"
            args.append(end)
        return int(self.conn.execute(q, args).fetchone()[0])

    # --- coverage ---------------------------------------------------------------------------------------------------

    def coverage(self, wallet: str) -> list[tuple[int, int]]:
        rows = self.conn.execute(
            "SELECT start_ts, end_ts FROM coverage WHERE wallet = ? ORDER BY start_ts", (wallet.lower(),)
        )
        return [(r[0], r[1]) for r in rows]

    def add_coverage(self, wallet: str, start: int, end: int) -> None:
        w = wallet.lower()
        merged = merge_intervals([*self.coverage(w), (start, end)])
        with self.tx() as c:
            c.execute("DELETE FROM coverage WHERE wallet = ?", (w,))
            c.executemany(
                "INSERT INTO coverage(wallet, start_ts, end_ts) VALUES (?, ?, ?)", [(w, a, b) for a, b in merged]
            )

    def missing_intervals(self, wallet: str, start: int, end: int) -> list[tuple[int, int]]:
        return subtract_intervals((start, end), self.coverage(wallet))

    def add_capped(self, wallet: str, start: int, end: int, low_ts: int) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO capped(wallet, start_ts, end_ts, low_ts) VALUES (?, ?, ?, ?)",
                (wallet.lower(), start, end, low_ts),
            )

    def window_status(self, wallet: str, start: int, end: int) -> str:
        """'complete' if activity in [start, end) is fully stored, 'capped' if the row cap was hit, else 'missing'."""
        if not self.missing_intervals(wallet, start, end):
            return "complete"
        hit = self.conn.execute(
            "SELECT 1 FROM capped WHERE wallet = ? AND start_ts < ? AND end_ts > ? LIMIT 1",
            (wallet.lower(), end, start),
        ).fetchone()
        return "capped" if hit else "missing"

    def migrate_sync_state(self, since: int) -> int:
        """One-off: turn phase-1 sync_state rows (backfilled from `since`) into coverage intervals.

        Truncated wallets are complete only from their earliest stored row (the API returns newest first).
        """
        rows = self.conn.execute(
            "SELECT s.wallet, s.synced_at, s.truncated FROM sync_state s "
            "WHERE NOT EXISTS (SELECT 1 FROM coverage c WHERE c.wallet = s.wallet)"
        ).fetchall()
        n = 0
        for r in rows:
            start = since
            if r["truncated"]:
                low = self.conn.execute("SELECT MIN(ts) FROM activity WHERE wallet = ?", (r["wallet"],)).fetchone()[0]
                if low is None:
                    continue
                start = low
            self.add_coverage(r["wallet"], start, r["synced_at"])
            n += 1
        return n

    def sync_state(self, wallet: str) -> SyncState | None:
        r = self.conn.execute("SELECT * FROM sync_state WHERE wallet = ?", (wallet.lower(),)).fetchone()
        if r is None:
            return None
        return SyncState(r["wallet"], r["last_ts"], r["synced_at"], r["n_rows"], bool(r["truncated"]))

    def set_sync_state(self, wallet: str, last_ts: int | None, n_rows: int, truncated: bool) -> None:
        with self.tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO sync_state(wallet, last_ts, synced_at, n_rows, truncated) "
                "VALUES (?, ?, ?, ?, ?)",
                (wallet.lower(), last_ts, _now(), n_rows, int(truncated)),
            )

    # --- tokens / markets ------------------------------------------------------------------------------------------

    def token_ids_needing_refresh(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT a.token_id FROM activity a LEFT JOIN tokens t ON t.token_id = a.token_id "
            "WHERE a.token_id != '' AND (t.token_id IS NULL OR t.resolved = 0)"
        )
        return [r[0] for r in rows]

    def upsert_tokens(self, rows: Iterable[dict[str, Any]]) -> int:
        now = _now()
        n = 0
        with self.tx() as c:
            for t in rows:
                c.execute(
                    "INSERT OR REPLACE INTO tokens(token_id, condition_id, outcome_index, outcome, opposite_token_id, "
                    "resolved, final_price, closed, neg_risk, neg_risk_market_id, event_id, event_slug, market_slug, "
                    "title, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(t["token_id"]),
                        t.get("condition_id") or "",
                        t.get("outcome_index"),
                        t.get("outcome"),
                        t.get("opposite_token_id"),
                        int(bool(t.get("resolved"))),
                        t.get("final_price"),
                        int(bool(t.get("closed"))),
                        int(bool(t.get("neg_risk"))),
                        t.get("neg_risk_market_id"),
                        str(t["event_id"]) if t.get("event_id") is not None else None,
                        t.get("event_slug"),
                        t.get("market_slug"),
                        t.get("title"),
                        now,
                    ),
                )
                n += 1
        return n

    def tokens(self, condition_ids: Iterable[str] | None = None) -> dict[str, sqlite3.Row]:
        if condition_ids is None:
            return {r["token_id"]: r for r in self.conn.execute("SELECT * FROM tokens")}
        self._temp_keys("tmp_cids", condition_ids)
        rows = self.conn.execute("SELECT t.* FROM tokens t JOIN tmp_cids k ON k.key = t.condition_id")
        return {r["token_id"]: r for r in rows}

    def _temp_keys(self, name: str, keys: Iterable[str]) -> None:
        self.conn.execute(f"CREATE TEMP TABLE IF NOT EXISTS {name} (key TEXT PRIMARY KEY)")
        self.conn.execute(f"DELETE FROM {name}")
        self.conn.executemany(f"INSERT OR IGNORE INTO {name}(key) VALUES (?)", [(k,) for k in keys])

    def condition_ids_for(self, wallets: Iterable[str], start: int, end: int) -> set[str]:
        """Conditions these wallets touched in [start, end): via traded/redeemed tokens and merge/split rows."""
        self._temp_keys("tmp_wallets", [w.lower() for w in wallets])
        rows = self.conn.execute(
            "SELECT DISTINCT t.condition_id FROM tmp_wallets w JOIN activity a ON a.wallet = w.key "
            "JOIN tokens t ON t.token_id = a.token_id WHERE a.ts >= ? AND a.ts < ? AND a.token_id != ''",
            (start, end),
        )
        out = {r[0] for r in rows}
        rows = self.conn.execute(
            "SELECT DISTINCT a.condition_id FROM tmp_wallets w JOIN activity a ON a.wallet = w.key "
            "WHERE a.ts >= ? AND a.ts < ? AND a.type IN ('MERGE', 'SPLIT') AND a.condition_id != ''",
            (start, end),
        )
        return out | {r[0] for r in rows}

    def condition_ids_needing_market(self) -> dict[str, bool]:
        """Conditions without market metadata (or stored as open but now closed) -> token-level closed flag."""
        rows = self.conn.execute(
            "SELECT t.condition_id, MAX(t.closed) FROM tokens t LEFT JOIN markets m ON m.condition_id = t.condition_id "
            "WHERE t.condition_id LIKE '0x%' AND (m.condition_id IS NULL OR (m.closed = 0 AND t.closed = 1)) "
            "GROUP BY t.condition_id"
        )
        return {r[0]: bool(r[1]) for r in rows}

    def upsert_markets(self, rows: Iterable[dict[str, Any]]) -> int:
        now = _now()
        n = 0
        with self.tx() as c:
            for m in rows:
                fee = m.get("feeSchedule") or {}
                c.execute(
                    "INSERT OR REPLACE INTO markets(condition_id, slug, question, category, fee_type, fee_rate, "
                    "end_date, closed_time, closed, neg_risk, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        m["conditionId"],
                        m.get("slug"),
                        m.get("question"),
                        category_from_fee_type(m.get("feeType")),
                        m.get("feeType"),
                        float(fee.get("rate") or 0.0),
                        m.get("endDate"),
                        parse_gamma_time(m.get("closedTime")),
                        int(bool(m.get("closed"))),
                        int(bool(m.get("negRisk"))),
                        now,
                    ),
                )
                n += 1
        return n

    def markets(self, condition_ids: Iterable[str] | None = None) -> dict[str, sqlite3.Row]:
        if condition_ids is None:
            return {r["condition_id"]: r for r in self.conn.execute("SELECT * FROM markets")}
        self._temp_keys("tmp_mcids", condition_ids)
        rows = self.conn.execute("SELECT m.* FROM markets m JOIN tmp_mcids k ON k.key = m.condition_id")
        return {r["condition_id"]: r for r in rows}

    # --- prices ----------------------------------------------------------------------------------------------------

    def has_price_window(self, token_id: str, start: int, end: int) -> bool:
        r = self.conn.execute(
            "SELECT 1 FROM price_windows WHERE token_id = ? AND start_ts <= ? AND end_ts >= ? LIMIT 1",
            (token_id, start, end),
        ).fetchone()
        return r is not None

    def add_prices(self, token_id: str, start: int, end: int, points: Iterable[tuple[int, float]]) -> None:
        with self.tx() as c:
            c.executemany(
                "INSERT OR REPLACE INTO prices(token_id, ts, price) VALUES (?, ?, ?)",
                [(token_id, ts, p) for ts, p in points],
            )
            c.execute(
                "INSERT OR IGNORE INTO price_windows(token_id, start_ts, end_ts) VALUES (?, ?, ?)",
                (token_id, start, end),
            )

    def prices(self, token_id: str, start: int, end: int) -> list[tuple[int, float]]:
        rows = self.conn.execute(
            "SELECT ts, price FROM prices WHERE token_id = ? AND ts >= ? AND ts <= ? ORDER BY ts",
            (token_id, start, end),
        )
        return [(r[0], r[1]) for r in rows]


def parse_gamma_time(value: str | None) -> int | None:
    """Gamma timestamps come as '2026-11-06 14:13:04.014295+00' or ISO-8601."""
    if not value:
        return None
    from datetime import datetime

    text = value.strip().replace(" ", "T", 1)
    if text.endswith("+00"):
        text += ":00"
    text = text.replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None

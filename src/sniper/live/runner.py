"""`sniper live run`: RTDS trades -> leader orders -> order-book snapshots -> paper portfolios -> alerts."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sniper.config import Settings
from sniper.http import ApiClient
from sniper.live import messages, report
from sniper.live.book import EPS, BookSource, ClobBooks
from sniper.live.markets import MarketDirectory
from sniper.live.paper import EntryOutcome, ExitOutcome, PaperConfig, PaperEngine, PaperListener, Settled, Variant
from sniper.live.rtds import RtdsClient
from sniper.live.signals import LeaderOrder, OrderAggregator
from sniper.live.store import PaperStore
from sniper.live.telegram import DryRunNotifier, Notifier, TelegramNotifier
from sniper.live.watchlist import WatchEntry, load_watchlist
from sniper.polymarket import CLOB_GENERAL, CLOB_ROUTES, DataApi, GammaApi

log = logging.getLogger(__name__)


@dataclass
class RunConfig:
    scores: Path = Path("reports/scores-now.csv")
    alert_tiers: tuple[str, ...] = ("candidate",)
    track_tiers: tuple[str, ...] = ("candidate", "watch")
    control_sample: int = 0
    seed: int = 0
    paper: PaperConfig = field(default_factory=PaperConfig)
    paper_db: Path = Path("data/paper.sqlite")
    alerts_log: Path = Path("data/alerts.log")
    duration_s: float = 0.0  # 0 = until interrupted
    window_s: float = 1.5
    debug_min_usdc: float | None = None  # smoke tests: any wallet's BUY >= this is a signal
    debug_alert: bool = False
    force_dry_run: bool = False
    alert_exits: bool = True
    positions_every_s: float = 300.0
    resolutions_every_s: float = 600.0
    mtm_every_s: float = 900.0
    stats_every_s: float = 60.0
    drain_s: float | None = None  # None = max delay + 15 s


def clob_book_client(settings: Settings) -> ApiClient:
    """Dedicated CLOB client for /book: a short timeout and one retry keep snapshot timing honest."""
    share = settings.rate_limit_share
    return ApiClient(
        settings.clob_url,
        general_limit=CLOB_GENERAL * share,
        route_limits={k: v * share for k, v in CLOB_ROUTES.items()},
        timeout_s=5.0,
        max_retries=1,
        backoff_base_s=0.25,
        user_agent=settings.user_agent,
    )


async def fetch_positions(data_api: DataApi, wallet: str, *, max_items: int = 5000) -> tuple[dict[str, float], bool]:
    """Open positions of a wallet as (token -> shares, complete); an incomplete list must not be read as exits."""
    out: dict[str, float] = {}
    n = 0
    params = {"user": wallet, "status": "OPEN", "limit": 500}
    async for row in data_api.client.paginate("/positions", params, max_items=max_items):
        n += 1
        token = str(row.get("token_id") or row.get("asset") or "")
        size = float(row.get("current_size") or row.get("size") or 0.0)
        if token and size > 0:
            out[token] = out.get(token, 0.0) + size
    return out, n < max_items


class LiveRunner(PaperListener):
    def __init__(
        self,
        cfg: RunConfig,
        settings: Settings | None = None,
        *,
        notifier: Notifier | None = None,
        rtds: RtdsClient | None = None,
        books: BookSource | None = None,
        data_api: DataApi | None = None,
        gamma: GammaApi | None = None,
        store: PaperStore | None = None,
    ):
        self.cfg = cfg
        settings = settings or Settings()
        self.watchlist = load_watchlist(
            cfg.scores,
            track_tiers=cfg.track_tiers,
            alert_tiers=cfg.alert_tiers,
            control_sample=cfg.control_sample,
            seed=cfg.seed,
        )
        self._owned: list[Callable[[], Awaitable[None]]] = []
        self.store = store or PaperStore(cfg.paper_db)
        self._owns_store = store is None
        self.data_api = data_api or DataApi.create(settings)
        self.gamma = gamma or GammaApi.create(settings)
        if data_api is None:
            self._owned.append(self.data_api.aclose)
        if gamma is None:
            self._owned.append(self.gamma.aclose)
        if books is None:
            client = clob_book_client(settings)
            self._owned.append(client.aclose)
            books = ClobBooks(client)
        self.books = books
        self.markets = MarketDirectory(self.data_api, self.gamma)
        self.engine = PaperEngine(self.store, books, self.markets, cfg.paper, listener=self)
        self.notifier: Notifier = notifier or self._make_notifier()
        self.rtds = rtds or RtdsClient()
        wallet_filter = None if cfg.debug_min_usdc is not None else self.watchlist.wallets.__contains__
        self.aggregator = OrderAggregator(window_s=cfg.window_s, wallet_filter=wallet_filter)
        self.debug_wallets: set[str] = set()
        self.counters: Counter[str] = Counter()
        self.started_at = time.time()
        self._stop = asyncio.Event()
        self._interrupted = False

    def _make_notifier(self) -> Notifier:
        if not self.cfg.force_dry_run:
            telegram = TelegramNotifier.from_env()
            if telegram is not None:
                return telegram
        return DryRunNotifier(self.cfg.alerts_log)

    # --- signal routing -----------------------------------------------------------------------------------------------

    def entry_of(self, wallet: str, name: str = "") -> WatchEntry | None:
        entry = self.watchlist.get(wallet)
        if entry is None and wallet in self.debug_wallets:
            entry = WatchEntry(wallet, tier="debug", role="debug", user_name=name)
        return entry

    def route(self, order: LeaderOrder) -> WatchEntry | None:
        """Watchlist wallets always; in debug mode also large BUYs of any wallet and exits from those wallets."""
        entry = self.watchlist.get(order.wallet)
        if entry is not None or self.cfg.debug_min_usdc is None:
            return entry
        if order.side == "BUY" and order.notional >= self.cfg.debug_min_usdc:
            self.debug_wallets.add(order.wallet)
            return WatchEntry(order.wallet, tier="debug", role="debug", user_name=order.name)
        if order.side == "SELL" and self.engine.has_any_position(order.wallet, order.token_id):
            return self.entry_of(order.wallet, order.name)
        return None

    def dispatch(self, orders: list[LeaderOrder]) -> int:
        n = 0
        for order in orders:
            entry = self.route(order)
            if entry is None:
                continue
            self.engine.on_order(order, entry)
            n += 1
        self.counters["orders_dispatched"] += n
        return n

    # --- alerts (PaperListener) ---------------------------------------------------------------------------------------

    def _alerts_for(self, entry: WatchEntry | None) -> bool:
        if entry is None:
            return False
        return entry.alerts or (entry.role == "debug" and self.cfg.debug_alert)

    async def _alert(self, kind: str, text: str, order_id: int | None = None) -> None:
        self.store.insert_alert(kind, text, sink=self.notifier.name, order_id=order_id)
        self.counters[f"alerts_{kind}"] += 1
        try:
            await self.notifier.send(text)
        except Exception:
            log.exception("alert delivery failed")

    async def entry(self, outcome: EntryOutcome) -> None:
        if outcome.variant != self.engine.fastest or not self._alerts_for(outcome.entry):
            return
        if self.engine.paused:  # /pause mutes signal alerts as well as new entries
            return
        await self._alert("signal", messages.render_signal(outcome), outcome.order_id)

    async def exit(self, outcome: ExitOutcome, order: LeaderOrder | None) -> None:
        if not self.cfg.alert_exits or outcome.variant != self.engine.fastest or outcome.status == "no_position":
            return
        entry = self.entry_of(outcome.wallet)
        if not self._alerts_for(entry):
            return
        title = self.engine.titles.get(outcome.token_id, "")
        await self._alert("exit", messages.render_exit(outcome, order, entry, title))

    async def settled(self, token_id: str, final_price: float, closed: list[Settled]) -> None:
        if not any(self._alerts_for(self.entry_of(s.open_lot.lot.wallet)) for s in closed):
            return
        title = self.engine.titles.get(token_id, token_id[:16])
        await self._alert("settle", messages.render_settlement(title, final_price, closed))

    async def kill_switch(self, variant: Variant, equity: float, peak: float) -> None:
        await self._alert("kill_switch", messages.render_kill_switch(variant, equity, peak))

    # --- commands -----------------------------------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = time.time()
        st = self.rtds.stats
        c = self.engine.counters
        variants = []
        for v in self.engine.variants:
            risk = self.engine.risk[v]
            _, n_open, _ = self.engine.unrealized(v)
            variants.append(
                {
                    "name": v.name,
                    "filled": c[f"{v.name}:filled"],
                    "skipped": sum(n for k, n in c.items() if k.startswith(f"{v.name}:skip:")),
                    "open_lots": n_open,
                    "deployed": risk.total_deployed,
                    "realized": risk.realized,
                    "equity": self.engine.equity(v),
                    "killed": risk.killed,
                }
            )
        counts = ", ".join(f"{k} {n}" for k, n in sorted(self.watchlist.counts().items()))
        return {
            "uptime_s": now - self.started_at,
            "notifier": self.notifier.name,
            "paused": self.engine.paused,
            "rtds": {
                "frames": st.frames,
                "trades": st.trades,
                "malformed": st.malformed,
                "reconnects": st.reconnects,
                "last_frame_age_s": now - st.last_frame_at if st.last_frame_at else 0.0,
            },
            "buy_orders": c["buy_orders"],
            "sell_orders": c["sell_orders"],
            "pending": self.engine.pending_tasks,
            "watchlist": counts + (f", debug {len(self.debug_wallets)}" if self.debug_wallets else ""),
            "variants": variants,
        }

    def portfolio(self) -> list[dict[str, Any]]:
        out = []
        for v in self.engine.variants:
            positions = [
                {
                    "trader": ol.lot.user_name or messages.short_wallet(ol.lot.wallet),
                    "title": self.engine.titles.get(ol.lot.token_id, ol.lot.token_id[:16]),
                    "shares": ol.shares,
                    "basis": ol.basis,
                    "mark": self.engine.marks.get(ol.lot.token_id),
                }
                for ol in self.engine.lots_of(v)
                if ol.shares > EPS
            ]
            positions.sort(key=lambda p: -p["basis"])
            unreal, _, _ = self.engine.unrealized(v)
            out.append(
                {
                    "name": v.name,
                    "deployed": self.engine.risk[v].total_deployed,
                    "unrealized": unreal,
                    "positions": positions,
                }
            )
        return out

    async def handle_command(self, cmd: str, args: list[str]) -> str | None:
        if cmd in ("start", "help"):
            return messages.HELP
        if cmd == "status":
            return messages.render_status(self.status())
        if cmd == "portfolio":
            return messages.render_portfolio(self.portfolio())
        if cmd == "watchlist":
            return messages.render_watchlist(list(self.watchlist.entries.values()))
        if cmd == "pause":
            self.engine.pause()
            return "⏸ Пауза: новые paper-входы и алерты о сигналах остановлены; выходы и расчёты продолжаются. /resume"
        if cmd == "resume":
            self.engine.resume()
            return "▶️ Продолжаем: paper-входы и алерты включены, kill-switch сброшен."
        if cmd == "report":
            rep = report.build_report(self.store, strategy=self.cfg.paper.strategy, n_boot=300)
            return messages.render_report({"by_latency": rep["by_strategy_latency"], "mtm": rep["mtm"]})
        return f"Неизвестная команда /{messages.esc(cmd)}. /help — список команд."

    # --- background jobs ----------------------------------------------------------------------------------------------

    async def init_holdings(self) -> int:
        sem = asyncio.Semaphore(4)
        loaded = 0

        async def one(wallet: str) -> None:
            nonlocal loaded
            async with sem:
                try:
                    positions, _ = await fetch_positions(self.data_api, wallet)
                except Exception as exc:
                    log.warning("positions for %s failed: %s", wallet[:10], exc)
                    return
            self.engine.set_holdings(wallet, positions)
            loaded += 1

        await asyncio.gather(*(one(w) for w in sorted(self.watchlist.wallets)))
        return loaded

    async def poll_positions(self) -> int:
        """Catch reductions the feed did not show as sells (merges, transfers)."""
        now = time.time()
        mirrored = 0
        for wallet in sorted(self.watchlist.wallets):
            try:
                positions, complete = await fetch_positions(self.data_api, wallet)
            except Exception as exc:
                log.warning("positions for %s failed: %s", wallet[:10], exc)
                continue
            if not complete:
                continue
            reductions = self.engine.reconcile(wallet, positions, now)
            if reductions:
                log.info("%s: %d position reductions without feed sells", wallet[:10], len(reductions))
                mirrored += await self.engine.apply_reductions(wallet, reductions, now)
        return mirrored

    async def log_stats(self) -> None:
        st = self.rtds.stats
        agg = self.aggregator
        c = self.engine.counters
        log.info(
            "rtds frames=%d trades=%d malformed=%d reconnects=%d | fills kept=%d orders=%d dispatched=%d | "
            "buy=%d sell=%d pending=%d",
            st.frames, st.trades, st.malformed, st.reconnects, agg.fills_kept, agg.orders_emitted,
            self.counters["orders_dispatched"], c["buy_orders"], c["sell_orders"], self.engine.pending_tasks,
        )  # fmt: skip

    async def _periodic(self, every_s: float, job: Callable[[], Awaitable[Any]], name: str) -> None:
        if every_s <= 0:
            return
        while True:
            await asyncio.sleep(every_s)
            try:
                await job()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("%s job failed", name)

    async def _consume(self) -> None:
        async for ev in self.rtds.events():
            self.aggregator.add(ev)

    async def _dispatch_loop(self) -> None:
        while True:
            await asyncio.sleep(0.2)
            self.dispatch(self.aggregator.pop_due(time.time()))

    def interrupt(self) -> None:
        self._interrupted = True
        self._stop.set()

    def stop(self) -> None:
        self._stop.set()

    # --- lifecycle ----------------------------------------------------------------------------------------------------

    def config_dict(self) -> dict[str, Any]:
        cfg = asdict(self.cfg)
        return {k: (str(v) if isinstance(v, Path) else v) for k, v in cfg.items()} | {
            "notifier": self.notifier.name,
            "watchlist": self.watchlist.counts(),
        }

    async def run(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(sig, self.interrupt)
        restored = self.engine.load()
        run_id = self.store.start_run(self.config_dict())
        log.info(
            "watchlist %s · restored %d open lots · variants %s · alerts via %s",
            self.watchlist.counts(), restored, [v.name for v in self.engine.variants], self.notifier.name,
        )  # fmt: skip
        if self.cfg.debug_min_usdc is not None:
            log.warning("DEBUG: any wallet's BUY >= $%.0f is treated as a tracked signal", self.cfg.debug_min_usdc)
        loaded = await self.init_holdings()
        log.info("leader holdings loaded for %d/%d wallets", loaded, len(self.watchlist))
        cfg = self.cfg
        tasks = [
            asyncio.create_task(self._consume()),
            asyncio.create_task(self._dispatch_loop()),
            asyncio.create_task(self._periodic(cfg.positions_every_s, self.poll_positions, "positions")),
            asyncio.create_task(self._periodic(cfg.resolutions_every_s, self.engine.check_resolutions, "resolutions")),
            asyncio.create_task(self._periodic(cfg.mtm_every_s, self.engine.mark_to_market, "mtm")),
            asyncio.create_task(self._periodic(cfg.stats_every_s, self.log_stats, "stats")),
            asyncio.create_task(self.notifier.run_commands(self.handle_command)),
        ]
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), cfg.duration_s if cfg.duration_s > 0 else None)
        log.info("stopping (%s)", "interrupted" if self._interrupted else "duration reached")
        self.rtds.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.dispatch(self.aggregator.pop_all(time.time()))
        left = 0
        if not self._interrupted:
            drain = cfg.drain_s if cfg.drain_s is not None else max(cfg.paper.delays, default=0) + 15
            left = await self.engine.drain(drain)
        cancelled = await self.engine.cancel_all()
        if not self._interrupted and self.engine.held_tokens():
            try:
                await asyncio.wait_for(self.engine.mark_to_market(), 30)
            except Exception as exc:
                log.warning("final mark-to-market failed: %s", exc)
        stats = self.final_stats(cancelled=cancelled, left=left)
        self.store.finish_run(run_id, stats)
        await self.notifier.flush(10)
        await self.aclose()
        return stats

    def final_stats(self, *, cancelled: int = 0, left: int = 0) -> dict[str, Any]:
        return {
            "duration_s": round(time.time() - self.started_at, 1),
            "interrupted": self._interrupted,
            "rtds": asdict(self.rtds.stats),
            "aggregator": {
                "fills_seen": self.aggregator.fills_seen,
                "fills_kept": self.aggregator.fills_kept,
                "orders_emitted": self.aggregator.orders_emitted,
                "dropped": self.aggregator.dropped,
            },
            "dispatched": self.counters["orders_dispatched"],
            "debug_wallets": len(self.debug_wallets),
            "engine": dict(sorted(self.engine.counters.items())),
            "alerts": {k: n for k, n in self.counters.items() if k.startswith("alerts_")},
            "notifier": self.notifier.name,
            "pending_at_stop": left,
            "cancelled_tasks": cancelled,
        }

    async def aclose(self) -> None:
        await self.notifier.aclose()
        for close in self._owned:
            with contextlib.suppress(Exception):
                await close()
        if self._owns_store:
            self.store.close()

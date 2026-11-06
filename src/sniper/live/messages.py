"""Telegram message texts (Russian, HTML parse mode). Every dynamic value is escaped."""

from __future__ import annotations

import html
import time
from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from sniper.live.paper import EntryOutcome, ExitOutcome, Settled, Variant
from sniper.live.signals import LeaderOrder
from sniper.live.watchlist import WatchEntry

REASONS = {
    "premium": "цена ушла: лучший ask выше лимита (VWAP лидера + премия)",
    "depth": "мало ликвидности до лимита цены",
    "extreme_price": "цена вне диапазона [0.03; 0.97]",
    "min_size": "меньше минимального размера ордера",
    "no_book": "стакан недоступен",
    "no_edge": "Kelly: нет преимущества после комиссии",
    "paused": "пауза (/resume — продолжить)",
    "risk_market_limit": "лимит риска на рынок",
    "risk_event_limit": "лимит риска на событие",
    "risk_leader_limit": "лимит риска на трейдера",
    "risk_category_limit": "лимит риска на категорию",
    "risk_total_limit": "лимит общей загрузки капитала",
    "risk_daily_stop": "дневной стоп по убытку",
    "risk_weekly_stop": "недельный стоп по убытку",
    "risk_kill_switch": "kill-switch: просадка от пика",
}
EXIT_STATUS = {
    "ok": "продано",
    "partial": "продано частично (не хватило покупателей)",
    "no_book": "стакан недоступен, позиция осталась",
    "no_bids": "нет покупателей, позиция осталась",
    "no_position": "позиции нет",
}
HELP = (
    "<b>Команды</b>\n"
    "/status — состояние потока, сигналы, капитал по вариантам задержки\n"
    "/portfolio — открытые paper-позиции\n"
    "/watchlist — отслеживаемые трейдеры\n"
    "/report — сводка paper-результатов (ROI с 95% ДИ, комиссии, просадка)\n"
    "/pause — остановить новые paper-входы и алерты о сигналах\n"
    "/resume — продолжить (также сбрасывает kill-switch)\n"
    "Реальной торговли нет: только сигналы и paper trading."
)


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def attr(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def num(x: float, digits: int = 1) -> str:
    return f"{x:,.{digits}f}".replace(",", " ")


def usd(x: float | None, *, signed: bool = False) -> str:
    if x is None:
        return "—"
    digits = 0 if abs(x) >= 10_000 else 2
    sign = ("+" if x >= 0 else "−") if signed else ("−" if x < 0 else "")
    return f"{sign}${num(abs(x), digits)}"


def price(p: float | None, digits: int = 3) -> str:
    return "—" if p is None else f"{p:.{digits}f}"


def cents(x: float | None, *, signed: bool = True) -> str:
    if x is None:
        return "—"
    return f"{x * 100:+.1f}¢" if signed else f"{x * 100:.1f}¢"


def short_wallet(wallet: str) -> str:
    return f"{wallet[:6]}…{wallet[-4:]}" if len(wallet) > 12 else wallet


def event_url(order: LeaderOrder | None, fallback_slug: str = "") -> str:
    slug = (order.event_slug or order.slug) if order else fallback_slug
    return f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"


def reason_text(status: str, outcome: EntryOutcome | None = None) -> str:
    reason = status.removeprefix("skip:")
    text = REASONS.get(reason, reason)
    book = outcome.snapshot.book if outcome and outcome.snapshot else None
    if outcome is not None and book is not None:
        if reason == "premium" and book.best_ask is not None:
            text += f": ask {book.best_ask:.3f} > {outcome.cap:.3f}"
        elif reason == "depth":
            text += f": до {outcome.cap:.3f} есть {usd(book.usdc_up_to(outcome.cap))} из {usd(outcome.probe_stake)}"
    return text


def _usage_line(usage: dict[str, float]) -> str:
    if not usage:
        return ""
    return (
        f"Риск (доля лимита): рынок {usage.get('market', 0):.0%} · событие {usage.get('event', 0):.0%}"
        f" · трейдер {usage.get('leader', 0):.0%} · категория {usage.get('category', 0):.0%}"
        f" · всего {usage.get('total', 0):.0%}"
    )


def render_signal(o: EntryOutcome, *, now: float | None = None) -> str:
    now = time.time() if now is None else now
    order, entry, lot, v = o.order, o.entry, o.lot, o.variant
    debug = entry.role == "debug"
    lines = [f"🟢 <b>Сигнал: покупка</b> · {esc(entry.tier)}" + (" · <i>DEBUG</i>" if debug else "")]
    theta = "" if debug else f" · θ̃ {entry.theta_shrunk:+.3f}"
    who = esc(entry.user_name or order.name or "—")
    lines.append(f"👤 <b>{who}</b> <code>{esc(short_wallet(order.wallet))}</code>{theta}")
    title = esc(order.title or o.info.question or order.slug or order.token_id[:16])
    lines.append(f'📈 <a href="{attr(event_url(order))}">{title}</a>')
    lines.append(f"Исход: <b>{esc(order.outcome or '?')}</b> · категория {esc(lot.category)}")
    taker = " · тейкер" if order.is_taker else ""
    lines.append(
        f"Лидер: {num(order.size)} sh × {order.vwap:.3f} (худшая {order.worst_price:.3f}) = {usd(order.notional)}"
        f" · {max(0.0, now - order.first_ts):.0f} с назад{taker}"
    )
    snap = o.snapshot
    book = snap.book if snap is not None else None
    if book is None:
        lines.append(f"Стакан: недоступен ({esc(snap.error if snap else 'нет снимка')})")
    else:
        lines.append(
            f"Стакан ({snap.latency_ms} мс): bid {price(book.best_bid)} / ask {price(book.best_ask)}"
            f" · спред {cents(book.spread, signed=False)} · глубина ±2¢: bid {usd(book.depth_usdc('bid'))}"
            f" / ask {usd(book.depth_usdc('ask'))}"
        )
        u = o.uncapped
        if u is not None and u.avg_price is not None:
            short = "" if u.complete else f", стакана хватает только на {usd(u.usdc)}"
            lines.append(
                f"Копия {usd(o.probe_stake)}: VWAP {u.avg_price:.4f} ({cents(u.avg_price - order.vwap)} к лидеру),"
                f" лимит цены {o.cap:.3f}{short}"
            )
    if lot.filled:
        lines.append(
            f"Paper ({v.delay_s} с): ✅ куплено {num(lot.shares)} sh по {price(lot.exec_price, 4)} на {usd(lot.cost)},"
            f" комиссия {usd(lot.entry_fee)}"
        )
    else:
        lines.append(f"Paper ({v.delay_s} с): ⏭ пропуск — {esc(reason_text(lot.status, o))}")
    usage = _usage_line(o.usage)
    if usage:
        lines.append(usage)
    return "\n".join(lines)


def render_exit(o: ExitOutcome, order: LeaderOrder | None, entry: WatchEntry | None, title: str) -> str:
    who = esc((entry.user_name if entry else "") or (order.name if order else "") or "—")
    head = "🔴 <b>Лидер продаёт</b>" if o.kind == "sell" else "🔄 <b>Позиция лидера уменьшилась</b> (merge/перевод)"
    lines = [f"{head} · <b>{who}</b> <code>{esc(short_wallet(o.wallet))}</code>"]
    lines.append(f'📈 <a href="{attr(event_url(order))}">{esc(title or o.token_id[:16])}</a>')
    if order is not None:
        lines.append(f"Лидер: продал {num(order.size)} sh по {order.vwap:.3f} = {o.fraction:.0%} позиции")
    else:
        lines.append(f"Лидер: −{o.fraction:.0%} позиции по данным /positions")
    status = EXIT_STATUS.get(o.status, o.status)
    if o.shares > 0:
        lines.append(
            f"Paper ({o.variant.delay_s} с): {status} {num(o.shares)} sh за {usd(o.proceeds)}, комиссия {usd(o.fee)},"
            f" PnL {usd(o.pnl, signed=True)}"
        )
    else:
        lines.append(f"Paper ({o.variant.delay_s} с): {status}")
    return "\n".join(lines)


def render_settlement(title: str, final_price: float, closed: Sequence[Settled]) -> str:
    lines = [f"🏁 <b>Рынок разрешился</b>: {esc(title)} → {final_price:.2f}"]
    by_variant: dict[Variant, list[Settled]] = defaultdict(list)
    for s in closed:
        by_variant[s.variant].append(s)
    for v in sorted(by_variant, key=lambda x: x.delay_s):
        group = by_variant[v]
        value = sum(s.value for s in group)
        pnl = sum(s.pnl for s in group)
        lines.append(f"Paper ({v.delay_s} с): лотов {len(group)}, выплата {usd(value)}, PnL {usd(pnl, signed=True)}")
    return "\n".join(lines)


def render_kill_switch(variant: Variant, equity: float, peak: float) -> str:
    dd = 1 - equity / peak if peak > 0 else 0.0
    return (
        f"⛔ <b>Kill-switch</b> ({esc(variant.name)}): капитал {usd(equity)}, просадка {dd:.1%} от пика {usd(peak)}."
        "\nНовые paper-входы остановлены. /resume — продолжить."
    )


def render_status(status: dict[str, Any]) -> str:
    rtds = status.get("rtds") or {}
    lines = [
        "📊 <b>Статус</b>" + (" · ⏸ пауза" if status.get("paused") else ""),
        f"Работает {status.get('uptime_s', 0) / 60:.0f} мин · алерты: {esc(status.get('notifier'))}",
        f"RTDS: кадров {rtds.get('frames', 0)}, сделок {rtds.get('trades', 0)}, битых {rtds.get('malformed', 0)},"
        f" переподключений {rtds.get('reconnects', 0)}, последний кадр {rtds.get('last_frame_age_s', 0):.0f} с назад",
        f"Ордера лидеров: покупок {status.get('buy_orders', 0)}, продаж {status.get('sell_orders', 0)};"
        f" в очереди снимков {status.get('pending', 0)}",
        f"Watchlist: {esc(status.get('watchlist'))}",
    ]
    for v in status.get("variants") or []:
        flag = " ⛔" if v.get("killed") else ""
        lines.append(
            f"<b>{esc(v['name'])}</b>{flag}: куплено {v['filled']}, пропусков {v['skipped']}, открыто {v['open_lots']},"
            f" вложено {usd(v['deployed'])}, реализовано {usd(v['realized'], signed=True)},"
            f" капитал {usd(v['equity'])}"
        )
    return "\n".join(lines)


def render_portfolio(variants: Sequence[dict[str, Any]], *, max_rows: int = 15) -> str:
    lines = ["💼 <b>Paper-портфель</b>"]
    for v in variants:
        lines.append(
            f"<b>{esc(v['name'])}</b>: открыто {len(v['positions'])}, вложено {usd(v['deployed'])},"
            f" нереализовано {usd(v['unrealized'], signed=True)}"
        )
        for p in v["positions"][:max_rows]:
            mark = price(p.get("mark"))
            lines.append(
                f"• {esc(p['trader'])}: {esc(p['title'][:60])} — {num(p['shares'])} sh, база {usd(p['basis'])},"
                f" цена {mark}"
            )
        if len(v["positions"]) > max_rows:
            lines.append(f"… и ещё {len(v['positions']) - max_rows}")
    if len(lines) == 1:
        lines.append("Позиций нет.")
    return "\n".join(lines)


def render_watchlist(entries: Sequence[WatchEntry], *, max_rows: int = 40) -> str:
    role_ru = {"alert": "алерты", "track": "paper", "control": "контроль", "debug": "debug"}
    lines = [f"👀 <b>Watchlist</b>: {len(entries)}"]
    order = {"alert": 0, "track": 1, "control": 2, "debug": 3}
    for e in sorted(entries, key=lambda e: (order.get(e.role, 9), -e.theta_shrunk))[:max_rows]:
        lines.append(
            f"• {esc(e.user_name or '—')} <code>{esc(short_wallet(e.wallet))}</code> · {esc(e.tier)}"
            f" · θ̃ {e.theta_shrunk:+.3f} · {role_ru.get(e.role, e.role)}"
        )
    if len(entries) > max_rows:
        lines.append(f"… и ещё {len(entries) - max_rows}")
    return "\n".join(lines)


def _ci(ci: Any) -> str:
    if not ci:
        return "n/a"
    lo, hi = ci
    return f"[{lo:+.1%}; {hi:+.1%}]"


def render_report(report: dict[str, Any]) -> str:
    lines = ["🧾 <b>Paper-отчёт</b> (по задержке)"]
    mtm = report.get("mtm") or {}
    for row in report.get("by_latency") or []:
        missed = ", ".join(f"{esc(k)} {v}" for k, v in sorted(row["missed_by_reason"].items(), key=lambda kv: -kv[1]))
        dd = (mtm.get(row.get("variant") or "") or {}).get("max_drawdown")
        lines.append(
            f"<b>{row['latency_s']} с</b>: сигналов {row['signals']}, куплено {row['filled']},"
            f" вложено {usd(row['cost'])}, PnL {usd(row['pnl'], signed=True)}, ROI {row['roi']:+.1%} {_ci(row['roi_ci95'])},"
            f" комиссии {usd(row['fees'])}, проскальзывание {row['slippage_cents']:+.2f}¢,"
            f" пик капитала {usd(row['peak_capital'])}, просадка (реализ.) {usd(row['max_drawdown'])}"
            + (f", MTM-просадка {usd(dd)}" if dd is not None else "")
            + (f"; пропуски: {missed}" if missed else "")
        )
    if len(lines) == 1:
        lines.append("Сделок пока нет.")
    lines.append("Открытые позиции учтены по себестоимости (PnL 0) до выхода или разрешения рынка.")
    return "\n".join(lines)

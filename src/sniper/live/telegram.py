"""Alert sinks: the Telegram Bot API (plain httpx) and a dry-run sink that appends to a log file.

Credentials come only from SNIPER_TELEGRAM_BOT_TOKEN and SNIPER_TELEGRAM_CHAT_ID. Commands are accepted only from
the configured chat.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)

API_URL = "https://api.telegram.org"
MAX_MESSAGE = 4096
TOKEN_ENV = "SNIPER_TELEGRAM_BOT_TOKEN"
CHAT_ENV = "SNIPER_TELEGRAM_CHAT_ID"

CommandHandler = Callable[[str, list[str]], Awaitable[str | None]]


class Notifier(Protocol):
    name: str

    async def send(self, text: str) -> None: ...

    async def run_commands(self, handler: CommandHandler) -> None: ...

    async def flush(self, timeout: float) -> None: ...

    async def aclose(self) -> None: ...


def fit_message(text: str, limit: int = MAX_MESSAGE) -> str:
    """Cut at a line boundary so HTML tags (always closed within a line here) stay balanced."""
    if len(text) <= limit:
        return text
    cut = text.rfind("\n", 0, limit - 2)
    return (text[:cut] if cut > 0 else text[: limit - 2]) + "\n…"


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """'/status@MyBot arg' -> ('status', ['arg'])."""
    parts = (text or "").strip().split()
    if not parts or not parts[0].startswith("/"):
        return None
    name = parts[0][1:].split("@", 1)[0].lower()
    return (name, parts[1:]) if name else None


class TelegramNotifier:
    name = "telegram"

    def __init__(
        self,
        token: str,
        chat_id: str | int,
        *,
        client: httpx.AsyncClient | None = None,
        min_interval_s: float = 1.0,
        max_queue: int = 200,
        poll_timeout_s: int = 25,
        api_url: str = API_URL,
    ):
        # httpx logs request URLs at INFO, and Bot API URLs embed the token.
        logging.getLogger("httpx").setLevel(max(logging.WARNING, logging.getLogger("httpx").level))
        self.chat_id = str(chat_id).strip()
        self._token = token
        self._base = f"{api_url}/bot{token}"
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(poll_timeout_s + 15, connect=10))
        self._owns_client = client is None
        self.min_interval_s = min_interval_s
        self.max_queue = max_queue
        self.poll_timeout_s = poll_timeout_s
        self.offset: int | None = None
        self.sent = self.failed = self.dropped = self.ignored_updates = 0
        self._queue: asyncio.Queue[str] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._last_sent = 0.0
        self._lock = asyncio.Lock()

    @classmethod
    def from_env(cls, **kwargs: Any) -> TelegramNotifier | None:
        token = os.environ.get(TOKEN_ENV, "").strip()
        chat = os.environ.get(CHAT_ENV, "").strip()
        return cls(token, chat, **kwargs) if token and chat else None

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    async def _call(self, method: str, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        try:
            kwargs: dict[str, Any] = {"json": payload}
            if timeout is not None:
                kwargs["timeout"] = timeout
            resp = await self._client.post(f"{self._base}/{method}", **kwargs)
        except httpx.HTTPError as exc:
            return {"ok": False, "description": self._redact(f"{type(exc).__name__}: {exc}")}
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if resp.status_code != 200 or not body.get("ok"):
            params = body.get("parameters") or {}
            return {
                "ok": False,
                "status": resp.status_code,
                "description": self._redact(str(body.get("description") or resp.reason_phrase)),
                "retry_after": params.get("retry_after"),
            }
        return body

    async def _throttle(self) -> None:
        wait = self._last_sent + self.min_interval_s - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def send_now(self, text: str) -> bool:
        """Send one message (respecting the minimum interval); retries after a 429 `retry_after`."""
        payload = {
            "chat_id": self.chat_id,
            "text": fit_message(text),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        async with self._lock:
            for _ in range(3):
                await self._throttle()
                body = await self._call("sendMessage", payload)
                self._last_sent = time.monotonic()
                if body.get("ok"):
                    self.sent += 1
                    return True
                retry = body.get("retry_after")
                log.warning("telegram sendMessage failed: %s %s", body.get("status"), body.get("description"))
                if retry is None:
                    break
                await asyncio.sleep(float(retry))
        self.failed += 1
        return False

    async def _run_worker(self) -> None:
        assert self._queue is not None
        while True:
            text = await self._queue.get()
            try:
                await self.send_now(text)
            except Exception:
                log.exception("telegram send failed")
            finally:
                self._queue.task_done()

    async def send(self, text: str) -> None:
        """Queue a message; one worker drains the queue at most one message per `min_interval_s`."""
        if self._queue is None:
            self._queue = asyncio.Queue()
            self._worker = asyncio.create_task(self._run_worker())
        if self._queue.qsize() >= self.max_queue:
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()
                self._queue.task_done()
                self.dropped += 1
        self._queue.put_nowait(text)

    async def flush(self, timeout: float) -> None:
        if self._queue is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._queue.join(), timeout)

    async def get_updates(self) -> list[dict[str, Any]] | None:
        payload: dict[str, Any] = {"timeout": self.poll_timeout_s, "allowed_updates": ["message"]}
        if self.offset is not None:
            payload["offset"] = self.offset
        body = await self._call("getUpdates", payload, timeout=self.poll_timeout_s + 15)
        if not body.get("ok"):
            log.warning("telegram getUpdates failed: %s %s", body.get("status"), body.get("description"))
            return None
        updates = [u for u in body.get("result") or [] if isinstance(u, dict) and "update_id" in u]
        if updates:
            self.offset = max(int(u["update_id"]) for u in updates) + 1
        return updates

    def command_of(self, update: dict[str, Any]) -> tuple[str, list[str]] | None:
        msg = update.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id", ""))
        if chat_id != self.chat_id:
            self.ignored_updates += 1
            return None
        return parse_command(msg.get("text") or "")

    async def handle_updates(self, updates: list[dict[str, Any]], handler: CommandHandler) -> int:
        handled = 0
        for update in updates:
            parsed = self.command_of(update)
            if parsed is None:
                continue
            try:
                reply = await handler(*parsed)
            except Exception:
                log.exception("command /%s failed", parsed[0])
                reply = "Ошибка при выполнении команды."
            if reply:
                await self.send(reply)
            handled += 1
        return handled

    async def run_commands(self, handler: CommandHandler) -> None:
        while True:
            updates = await self.get_updates()
            if updates is None:
                await asyncio.sleep(5)
                continue
            await self.handle_updates(updates, handler)

    async def aclose(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        if self._owns_client:
            await self._client.aclose()


class DryRunNotifier:
    """Used when Telegram is not configured: appends rendered messages to a log file and logs them."""

    name = "dry-run"

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self.sent = 0
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    async def send(self, text: str) -> None:
        stamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        if self.path is not None:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(f"===== {stamp} =====\n{text}\n\n")
        log.info("[dry-run alert]\n%s", text)
        self.sent += 1

    async def run_commands(self, handler: CommandHandler) -> None:
        return None

    async def flush(self, timeout: float) -> None:
        return None

    async def aclose(self) -> None:
        return None

"""Phase 2: real-time signal alerts and paper trading against live order books.

Read-only by design: public market-data APIs (RTDS WebSocket, CLOB `/book`, Data API, Gamma) and the Telegram Bot
API. Nothing here places orders, holds keys or signs transactions.
"""

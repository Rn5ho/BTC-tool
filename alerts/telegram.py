"""Telegram bot for sending edge alerts, trade notifications, and periodic stats.

Also listens for interactive commands (/status, /stats, /help) so the user
can query the bot on demand from Telegram.

Uses python-telegram-bot library (v21+, async).
Gracefully degrades if the library is not installed or credentials are missing.
"""

import asyncio
import logging
import re
from typing import Any, Callable, Coroutine, Optional

logger = logging.getLogger(__name__)

# Handle the case where python-telegram-bot is not installed
try:
    from telegram import Bot

    TELEGRAM_AVAILABLE = True
except BaseException:
    Bot = None  # type: ignore[assignment,misc]
    TELEGRAM_AVAILABLE = False
    logger.warning(
        "python-telegram-bot could not be loaded. "
        "Telegram alerts will be disabled. "
        "Install with: pip install python-telegram-bot"
    )

# Telegram's hard limit on message length
_MAX_MESSAGE_LENGTH = 4096


class TelegramAlerter:
    """Sends formatted alerts to a Telegram chat via a bot."""

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token: str = bot_token
        self.chat_id: str = chat_id
        self._bot: Optional[object] = None
        self._enabled: bool = bool(bot_token and chat_id)
        self._update_offset: int = 0
        # Command handlers: command_name -> async fn(args) returning response string
        self._commands: dict[str, Callable[[str], Coroutine[Any, Any, str]]] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Initialise the bot and send a startup message."""
        if not self._enabled:
            logger.warning(
                "Telegram alerter is disabled (missing bot_token or chat_id)"
            )
            return

        if not TELEGRAM_AVAILABLE:
            logger.warning(
                "Telegram alerter is disabled (python-telegram-bot not installed)"
            )
            self._enabled = False
            return

        self._bot = Bot(token=self.bot_token)
        await self._send("BTC Polymarket Edge Finder started")

    async def stop(self) -> None:
        """Send a shutdown message and clean up."""
        if self._enabled and self._bot is not None:
            await self._send("BTC Polymarket Edge Finder stopped")
        self._bot = None

    # ------------------------------------------------------------------
    # Internal send helper
    # ------------------------------------------------------------------

    async def _send(self, text: str, parse_mode: str = "HTML") -> None:
        """Send a message to the configured Telegram chat.

        If the alerter is disabled the message is only logged.
        Messages longer than 4096 characters are truncated.
        Telegram API errors are caught and logged so they never crash the caller.
        """
        if not self._enabled or self._bot is None:
            # Log at DEBUG only — avoids flooding the console when Telegram
            # is disabled.  Strip HTML/emojis to prevent UnicodeEncodeError
            # on Windows cp1252 consoles.
            if logger.isEnabledFor(logging.DEBUG):
                safe = re.sub(r"<[^>]+>", "", text)
                safe = safe.encode("ascii", errors="ignore").decode("ascii")
                safe = re.sub(r"\n{3,}", "\n\n", safe).strip()
                logger.debug("[Telegram disabled] %s", safe)
            return

        # Truncate to Telegram's maximum message length
        if len(text) > _MAX_MESSAGE_LENGTH:
            text = text[: _MAX_MESSAGE_LENGTH - 3] + "..."

        try:
            await self._bot.send_message(  # type: ignore[union-attr]
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode,
            )
        except Exception:
            logger.error("Failed to send Telegram message", exc_info=True)

    # ------------------------------------------------------------------
    # Public alert methods
    # ------------------------------------------------------------------

    async def send_trade_placed_alert(
        self,
        side: str,
        slug: str,
        amount: float,
        entry_price: float,
        confidence: float,
        edge: float,
        order_id: str | None = None,
        success: bool = True,
        error_msg: str = "",
    ) -> None:
        """Send a combined trade placement notification (edge + order details)."""
        if success:
            side_emoji = "\U0001f7e2" if side == "UP" else "\U0001f534"
            text = (
                f"{side_emoji} <b>LIVE TRADE PLACED</b>\n\n"
                f"Market: {slug}\n"
                f"Side: <b>{side}</b> @ {entry_price:.3f}\n"
                f"Size: <b>${amount:.2f}</b>\n"
                f"Model: {min(50 + confidence * 100, 99):.0f}% {side} | Edge: {edge:+.1%}\n"
                f"Order: {order_id or 'N/A'}"
            )
        else:
            text = (
                f"\u274c <b>LIVE TRADE FAILED</b>\n\n"
                f"Market: {slug}\n"
                f"Side: {side} @ {entry_price:.3f}\n"
                f"Amount: ${amount:.2f}\n"
                f"Error: {error_msg}"
            )
        await self._send(text)

    async def send_settlement_alert(
        self,
        slug: str,
        side: str,
        outcome: str,
        pnl: float,
        bankroll: float,
    ) -> None:
        """Send a trade settlement notification (win or loss)."""
        result_emoji = "\u2705" if pnl >= 0 else "\u274c"
        text = (
            f"{result_emoji} <b>TRADE SETTLED</b>\n\n"
            f"Market: {slug}\n"
            f"Side: {side} \u2192 {outcome}\n"
            f"P&amp;L: ${pnl:+.2f}\n"
            f"Bankroll: ${bankroll:.2f}"
        )
        await self._send(text)

    async def send_stats_summary(
        self, stats: dict, live_info: Optional[dict] = None
    ) -> None:
        """Send a periodic statistics summary with live trading as primary."""
        parts = [f"\U0001f4c8 <b>STATS UPDATE</b>\n"]

        # Live trading first (primary)
        if live_info:
            balance = live_info.get("balance")
            db = live_info.get("db_stats", {})
            session = live_info.get("session", {})
            bankroll = live_info.get("bankroll", 0)
            parts.append("\U0001f4b5 <b>Live Trading:</b>")
            if balance is not None:
                parts.append(f"USDC: <b>${balance:.2f}</b> | Bankroll: ${bankroll:.2f}")
            settled = db.get("settled", 0)
            wins = db.get("wins", 0)
            losses = db.get("losses", 0)
            wr = db.get("win_rate", 0)
            pnl = db.get("total_pnl", 0)
            ee = db.get("early_exits", 0)
            ee_str = f" + {ee}ee" if ee else ""
            mk = db.get("maker_fills", 0)
            mk_str = f" + {mk}mk" if mk else ""
            parts.append(
                f"Settled: {settled} | W/L: {wins}/{losses}{ee_str}{mk_str} ({wr:.0%})\n"
                f"P&amp;L: <b>${pnl:+.2f}</b>"
            )
            parts.append(
                f"Session: {session.get('successful', 0)} filled / "
                f"{session.get('total', 0)} total"
            )

        # Paper trading (secondary)
        parts.append(
            f"\n\U0001f4dd <b>Paper Trading:</b>\n"
            f"Settled: {stats.get('settled_trades', 0)} | "
            f"WR: {stats.get('win_rate', 0):.1%}\n"
            f"P&amp;L: ${stats.get('total_pnl', 0):+.2f} | "
            f"Bankroll: ${stats.get('bankroll', 0):.2f}"
        )

        await self._send("\n".join(parts))

    async def send_live_settlement_alert(
        self,
        slug: str,
        side: str,
        outcome: str,
        amount: float,
        entry_price: float = 0.0,
    ) -> None:
        """Send a live trade settlement notification."""
        if outcome == "WIN":
            # Estimate profit: tokens × $1 - cost
            tokens = amount / entry_price if entry_price > 0 else 0
            profit = tokens - amount if tokens > 0 else 0
            text = (
                f"\u2705 <b>LIVE WIN</b>\n\n"
                f"Market: {slug}\n"
                f"Side: {side} | Cost: ${amount:.2f}\n"
                f"Est. profit: ${profit:.2f}"
            )
        else:
            text = (
                f"\u274c <b>LIVE LOSS</b>\n\n"
                f"Market: {slug}\n"
                f"Side: {side} | Lost: ${amount:.2f}"
            )
        await self._send(text)

    async def send_error_alert(self, error: str) -> None:
        """Send an error notification."""
        text = f"\u26a0\ufe0f <b>ERROR:</b> {error}"
        await self._send(text)

    # ------------------------------------------------------------------
    # Interactive command handling
    # ------------------------------------------------------------------

    def register_command(
        self, name: str, handler: Callable[[str], Coroutine[Any, Any, str]]
    ) -> None:
        """Register an async handler for a bot command.

        Args:
            name: Command name without the leading slash (e.g. ``"status"``).
            handler: Async callable that accepts an args string and returns
                the response text (HTML).
        """
        self._commands[name.lower()] = handler

    async def run_command_listener(self) -> None:
        """Poll for incoming Telegram messages and dispatch commands.

        Runs indefinitely. Should be launched as an ``asyncio.Task``.
        Only processes messages from the configured chat_id.
        """
        if not self._enabled or self._bot is None:
            return

        # Skip any messages that arrived before we started
        try:
            updates = await self._bot.get_updates(timeout=0)  # type: ignore[union-attr]
            if updates:
                self._update_offset = updates[-1].update_id + 1
        except Exception:
            pass

        logger.info("Telegram command listener started")

        while True:
            try:
                updates = await self._bot.get_updates(  # type: ignore[union-attr]
                    offset=self._update_offset, timeout=10,
                )
                for update in updates:
                    self._update_offset = update.update_id + 1
                    msg = update.message
                    if msg is None:
                        continue
                    # Only respond to our configured chat
                    if str(msg.chat_id) != str(self.chat_id):
                        continue
                    text = (msg.text or "").strip()
                    if not text.startswith("/"):
                        continue

                    parts = text.split(maxsplit=1)
                    cmd = parts[0].lstrip("/").lower()
                    args = parts[1] if len(parts) > 1 else ""
                    # Strip @botname suffix (e.g. /status@Matic5m_bot)
                    if "@" in cmd:
                        cmd = cmd.split("@")[0]

                    handler = self._commands.get(cmd)
                    if handler is not None:
                        try:
                            response = await handler(args)
                            await self._send(response)
                        except Exception:
                            logger.exception("Error handling /%s command", cmd)
                            await self._send(
                                f"\u26a0\ufe0f Error processing /{cmd}"
                            )
                    elif cmd == "help":
                        cmds = ", ".join(f"/{c}" for c in sorted(self._commands))
                        await self._send(
                            f"\U0001f916 <b>Available commands:</b>\n"
                            f"{cmds}\n/help"
                        )
                    else:
                        await self._send(
                            f"Unknown command: /{cmd}\nTry /help"
                        )

            except asyncio.CancelledError:
                logger.info("Telegram command listener cancelled")
                raise
            except Exception:
                logger.debug("Command listener poll error", exc_info=True)
                await asyncio.sleep(5)

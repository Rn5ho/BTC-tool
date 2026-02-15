"""Telegram bot for sending edge alerts, trade notifications, and periodic stats.

Uses python-telegram-bot library (v21+, async).
Gracefully degrades if the library is not installed or credentials are missing.
"""

import logging
from typing import Optional

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
            logger.info("[Telegram disabled] %s", text)
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

    async def send_edge_alert(
        self, signal: dict, features_breakdown: dict
    ) -> None:
        """Send an edge-detection alert with signal details and feature scores."""
        side_emoji = "\U0001f7e2" if signal["side"] == "UP" else "\U0001f534"
        text = (
            f"\U0001f514 <b>EDGE DETECTED</b>\n\n"
            f"Market: {signal['market_slug']}\n"
            f"{side_emoji} Side: <b>{signal['side']}</b>\n"
            f"Our P({signal['side']}): <b>{signal['our_prob']:.1%}</b>\n"
            f"Market P({signal['side']}): {signal['market_prob']:.1%}\n"
            f"Edge: <b>{signal['edge']:+.1%}</b>\n\n"
            f"\U0001f4ca <b>Signals:</b>\n"
        )
        for name, value in features_breakdown.items():
            text += f"  {name}: {value:+.4f}\n"

        await self._send(text)

    async def send_trade_alert(
        self,
        side: str,
        slug: str,
        size: float,
        entry_price: float,
        our_prob: float,
        edge: float,
    ) -> None:
        """Send a paper-trade placement notification."""
        text = (
            f"\U0001f4dd <b>PAPER TRADE PLACED</b>\n\n"
            f"Market: {slug}\n"
            f"Side: {side} @ {entry_price:.3f}\n"
            f"Size: ${size:.2f}\n"
            f"Our prob: {our_prob:.1%} | Edge: {edge:+.1%}"
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

    async def send_stats_summary(self, stats: dict) -> None:
        """Send a periodic statistics summary."""
        text = (
            f"\U0001f4c8 <b>STATS UPDATE</b>\n\n"
            f"Trades: {stats.get('total_trades', 0)} | Settled: {stats.get('settled_trades', 0)}\n"
            f"Win rate: {stats.get('win_rate', 0):.1%}\n"
            f"Total P&amp;L: ${stats.get('total_pnl', 0):+.2f}\n"
            f"Bankroll: ${stats.get('bankroll', 0):.2f}\n"
            f"ROI: {stats.get('roi', 0):+.1%}"
        )
        await self._send(text)

    async def send_error_alert(self, error: str) -> None:
        """Send an error notification."""
        text = f"\u26a0\ufe0f <b>ERROR:</b> {error}"
        await self._send(text)

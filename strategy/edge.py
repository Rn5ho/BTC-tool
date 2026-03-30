"""Edge detection -- compare our P(up) to Polymarket's implied odds.

When Polymarket taker fees are enabled (crypto 5-min / 15-min markets),
the raw market price no longer represents the true break-even probability.
Buying a share at price *p* with fee factor *f* means you receive fewer
shares, so the break-even probability is ``p / (1 - f)`` rather than ``p``.

The edge calculation adjusts for this: we compare our model probability
against the fee-adjusted implied probability to ensure the edge is real
*after* fees.
"""

import logging
from typing import Optional

from config import settings
from data.models import FeatureVector, PolymarketMarket
from data.polymarket import compute_fee_factor
from signals.probability import ProbabilityModel

logger = logging.getLogger(__name__)


class EdgeDetector:
    """Find edges between our probability model and Polymarket prices."""

    def __init__(
        self,
        model: ProbabilityModel,
        min_edge: float = 0.05,
        max_edge: float = 0.20,
        fee_rate: float = 0.0,
        fee_exponent: int = 2,
        always_trade: bool = False,
        min_confidence: float = 0.015,
        side_selection: str = "model",
    ) -> None:
        self.model = model
        self.min_edge = min_edge
        self.max_edge = max_edge
        self.fee_rate = fee_rate
        self.fee_exponent = fee_exponent
        self.always_trade = always_trade
        self.min_confidence = min_confidence
        self.side_selection = side_selection
        self.last_skip_reason: str | None = None
        logger.info(
            "EdgeDetector initialised with min_edge=%.2f, max_edge=%.2f, fee_rate=%.3f, "
            "fee_exponent=%d, always_trade=%s, min_confidence=%.3f, side_selection=%s",
            self.min_edge,
            self.max_edge,
            self.fee_rate,
            self.fee_exponent,
            self.always_trade,
            self.min_confidence,
            self.side_selection,
        )

    def _fee_adjusted_prob(self, market_price: float) -> float:
        """Compute the fee-adjusted break-even probability for a market price.

        When buying shares at price *p*, taker fees reduce the number of
        shares received.  The effective break-even probability becomes:
            p_adjusted = p / (1 - fee_factor)

        If fee_rate is 0 (fee-free market), returns the raw price.
        """
        if self.fee_rate <= 0.0:
            return market_price
        ff = compute_fee_factor(market_price, self.fee_rate, self.fee_exponent)
        if ff >= 1.0:
            return 1.0
        return market_price / (1.0 - ff)

    def evaluate(
        self,
        features: FeatureVector,
        market: PolymarketMarket,
        p_up_override: float | None = None,
    ) -> Optional[dict]:
        """Compare our probability estimate against Polymarket prices.

        When *always_trade* is False (classic mode), returns an edge-report
        dict only when the best positive edge exceeds *min_edge*.

        When *always_trade* is True, always returns a signal — the ML model's
        predicted direction — regardless of edge size. Edge is still computed
        for bet sizing and logging.

        Parameters
        ----------
        p_up_override : float, optional
            If provided, use this P(up) instead of calling self.model.predict().
            Allows main.py to pass the ML model's candle-based prediction.
        """
        p_up = p_up_override if p_up_override is not None else self.model.predict(features)

        # Raw market prices
        raw_up = market.up_price
        raw_down = market.down_price

        # Fee-adjusted break-even probabilities
        adj_up = self._fee_adjusted_prob(raw_up)
        adj_down = self._fee_adjusted_prob(raw_down)

        up_edge = p_up - adj_up
        down_edge = (1.0 - p_up) - adj_down

        logger.debug(
            "evaluate: p_up=%.4f  mkt_up=%.4f(adj=%.4f)  "
            "mkt_down=%.4f(adj=%.4f)  up_edge=%.4f  down_edge=%.4f",
            p_up,
            raw_up,
            adj_up,
            raw_down,
            adj_down,
            up_edge,
            down_edge,
        )

        if self.always_trade:
            confidence = abs(p_up - 0.5)

            if self.side_selection == "cheaper":
                # Cheaper-side mode: always buy whichever side has the lower
                # ask price.  Model direction is ignored.  No confidence gate
                # since we're not using the model for side selection.
                up_ask = market.up_best_ask if market.up_best_ask else raw_up
                down_ask = market.down_best_ask if market.down_best_ask else raw_down
                best_side = "UP" if up_ask <= down_ask else "DOWN"
                best_edge = up_edge if best_side == "UP" else down_edge
            else:
                # Model mode: pick direction based on ML model's P(up).
                # Skip when model confidence is below threshold.
                if confidence < self.min_confidence:
                    self.last_skip_reason = (
                        f"low confidence ({confidence*100:.1f}% < {self.min_confidence*100:.1f}%)"
                    )
                    return None
                best_side = "UP" if p_up > 0.5 else "DOWN"
                best_edge = up_edge if best_side == "UP" else down_edge

            # In always-trade mode, edge cap is not applied — we trade the
            # model's direction regardless of edge size.  The entry price filter
            # below handles the real risk (thin books / stale prices).
        else:
            # Classic mode: only trade when we have a positive edge above threshold.
            candidates = []
            if up_edge > 0:
                candidates.append(("UP", up_edge))
            if down_edge > 0:
                candidates.append(("DOWN", down_edge))

            if not candidates:
                logger.debug(
                    "No positive edge on either side (up=%.4f, down=%.4f)",
                    up_edge,
                    down_edge,
                )
                return None

            best_side, best_edge = max(candidates, key=lambda x: x[1])

            if best_edge < self.min_edge:
                logger.debug(
                    "No actionable edge (best=%.4f, threshold=%.4f)",
                    best_edge,
                    self.min_edge,
                )
                return None

            if best_edge > self.max_edge:
                logger.info(
                    "Skipping edge — too large (%.1f%% > %.1f%% cap) on %s %s",
                    best_edge * 100,
                    self.max_edge * 100,
                    best_side,
                    market.slug,
                )
                return None

            confidence = abs(p_up - 0.5)

        # Entry price: use best ask (taker price) if available, midpoint fallback
        midpoint_price = raw_up if best_side == "UP" else raw_down
        if best_side == "UP":
            entry_price = market.up_best_ask if market.up_best_ask else raw_up
            spread = market.up_spread
        else:
            entry_price = market.down_best_ask if market.down_best_ask else raw_down
            spread = market.down_spread

        # Reject entry prices outside the tradeable range.
        # Core range: LIVE_ENTRY_MIN to LIVE_ENTRY_MAX (default 0.40-0.50) — EE sweet spot.
        # Exploration range 0.25-LIVE_ENTRY_MIN and LIVE_ENTRY_MAX-0.65: paper-only.
        # Below 0.25: too contrarian to be useful.
        if entry_price > 0.65 or entry_price < 0.25:
            self.last_skip_reason = (
                f"entry price ${entry_price:.3f} outside 0.25-0.65"
            )
            logger.info(
                "Skipping — %s on %s %s",
                self.last_skip_reason, best_side, market.slug,
            )
            return None

        exploration = entry_price < settings.live_entry_min or entry_price >= settings.live_entry_max
        if exploration:
            logger.info(
                "Exploration signal — entry price %.3f outside %.2f-%.2f live range on %s %s",
                entry_price, settings.live_entry_min, settings.live_entry_max,
                best_side, market.slug,
            )

        fee_factor = compute_fee_factor(
            entry_price, self.fee_rate, self.fee_exponent
        )

        # Build signal breakdown using the actual p_up used for the trade
        signals = {
            "ml_p_up": p_up,
            "ml_confidence": confidence,
            "ml_side": best_side,
        }

        result = {
            "side": best_side,
            "our_prob": p_up if best_side == "UP" else (1.0 - p_up),
            "market_prob": raw_up if best_side == "UP" else raw_down,
            "edge": best_edge,
            "entry_price": entry_price,
            "midpoint_price": midpoint_price,
            "spread": spread,
            "fee_factor": fee_factor,
            "confidence": confidence,
            "signals": signals,
            "market_slug": market.slug,
            "exploration": exploration,
        }

        logger.debug(
            "Edge detected on %s: our=%.4f  market=%.4f  edge=%.4f  "
            "confidence=%.4f  fee_factor=%.4f  slug=%s",
            result["side"],
            result["our_prob"],
            result["market_prob"],
            result["edge"],
            confidence,
            fee_factor,
            result["market_slug"],
        )

        return result

    def format_signal_report(
        self, evaluation: dict, features: FeatureVector
    ) -> str:
        """Format a human-readable edge analysis report for logging/alerts."""
        signals = evaluation.get("signals", {})

        lines = [
            "=" * 50,
            "  EDGE SIGNAL REPORT",
            "=" * 50,
            f"  Market : {evaluation['market_slug']}",
            f"  Side   : {evaluation['side']}",
            f"  Our P  : {evaluation['our_prob']:.4f}",
            f"  Mkt P  : {evaluation['market_prob']:.4f}",
            f"  Edge   : {evaluation['edge']:+.4f} (after fees)",
            f"  Entry  : {evaluation['entry_price']:.4f}",
            f"  Fee    : {evaluation.get('fee_factor', 0):.4f} ({evaluation.get('fee_factor', 0) * 100:.2f}%)",
            "-" * 50,
            "  Signal Breakdown:",
        ]

        for name, value in signals.items():
            bar_len = int(abs(value) * 40)
            direction = "+" if value >= 0 else "-"
            bar = direction * bar_len
            lines.append(f"    {name:>10s}: {value:+.4f}  {bar}")

        lines.append("-" * 50)
        lines.append(
            f"  Features @ ts={features.timestamp}  "
            f"RSI={features.rsi:.1f}  OBI={features.obi:.4f}  "
            f"Funding={features.funding_rate:.6f}"
        )
        lines.append("=" * 50)

        report = "\n".join(lines)
        logger.debug("Signal report:\n%s", report)
        return report

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
    ) -> None:
        self.model = model
        self.min_edge = min_edge
        self.max_edge = max_edge
        self.fee_rate = fee_rate
        self.fee_exponent = fee_exponent
        logger.info(
            "EdgeDetector initialised with min_edge=%.2f, max_edge=%.2f, fee_rate=%.3f, fee_exponent=%d",
            self.min_edge,
            self.max_edge,
            self.fee_rate,
            self.fee_exponent,
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
        self, features: FeatureVector, market: PolymarketMarket
    ) -> Optional[dict]:
        """Compare our probability estimate against Polymarket prices.

        Returns an edge-report dict when the best positive edge exceeds
        *min_edge* **after taker fees**, otherwise ``None``.
        """
        p_up = self.model.predict(features)

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

        # Only consider sides where we have a POSITIVE edge
        # (our probability exceeds the market's implied probability).
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

        # Pick the side with the larger positive edge
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
                "Skipping edge — too large (%.1f%% > %.1f%% cap) on %s %s — model likely overconfident",
                best_edge * 100,
                self.max_edge * 100,
                best_side,
                market.slug,
            )
            return None

        entry_price = raw_up if best_side == "UP" else raw_down
        fee_factor = compute_fee_factor(
            entry_price, self.fee_rate, self.fee_exponent
        )

        result = {
            "side": best_side,
            "our_prob": p_up if best_side == "UP" else (1.0 - p_up),
            "market_prob": raw_up if best_side == "UP" else raw_down,
            "edge": best_edge,
            "entry_price": entry_price,
            "fee_factor": fee_factor,
            "signals": self.model.get_signal_breakdown(features),
            "market_slug": market.slug,
        }

        logger.debug(
            "Edge detected on %s: our=%.4f  market=%.4f  edge=%.4f  "
            "fee_factor=%.4f  slug=%s",
            result["side"],
            result["our_prob"],
            result["market_prob"],
            result["edge"],
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

"""Edge detection -- compare our P(up) to Polymarket's implied odds."""

import logging
from typing import Optional

from data.models import FeatureVector, PolymarketMarket
from signals.probability import ProbabilityModel

logger = logging.getLogger(__name__)


class EdgeDetector:
    """Find edges between our probability model and Polymarket prices."""

    def __init__(
        self, model: ProbabilityModel, min_edge: float = 0.05
    ) -> None:
        self.model = model
        self.min_edge = min_edge
        logger.info(
            "EdgeDetector initialised with min_edge=%.2f", self.min_edge
        )

    def evaluate(
        self, features: FeatureVector, market: PolymarketMarket
    ) -> Optional[dict]:
        """Compare our probability estimate against Polymarket prices.

        Returns an edge-report dict when the absolute edge on the best
        side exceeds *min_edge*, otherwise ``None``.
        """
        p_up = self.model.predict(features)
        market_p_up = market.up_price

        up_edge = p_up - market_p_up
        down_edge = (1.0 - p_up) - market.down_price

        logger.debug(
            "evaluate: p_up=%.4f  market_p_up=%.4f  up_edge=%.4f  down_edge=%.4f",
            p_up,
            market_p_up,
            up_edge,
            down_edge,
        )

        # Choose the side with the larger absolute edge.
        if abs(up_edge) >= abs(down_edge):
            best_side = "UP"
            best_edge = up_edge
        else:
            best_side = "DOWN"
            best_edge = down_edge

        if abs(best_edge) < self.min_edge:
            logger.debug(
                "No actionable edge (best=%.4f, threshold=%.4f)",
                best_edge,
                self.min_edge,
            )
            return None

        result = {
            "side": best_side,
            "our_prob": p_up if best_side == "UP" else (1.0 - p_up),
            "market_prob": market.up_price if best_side == "UP" else market.down_price,
            "edge": best_edge,
            "entry_price": market.up_price if best_side == "UP" else market.down_price,
            "signals": self.model.get_signal_breakdown(features),
            "market_slug": market.slug,
        }

        logger.debug(
            "Edge detected on %s: our=%.4f  market=%.4f  edge=%.4f  slug=%s",
            result["side"],
            result["our_prob"],
            result["market_prob"],
            result["edge"],
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
            f"  Edge   : {evaluation['edge']:+.4f}",
            f"  Entry  : {evaluation['entry_price']:.4f}",
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

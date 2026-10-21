"""Paper-trading risk controls. No broker order-placement code lives here."""

from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class RiskLimits:
    account_equity: float = 100000.0
    risk_per_trade_percent: float = 0.5
    max_daily_loss_percent: float = 2.0
    max_open_positions: int = 3
    max_position_value_percent: float = 20.0
    # #21/#22 extra guardrails (opt-in via engine flag `risk_guardrails`)
    max_consecutive_losses: int = 3
    max_daily_drawdown_percent: float = 3.0


@dataclass(frozen=True)
class SizingDecision:
    approved: bool
    quantity: int
    risk_amount: float
    position_value: float
    reason: str


class PaperRiskManager:
    """Approves paper entries using loss, exposure, and position-count limits."""

    def __init__(self, limits: RiskLimits):
        self.limits = limits
        # #21/#22 runtime state (only enforced when `risk_guardrails` enabled).
        self.consecutive_losses: int = 0
        self.daily_drawdown_pct: float = 0.0
        self._trade_day: date = date.today()

    def reset_daily(self):
        """Call at market open to clear daily rolling state (#21/#22)."""
        self.consecutive_losses = 0
        self.daily_drawdown_pct = 0.0
        self._trade_day = date.today()

    def on_trade_closed(self, pnl_percent: float):
        """Update trailing state after a paper trade closes (#21/#22)."""
        if date.today() != self._trade_day:
            self.reset_daily()
        if pnl_percent < 0:
            self.consecutive_losses += 1
            self.daily_drawdown_pct = max(self.daily_drawdown_pct, abs(pnl_percent))
        else:
            self.consecutive_losses = 0

    def _guardrails_ok(self, guardrails_enabled: bool) -> "tuple[bool, str]":
        if not guardrails_enabled:
            return True, ""
        if self.consecutive_losses >= self.limits.max_consecutive_losses:
            return False, (
                f"Consecutive-loss guardrail hit ({self.consecutive_losses}>= "
                f"{self.limits.max_consecutive_losses}); paper entries paused"
            )
        if self.daily_drawdown_pct >= self.limits.max_daily_drawdown_percent:
            return False, (
                f"Daily-drawdown guardrail hit ({self.daily_drawdown_pct:.1f}%>= "
                f"{self.limits.max_daily_drawdown_percent}%); paper entries paused"
            )
        return True, ""

    def size(self, entry: float, stop_loss: float, open_positions: int = 0, realized_pnl: float = 0.0,
             guardrails_enabled: bool = False) -> SizingDecision:
        if entry <= 0 or stop_loss <= 0 or entry == stop_loss:
            return SizingDecision(False, 0, 0.0, 0.0, "A valid entry and stop-loss are required")
        if open_positions >= self.limits.max_open_positions:
            return SizingDecision(False, 0, 0.0, 0.0, "Maximum concurrent paper positions reached")
        if realized_pnl <= -(self.limits.account_equity * self.limits.max_daily_loss_percent / 100):
            return SizingDecision(False, 0, 0.0, 0.0, "Daily loss limit reached; new paper entries blocked")
        ok, reason = self._guardrails_ok(guardrails_enabled)
        if not ok:
            return SizingDecision(False, 0, 0.0, 0.0, reason)
        allowed_risk = self.limits.account_equity * self.limits.risk_per_trade_percent / 100
        risk_per_share = abs(entry - stop_loss)
        quantity = min(int(allowed_risk // risk_per_share), int((self.limits.account_equity * self.limits.max_position_value_percent / 100) // entry))
        if quantity < 1:
            return SizingDecision(False, 0, allowed_risk, 0.0, "Risk limits do not allow one share")
        return SizingDecision(True, quantity, round(quantity * risk_per_share, 2), round(quantity * entry, 2), "Paper entry approved")

from dataclasses import dataclass
from typing import Optional

import pandas as pd

import config
from engine.risk_manager import calc_position_size, calc_tp1_price, calc_trail_stop


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    stop_loss: float
    shares: int
    tp1_price: float

    # Mutable state
    exit_date: Optional[pd.Timestamp] = None
    exit_price: Optional[float] = None
    exit_reason: str = ""
    pnl: float = 0.0          # total realised P&L (TP1 + runner) — for reporting only
    r_multiple: float = 0.0

    tp1_hit: bool = False
    shares_remaining: int = 0
    highest_close: float = 0.0
    trail_stop: float = 0.0
    hold_days: int = 0
    initial_stop: float = 0.0

    # Capital credit to apply THIS bar (cleared by backtester after each update)
    tp1_cash_credit: float = 0.0

    def __post_init__(self):
        self.shares_remaining = self.shares
        self.highest_close = self.entry_price
        self.trail_stop = self.stop_loss
        self.initial_stop = self.stop_loss

    @property
    def r(self) -> float:
        return self.entry_price - self.initial_stop

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "entry_date": self.entry_date,
            "entry_price": round(self.entry_price, 2),
            "stop_loss": round(self.stop_loss, 2),
            "tp1_price": round(self.tp1_price, 2),
            "shares": self.shares,
            "exit_date": self.exit_date,
            "exit_price": round(self.exit_price, 2) if self.exit_price else None,
            "exit_reason": self.exit_reason,
            "pnl": round(self.pnl, 2),
            "r_multiple": round(self.r_multiple, 2),
        }


class TradeEngine:
    def __init__(self):
        self.open_trades: list[Trade] = []
        self.closed_trades: list[Trade] = []

    # ── Open ─────────────────────────────────────────────────────────────────

    def open_trade(
        self,
        symbol: str,
        entry_price: float,
        stop_loss: float,
        entry_date: pd.Timestamp,
        capital: float,
    ) -> Optional[Trade]:
        if capital <= 0:
            return None
        if len(self.open_trades) >= config.MAX_POSITIONS:
            return None
        shares = calc_position_size(capital, entry_price, stop_loss)
        if shares <= 0:
            return None

        # Clamp to 25% of available capital — scale down rather than reject
        position_cost = shares * entry_price
        if position_cost > capital * 0.25:
            shares = int(capital * 0.25 / entry_price)
            if shares <= 0:
                return None

        r = entry_price - stop_loss
        trade = Trade(
            symbol=symbol,
            entry_date=entry_date,
            entry_price=entry_price,
            stop_loss=stop_loss,
            shares=shares,
            tp1_price=calc_tp1_price(entry_price, r),
        )
        self.open_trades.append(trade)
        return trade

    # ── Update (one bar) ─────────────────────────────────────────────────────

    def update_trade(
        self,
        trade: Trade,
        bar: dict,
        date: pd.Timestamp,
        exit_method: str = "atr_trail",
    ) -> Optional[float]:
        """
        Process one OHLC bar.

        Returns the RUNNER PnL (float) if the trade closed this bar, else None.

        TP1 partial profits are signalled via trade.tp1_cash_credit so the
        backtester can credit capital immediately without waiting for full close.

        Capital accounting (no entry deduction — pure P&L model):
          Open  : no capital change
          TP1   : capital += tp1_cash_credit  (backtester applies this)
          Close : capital += runner_pnl       (return value of this method)
        """
        trade.hold_days += 1
        open_p = float(bar["open"])
        high_p = float(bar["high"])
        low_p  = float(bar["low"])
        close_p = float(bar["close"])
        atr10 = float(bar.get("atr10") or trade.r or 1.0)

        # 1. Gap-down open at or below stop
        if open_p <= trade.stop_loss:
            return self._close(trade, open_p, date, "stop_gap")

        # 2. Intraday stop
        if low_p <= trade.stop_loss:
            return self._close(trade, trade.stop_loss, date, "stop_loss")

        # 3. TP1 partial exit (30 % of shares)
        if not trade.tp1_hit and high_p >= trade.tp1_price:
            tp1_shares = max(1, int(trade.shares * config.TP1_SIZE_PCT))
            partial_pnl = tp1_shares * (trade.tp1_price - trade.entry_price)

            trade.pnl += partial_pnl
            trade.tp1_cash_credit = partial_pnl   # backtester credits capital now
            trade.shares_remaining = trade.shares - tp1_shares
            trade.tp1_hit = True
            trade.stop_loss = trade.entry_price   # move stop to breakeven
            trade.trail_stop = trade.entry_price

        # 4. Update trailing high
        if close_p > trade.highest_close:
            trade.highest_close = close_p

        # 5. Runner exit logic (only after TP1)
        if trade.tp1_hit:
            if exit_method == "atr_trail":
                new_trail = calc_trail_stop(trade.highest_close, atr10)
                trade.trail_stop = max(trade.trail_stop, new_trail)
                if low_p <= trade.trail_stop:
                    fill = max(trade.trail_stop, open_p)
                    return self._close(trade, fill, date, "trail_stop")

            elif exit_method == "ema":
                ema10 = bar.get("ema10")
                if ema10 is not None and close_p < float(ema10):
                    return self._close(trade, close_p, date, "ema_exit")

        # 6. Time stop
        if trade.hold_days >= config.MAX_HOLD_DAYS:
            return self._close(trade, close_p, date, "time_stop")

        return None

    # ── Close ────────────────────────────────────────────────────────────────

    def _close(
        self, trade: Trade, exit_price: float, date: pd.Timestamp, reason: str
    ) -> float:
        """
        Finalise a trade and return RUNNER PnL only.

        Runner PnL = proceeds on shares still open at time of close.
        TP1 PnL was already credited to capital via tp1_cash_credit.
        trade.pnl accumulates both parts for reporting.
        """
        runner_pnl = trade.shares_remaining * (exit_price - trade.entry_price)
        trade.pnl += runner_pnl
        trade.exit_date = date
        trade.exit_price = exit_price
        trade.exit_reason = reason

        total_risk = trade.shares * trade.r
        trade.r_multiple = trade.pnl / total_risk if total_risk > 0 else 0.0

        self.open_trades.remove(trade)
        self.closed_trades.append(trade)
        return runner_pnl   # NOT trade.pnl — TP1 portion already credited separately

    # ── Force-close all open trades ───────────────────────────────────────────

    def close_all(self, date: pd.Timestamp, prices: dict[str, float]) -> float:
        """
        Close every remaining open trade at last known price.
        Returns the total runner PnL so the caller can update capital.
        """
        total_runner_pnl = 0.0
        for trade in list(self.open_trades):
            price = prices.get(trade.symbol, trade.entry_price)
            total_runner_pnl += self._close(trade, price, date, "end_of_backtest")
        return total_runner_pnl

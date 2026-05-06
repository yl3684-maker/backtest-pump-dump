import numpy as np
import pandas as pd

import config


def compute_metrics(trades_df: pd.DataFrame, equity_df: pd.DataFrame) -> dict:
    if trades_df.empty:
        return {"error": "No trades to analyse"}

    total = len(trades_df)
    winners = trades_df[trades_df["pnl"] > 0]
    losers = trades_df[trades_df["pnl"] <= 0]

    win_rate = len(winners) / total
    loss_rate = 1.0 - win_rate

    avg_win = float(winners["pnl"].mean()) if len(winners) else 0.0
    avg_loss = float(losers["pnl"].mean()) if len(losers) else 0.0
    avg_win_r = float(winners["r_multiple"].mean()) if len(winners) else 0.0
    avg_loss_r = float(losers["r_multiple"].mean()) if len(losers) else 0.0

    gross_profit = float(winners["pnl"].sum()) if len(winners) else 0.0
    gross_loss = abs(float(losers["pnl"].sum())) if len(losers) else 0.0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    # Expectancy = (win_rate × avg_win) + (loss_rate × avg_loss)
    expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)
    avg_r = float(trades_df["r_multiple"].mean())

    # Max drawdown
    max_dd_dollar = 0.0
    max_dd_pct = 0.0
    total_return_pct = 0.0

    if not equity_df.empty and "equity" in equity_df.columns:
        eq = equity_df["equity"].values.astype(float)
        peak = eq[0]
        for val in eq:
            if val > peak:
                peak = val
            dd = peak - val
            dd_p = dd / peak if peak > 0 else 0.0
            max_dd_dollar = max(max_dd_dollar, dd)
            max_dd_pct = max(max_dd_pct, dd_p)
        total_return_pct = (eq[-1] - config.INITIAL_CAPITAL) / config.INITIAL_CAPITAL

    # Annualised Sharpe (daily returns, 252 trading days)
    sharpe = 0.0
    if not equity_df.empty and len(equity_df) > 1:
        daily_ret = equity_df["equity"].pct_change().dropna()
        std = daily_ret.std()
        if std > 0:
            sharpe = float((daily_ret.mean() / std) * np.sqrt(252))

    # Consecutive wins/losses
    streaks = _streak_stats(trades_df)

    return {
        "total_trades": total,
        "winners": len(winners),
        "losers": len(losers),
        "win_rate_%": round(win_rate * 100, 1),
        "loss_rate_%": round(loss_rate * 100, 1),
        "avg_win_$": round(avg_win, 2),
        "avg_loss_$": round(avg_loss, 2),
        "avg_win_R": round(avg_win_r, 2),
        "avg_loss_R": round(avg_loss_r, 2),
        "avg_R_multiple": round(avg_r, 2),
        "profit_factor": round(profit_factor, 2),
        "expectancy_$": round(expectancy, 2),
        "gross_profit_$": round(gross_profit, 2),
        "gross_loss_$": round(gross_loss, 2),
        "max_drawdown_$": round(max_dd_dollar, 2),
        "max_drawdown_%": round(max_dd_pct * 100, 1),
        "sharpe_ratio": round(sharpe, 2),
        "total_return_%": round(total_return_pct * 100, 1),
        "max_consec_wins": streaks["max_wins"],
        "max_consec_losses": streaks["max_losses"],
    }


def _streak_stats(trades_df: pd.DataFrame) -> dict:
    wins = (trades_df["pnl"] > 0).astype(int).tolist()
    max_w = max_l = cur_w = cur_l = 0
    for w in wins:
        if w:
            cur_w += 1
            cur_l = 0
        else:
            cur_l += 1
            cur_w = 0
        max_w = max(max_w, cur_w)
        max_l = max(max_l, cur_l)
    return {"max_wins": max_w, "max_losses": max_l}

import numpy as np
import pandas as pd

import config


# ── Indicator computation ────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add all technical indicators to the dataframe.
    Every value only uses data up to and including that bar — zero lookahead.
    """
    df = df.copy()

    # Volume SMA20
    df["sma20_vol"] = df["volume"].rolling(config.RVOL_WINDOW, min_periods=config.RVOL_WINDOW).mean()

    # Bollinger Bands (30-day)
    df["sma30"] = df["close"].rolling(config.BB_WINDOW, min_periods=config.BB_WINDOW).mean()
    df["std30"] = df["close"].rolling(config.BB_WINDOW, min_periods=config.BB_WINDOW).std()
    df["upper_bb"] = df["sma30"] + 2.0 * df["std30"]

    # EMA10
    df["ema10"] = df["close"].ewm(span=config.EMA_WINDOW, adjust=False).mean()

    # ATR10
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift(1)).abs()
    lc = (df["low"] - df["close"].shift(1)).abs()
    df["tr"] = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    df["atr10"] = df["tr"].rolling(config.ATR_WINDOW, min_periods=config.ATR_WINDOW).mean()

    # 5-day rolling high/low for momentum calculation
    df["roll_high_5d"] = df["high"].rolling(config.MOMENTUM_WINDOW, min_periods=config.MOMENTUM_WINDOW).max()
    df["roll_low_5d"] = df["low"].rolling(config.MOMENTUM_WINDOW, min_periods=config.MOMENTUM_WINDOW).min()

    return df


# ── Signal detection ─────────────────────────────────────────────────────────

def detect_signals(df: pd.DataFrame) -> pd.Series:
    """
    Returns a boolean Series where True marks a valid setup bar.
    Uses only data available at bar close — no lookahead.
    """
    df = compute_indicators(df)

    # A. 5-Day Momentum Expansion  ≥ 50%
    denom_mom = df["roll_low_5d"].replace(0, np.nan)
    move_pct = (df["roll_high_5d"] - df["roll_low_5d"]) / denom_mom
    cond_momentum = move_pct >= config.MOMENTUM_THRESHOLD

    # B. Relative Volume ≥ 2×
    rvol = df["volume"] / df["sma20_vol"].replace(0, np.nan)
    cond_rvol = rvol >= config.RVOL_THRESHOLD

    # C. Bollinger Band Breakout
    cond_bb = df["close"] > df["upper_bb"]

    # D. Retention Filter < 0.75
    pullback_low = df["low"].rolling(3, min_periods=1).min()
    peak = df["roll_high_5d"]
    base_low = df["roll_low_5d"]
    denom_ret = (peak - base_low).replace(0, np.nan)
    retracement = (peak - pullback_low) / denom_ret
    cond_retention = retracement < config.RETENTION_THRESHOLD

    signal = cond_momentum & cond_rvol & cond_bb & cond_retention
    return signal.fillna(False)


# ── Inside bar scanner ───────────────────────────────────────────────────────

def find_inside_bar(df: pd.DataFrame, signal_iloc: int, max_wait: int = 5) -> int:
    """
    Scan forward from signal_iloc+1 for an inside bar.
    Returns iloc of inside bar, or -1 if not found within max_wait bars.
    """
    limit = min(signal_iloc + max_wait + 1, len(df) - 1)
    for i in range(signal_iloc + 1, limit):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        if row["high"] < prev["high"] and row["low"] > prev["low"]:
            return i
    return -1

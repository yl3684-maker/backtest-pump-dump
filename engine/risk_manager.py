import config


def calc_position_size(capital: float, entry: float, stop: float) -> int:
    if capital <= 0 or entry <= 0:
        return 0
    shares = config.FIXED_SHARES
    # Risk-based cap: never risk more than POSITION_RISK_PCT of available capital
    if 0 < stop < entry:
        risk_per_share = entry - stop
        risk_shares = int(capital * config.POSITION_RISK_PCT / risk_per_share)
        shares = min(shares, risk_shares)
    return max(0, shares)


def calc_tp1_price(entry: float, r: float) -> float:
    return entry + r * config.TP1_R_MULTIPLE


def calc_trail_stop(highest_close: float, atr10: float) -> float:
    return highest_close - config.TRAILING_ATR_MULT * atr10

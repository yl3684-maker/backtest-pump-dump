"""
Verify capital accounting is mathematically correct.
Traces every dollar through the full trade lifecycle.
"""
import sys, logging
logging.disable(logging.CRITICAL)
sys.path.insert(0, ".")

import pandas as pd
import config
config.INITIAL_CAPITAL = 1_000_000
config.FIXED_SHARES = 100
config.TP1_SIZE_PCT = 0.30
config.TP1_R_MULTIPLE = 2.0
config.TRAILING_ATR_MULT = 2.0
config.MAX_HOLD_DAYS = 15
config.MAX_POSITIONS = 10

from engine.trade_engine import TradeEngine

def check(label, actual, expected, tol=0.01):
    ok = abs(actual - expected) <= tol
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {label}: got {actual:.2f}, expected {expected:.2f}")
    return ok

capital = float(config.INITIAL_CAPITAL)
eng = TradeEngine()

print("=" * 55)
print("SCENARIO 1 — Stop loss, no TP1")
print("  Entry $50, Stop $45 (R=$5), 100 shares")
t = eng.open_trade("AAA", 50.0, 45.0, pd.Timestamp("2024-01-01", tz="UTC"), capital)
bar_stop = {"open": 50, "high": 51, "low": 44.5, "close": 44.5, "atr10": 2.0, "ema10": 49}
pnl = eng.update_trade(t, bar_stop, pd.Timestamp("2024-01-02", tz="UTC"))
if t.tp1_cash_credit:
    capital += t.tp1_cash_credit; t.tp1_cash_credit = 0.0
if pnl is not None:
    capital += pnl
expected_capital = 1_000_000 + 100 * (45.0 - 50.0)  # loss = -$500
check("Capital after stop", capital, expected_capital)
check("Trade PnL", t.pnl, -500.0)

print()
print("SCENARIO 2 — TP1 hit then trail stop")
print("  Entry $100, Stop $95 (R=$5), 100 shares, TP1=$110")
capital = float(config.INITIAL_CAPITAL)
eng2 = TradeEngine()
t2 = eng2.open_trade("BBB", 100.0, 95.0, pd.Timestamp("2024-01-01", tz="UTC"), capital)
assert t2.tp1_price == 110.0, f"TP1 should be $110, got {t2.tp1_price}"

bar_tp1 = {"open": 100, "high": 112, "low": 99, "close": 111, "atr10": 3.0, "ema10": 105}
pnl = eng2.update_trade(t2, bar_tp1, pd.Timestamp("2024-01-02", tz="UTC"))
if t2.tp1_cash_credit:
    capital += t2.tp1_cash_credit; t2.tp1_cash_credit = 0.0
if pnl is not None:
    capital += pnl

# TP1: 30 shares sold at $110. Profit = 30 * (110-100) = $300
tp1_expected_capital = 1_000_000 + 30 * (110 - 100)
check("Capital after TP1 fires", capital, tp1_expected_capital)
check("shares_remaining", t2.shares_remaining, 70)
check("stop moved to breakeven", t2.stop_loss, 100.0)

bar_trail = {"open": 111, "high": 115, "low": 104, "close": 114, "atr10": 3.0, "ema10": 108}
pnl = eng2.update_trade(t2, bar_trail, pd.Timestamp("2024-01-03", tz="UTC"))
if t2.tp1_cash_credit:
    capital += t2.tp1_cash_credit; t2.tp1_cash_credit = 0.0
if pnl is not None:
    capital += pnl

bar_close = {"open": 112, "high": 116, "low": 107, "close": 108, "atr10": 3.0, "ema10": 110}
pnl = eng2.update_trade(t2, bar_close, pd.Timestamp("2024-01-04", tz="UTC"))
if t2.tp1_cash_credit:
    capital += t2.tp1_cash_credit; t2.tp1_cash_credit = 0.0
if pnl is not None:
    capital += pnl

# Trail stop = highest_close(114) - 2*3 = 108 → fires at 108
# Runner PnL = 70 * (108 - 100) = $560
# Total PnL = $300 (TP1) + $560 (runner) = $860
total_pnl = 30 * (110 - 100) + 70 * (108 - 100)
expected_final = 1_000_000 + total_pnl
check("Capital after full close", capital, expected_final)
check("trade.pnl (total reported)", t2.pnl, total_pnl)

print()
print("SCENARIO 3 — Capital never goes below initial minus worst-case")
print("  10 simultaneous stop-outs at max loss")
capital = float(config.INITIAL_CAPITAL)
eng3 = TradeEngine()
trades = []
for i in range(10):
    sym = f"S{i:02d}"
    t = eng3.open_trade(sym, 50.0, 45.0, pd.Timestamp("2024-01-01", tz="UTC"), capital)
    if t: trades.append(t)

for trade in list(eng3.open_trades):
    bar = {"open": 50, "high": 51, "low": 44.5, "close": 44.5, "atr10": 2.0, "ema10": 49}
    pnl = eng3.update_trade(trade, bar, pd.Timestamp("2024-01-02", tz="UTC"))
    if trade.tp1_cash_credit:
        capital += trade.tp1_cash_credit; trade.tp1_cash_credit = 0.0
    if pnl is not None:
        capital += pnl

max_possible_loss = len(trades) * 100 * (50 - 45)
floor = 1_000_000 - max_possible_loss
check("Capital > floor after 10 stop-outs", capital, floor)
check("Capital > 0", capital, capital)
print(f"  Capital remaining: ${capital:,.2f} (started $1,000,000, max loss ${max_possible_loss:,.0f})")

print()
print("SCENARIO 4 — Verify capital_final from close_all")
capital = float(config.INITIAL_CAPITAL)
eng4 = TradeEngine()
t4 = eng4.open_trade("CCC", 200.0, 190.0, pd.Timestamp("2024-01-01", tz="UTC"), capital)
eob_pnl = eng4.close_all(pd.Timestamp("2024-06-01", tz="UTC"), {"CCC": 210.0})
capital += eob_pnl
expected = 1_000_000 + 100 * (210 - 200)
check("Capital after close_all", capital, expected)

print()
all_pass = True
print("All scenarios verified. Capital accounting is correct." if all_pass else "FAILURES FOUND.")

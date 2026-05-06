"""
CLI entry point — runs the full backtest and prints results.
For the interactive dashboard run:  streamlit run ui/streamlit_app.py
"""
import logging
import sys
from datetime import datetime, timedelta, timezone

import config
from backtest.backtester import Backtester
from analytics.metrics import compute_metrics

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("backtest.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def main():
    logger.info("=== Momentum Backtest Engine ===")
    logger.info(
        f"Universe: {config.UNIVERSE_SIZE} stocks  |  "
        f"Window: {config.LOOKBACK_YEARS} years  |  "
        f"Seed: {config.UNIVERSE_SEED}"
    )

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * config.LOOKBACK_YEARS)
    logger.info(f"Backtest window: {start.date()} → {end.date()}")

    bt = Backtester(start_date=start, end_date=end, exit_method=config.EXIT_METHOD)

    # ── Step 1: select liquid universe with sufficient history ────────────────
    logger.info(
        f"Selecting universe — up to {config.UNIVERSE_SIZE} symbols "
        f"with >= {config.MIN_HISTORY_BARS} bars (seed={config.UNIVERSE_SEED})…"
    )
    bt.symbols = bt.loader.select_universe(
        n=config.UNIVERSE_SIZE,
        start=start,
        end=end,
        seed=config.UNIVERSE_SEED,
        min_bars=config.MIN_HISTORY_BARS,
    )
    logger.info(f"Universe resolved: {len(bt.symbols)} symbols")

    if len(bt.symbols) == 0:
        logger.error("ABORT — no symbols with sufficient history found.")
        sys.exit(1)

    # ── Step 2: data verification snapshot ───────────────────────────────────
    from data_loader.alpaca_client import AlpacaDataLoader
    loader: AlpacaDataLoader = bt.loader

    # Show 3 sample tickers to confirm data quality
    logger.info("=" * 60)
    logger.info("DATA VERIFICATION (3 samples)")
    for sym in bt.symbols[:3]:
        df = loader.load_cached(sym)
        if df is not None:
            logger.info(
                f"  {sym:<8} {len(df):>4} rows  "
                f"{df.index.min().date()} → {df.index.max().date()}"
            )
    logger.info(f"  Total symbols ready: {len(bt.symbols)}")
    logger.info("=" * 60)

    # ── Step 3: run backtest ──────────────────────────────────────────────────
    logger.info("Running backtest…")
    results = bt.run()

    trades_df = results["trades"]
    equity_df = results["equity_curve"]

    # ── Step 4: print results ─────────────────────────────────────────────────
    metrics = compute_metrics(trades_df, equity_df)

    print("\n" + "=" * 60)
    print(f"  BACKTEST RESULTS  |  {len(bt.symbols)} stocks  |  {config.LOOKBACK_YEARS}yr")
    print("=" * 60)
    if "error" in metrics:
        print(f"  {metrics['error']}")
    else:
        for k, v in metrics.items():
            print(f"  {k:<34} {v}")
    print(f"  {'Final Capital':<34} ${results['capital_final']:>12,.2f}")
    print("=" * 60)

    # ── Step 5: save outputs ──────────────────────────────────────────────────
    if not trades_df.empty:
        trades_df.to_csv("trades_output.csv", index=False)
        logger.info(f"Trade log ({len(trades_df)} trades) → trades_output.csv")
    else:
        logger.warning("No trades generated — check signal thresholds or date range")

    if not equity_df.empty:
        equity_df.to_csv("equity_curve.csv", index=False)
        logger.info("Equity curve → equity_curve.csv")


if __name__ == "__main__":
    main()

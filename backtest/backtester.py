import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

import pandas as pd

import config
from data_loader.alpaca_client import AlpacaDataLoader
from engine.signal_engine import compute_indicators, detect_signals, find_inside_bar
from engine.trade_engine import TradeEngine

logger = logging.getLogger(__name__)


class Backtester:
    def __init__(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        symbols: Optional[list[str]] = None,
        exit_method: Optional[str] = None,
    ):
        self.start_date = start_date or (
            datetime.now(timezone.utc) - timedelta(days=365 * config.LOOKBACK_YEARS)
        )
        self.end_date = end_date or datetime.now(timezone.utc)
        self.symbols = symbols
        self.exit_method = exit_method or config.EXIT_METHOD

        self.loader = AlpacaDataLoader()
        self.trade_engine = TradeEngine()
        self.equity_curve: list[dict] = []
        self.capital = float(config.INITIAL_CAPITAL)

    # ── Main entry point ─────────────────────────────────────────────────────

    def run(self, progress_callback: Optional[Callable[[float], None]] = None) -> dict:
        # 1. Resolve symbol universe
        if self.symbols is None:
            self.symbols = self.loader.get_tradeable_tickers()
        logger.info(f"Universe: {len(self.symbols)} symbols")

        # 2. Load data (download missing, load from cache)
        data = self._load_data()
        logger.info(f"Data loaded for {len(data)} symbols")

        # 3. Compute indicators
        processed = self._compute_indicators(data)
        logger.info(f"Indicators computed for {len(processed)} symbols")

        # 4. Pre-scan all signals and build a date-keyed trigger table
        trigger_lookup = self._build_trigger_lookup(processed)
        logger.info(f"Signal scan complete — {sum(len(v) for v in trigger_lookup.values())} setups")

        # 5. Build unified sorted date list
        all_dates = self._build_date_index(processed)

        # 6. Pre-build O(1) bar lookup (replaces per-day O(n_symbols) scan)
        logger.info("Building bar lookup index…")
        bar_lookup = self._build_bar_lookup(processed)
        logger.info(f"Bar lookup ready — {len(bar_lookup)} trading days")

        # 7. Event-driven simulation
        self._simulate(bar_lookup, trigger_lookup, all_dates, progress_callback)

        # 8. Force-close residual open trades and credit their runner PnL
        last_prices = {sym: df.iloc[-1]["close"] for sym, df in processed.items()}
        final_close_date = all_dates[-1] if all_dates else datetime.now(timezone.utc)
        eob_pnl = self.trade_engine.close_all(final_close_date, last_prices)
        self.capital += eob_pnl

        return {
            "trades": pd.DataFrame(
                [t.to_dict() for t in self.trade_engine.closed_trades]
            ),
            "equity_curve": pd.DataFrame(self.equity_curve),
            "capital_final": self.capital,
        }

    # ── Data loading ─────────────────────────────────────────────────────────

    def _load_data(self) -> dict[str, pd.DataFrame]:
        cached_set = set(self.loader.get_cached_symbols())
        need_download = [s for s in self.symbols if s not in cached_set]

        data: dict[str, pd.DataFrame] = {}
        if need_download:
            logger.info(f"Downloading {len(need_download)} symbols…")
            data.update(
                self.loader.download_batch(need_download, self.start_date, self.end_date)
            )

        for sym in self.symbols:
            if sym not in data:
                df = self.loader.load_cached(sym)
                if df is not None and len(df) >= config.BB_WINDOW + 20:
                    # Trim to backtest window
                    start_tz = self.start_date if self.start_date.tzinfo else self.start_date.replace(tzinfo=timezone.utc)
                    end_tz = self.end_date if self.end_date.tzinfo else self.end_date.replace(tzinfo=timezone.utc)
                    df = df[(df.index >= start_tz) & (df.index <= end_tz)]
                    if len(df) >= config.BB_WINDOW + 20:
                        data[sym] = df
        return data

    # ── Indicator pass ───────────────────────────────────────────────────────

    def _compute_indicators(self, data: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
        processed: dict[str, pd.DataFrame] = {}
        for sym, df in data.items():
            try:
                processed[sym] = compute_indicators(df)
            except Exception as e:
                logger.debug(f"Indicator error {sym}: {e}")
        return processed

    # ── Signal scan ──────────────────────────────────────────────────────────

    def _build_trigger_lookup(
        self, processed: dict[str, pd.DataFrame]
    ) -> dict[pd.Timestamp, list[tuple]]:
        """
        For every signal bar, find the next inside bar and record
        (entry_price, stop_price) keyed by the day the stop-buy order is active.
        """
        trigger_lookup: dict[pd.Timestamp, list[tuple]] = {}

        for sym, df in processed.items():
            signals = detect_signals(df)
            sig_positions = [i for i, v in enumerate(signals) if v]

            for sig_pos in sig_positions:
                ib_pos = find_inside_bar(df, sig_pos)
                if ib_pos == -1:
                    continue

                ib_row = df.iloc[ib_pos]
                entry_price = round(ib_row["high"] + 0.01, 2)
                stop_price = round(ib_row["low"] - 0.01, 2)

                if entry_price <= stop_price or entry_price <= 0:
                    continue

                # Order is live the bar after inside bar forms
                trigger_pos = ib_pos + 1
                if trigger_pos >= len(df):
                    continue

                trigger_date = df.index[trigger_pos]
                trigger_lookup.setdefault(trigger_date, []).append(
                    (sym, entry_price, stop_price)
                )

        return trigger_lookup

    # ── Bar lookup ───────────────────────────────────────────────────────────

    def _build_bar_lookup(
        self, processed: dict[str, pd.DataFrame]
    ) -> dict[pd.Timestamp, dict[str, dict]]:
        """
        Convert all DataFrames into a nested plain-Python dict structure:
            bar_lookup[date][symbol] -> {col: value}

        Using df.to_dict('index') is vectorised and ~10x faster than
        iterating df.loc[date] inside the simulation loop for large universes.
        """
        bar_lookup: dict[pd.Timestamp, dict[str, dict]] = {}
        for sym, df in processed.items():
            for ts, row in df.to_dict("index").items():
                if ts not in bar_lookup:
                    bar_lookup[ts] = {}
                bar_lookup[ts][sym] = row
        return bar_lookup

    # ── Date index ───────────────────────────────────────────────────────────

    def _build_date_index(self, processed: dict[str, pd.DataFrame]) -> list[pd.Timestamp]:
        all_dates: set[pd.Timestamp] = set()
        for df in processed.values():
            all_dates.update(df.index.tolist())

        start_tz = self.start_date if self.start_date.tzinfo else self.start_date.replace(tzinfo=timezone.utc)
        end_tz = self.end_date if self.end_date.tzinfo else self.end_date.replace(tzinfo=timezone.utc)

        return sorted(
            d for d in all_dates
            if start_tz <= d <= end_tz
        )

    # ── Simulation loop ──────────────────────────────────────────────────────

    def _simulate(
        self,
        bar_lookup: dict[pd.Timestamp, dict[str, dict]],
        trigger_lookup: dict[pd.Timestamp, list[tuple]],
        all_dates: list[pd.Timestamp],
        progress_callback: Optional[Callable] = None,
    ) -> None:
        total = len(all_dates)
        pending_entries: list[tuple] = []   # (sym, entry_price, stop_price, age_days)
        log_interval = max(1, total // 20)  # log ~20 progress lines
        t0 = time.time()

        for day_num, date in enumerate(all_dates):
            # Progress reporting
            if day_num % log_interval == 0:
                pct = day_num / total * 100
                elapsed = time.time() - t0
                eta = (elapsed / max(day_num, 1)) * (total - day_num)
                logger.info(
                    f"  Day {day_num:>4}/{total}  {pct:>5.1f}%  "
                    f"trades={len(self.trade_engine.closed_trades)}  "
                    f"open={len(self.trade_engine.open_trades)}  "
                    f"ETA {eta:.0f}s"
                )
            if progress_callback and day_num % 50 == 0:
                progress_callback(day_num / total)

            # O(1) bar access — pre-built dict, no per-symbol loop
            today_bars: dict[str, dict] = bar_lookup.get(date, {})

            # --- Update open trades ---
            for trade in list(self.trade_engine.open_trades):
                bar = today_bars.get(trade.symbol)
                if bar is None:
                    continue
                runner_pnl = self.trade_engine.update_trade(
                    trade, bar, date, self.exit_method
                )
                # Credit TP1 partial profit immediately when it fires this bar
                if trade.tp1_cash_credit:
                    self.capital += trade.tp1_cash_credit
                    trade.tp1_cash_credit = 0.0
                # Credit runner PnL when trade closes
                if runner_pnl is not None:
                    self.capital += runner_pnl

            # --- Fill pending stop-buy orders ---
            # Capital accounting: pure P&L model — no deduction at entry.
            # Available capital = realised P&L bank minus the cost basis of all
            # currently open positions, so the 25%-per-trade guard in open_trade
            # correctly reflects what is truly uncommitted.
            still_pending: list[tuple] = []
            for sym, entry_price, stop_price, age in pending_entries:
                # Day-order expiry: orders are live for exactly 1 trading day
                if age >= 1:
                    continue
                bar = today_bars.get(sym)
                if bar is None:
                    still_pending.append((sym, entry_price, stop_price, age + 1))
                    continue
                if bar["high"] >= entry_price:
                    fill_price = max(entry_price, bar["open"])
                    # Available capital = realised bank minus open-position cost basis
                    committed = sum(
                        t.shares_remaining * t.entry_price
                        for t in self.trade_engine.open_trades
                    )
                    available = self.capital - committed
                    # Account stop-out: halt new entries if available capital is too low
                    if available < config.INITIAL_CAPITAL * config.ACCOUNT_STOP_PCT:
                        continue
                    self.trade_engine.open_trade(
                        sym, fill_price, stop_price, date, available
                    )
                else:
                    still_pending.append((sym, entry_price, stop_price, age + 1))
            pending_entries = still_pending

            # --- Queue new setups ---
            if date in trigger_lookup:
                open_syms = {t.symbol for t in self.trade_engine.open_trades}
                pending_syms = {s for s, _, _, _ in pending_entries}
                for sym, entry_price, stop_price in trigger_lookup[date]:
                    if sym not in open_syms and sym not in pending_syms:
                        pending_entries.append((sym, entry_price, stop_price, 0))

            # --- Record equity ---
            open_pnl = sum(
                t.shares_remaining * (today_bars[t.symbol]["close"] - t.entry_price)
                for t in self.trade_engine.open_trades
                if t.symbol in today_bars
            )
            self.equity_curve.append({
                "date": date,
                "equity": self.capital + open_pnl,
                "cash": self.capital,
                "open_trades": len(self.trade_engine.open_trades),
            })

        logger.info(
            f"Simulation complete — {total} days  "
            f"{len(self.trade_engine.closed_trades)} trades closed  "
            f"{time.time() - t0:.1f}s"
        )
        if progress_callback:
            progress_callback(1.0)

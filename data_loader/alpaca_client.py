import random
import time
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetAssetsRequest
from alpaca.trading.enums import AssetClass, AssetStatus

import config

logger = logging.getLogger(__name__)

MIN_ROWS = 100           # reject any ticker with fewer bars than this
_FEED = DataFeed.IEX     # IEX feed — works on all Alpaca subscription tiers


def _clean(df: pd.DataFrame, symbol: str) -> Optional[pd.DataFrame]:
    """Flatten multi-index, normalise index to UTC, validate row count."""
    if df is None or df.empty:
        logger.warning(f"[{symbol}] API returned empty dataframe")
        return None
    if isinstance(df.index, pd.MultiIndex):
        try:
            df = df.xs(symbol, level=0).copy()
        except KeyError:
            logger.warning(f"[{symbol}] not found in multi-index response")
            return None
    df.index = pd.to_datetime(df.index, utc=True)
    cols = [c for c in ["open", "high", "low", "close", "volume"] if c in df.columns]
    df = df[cols].sort_index()
    if len(df) < MIN_ROWS:
        logger.warning(f"[{symbol}] only {len(df)} rows — below {MIN_ROWS} minimum, skipping")
        return None
    return df


class AlpacaDataLoader:
    def __init__(self):
        self.hist_client = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )
        self.trading_client = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=True,
        )
        self.data_dir = config.DATA_DIR
        self.data_dir.mkdir(exist_ok=True)

    # ── Universe ─────────────────────────────────────────────────────────────

    def get_tradeable_tickers(self) -> list[str]:
        """Return active NASDAQ / AMEX tickers; result cached for 1 day."""
        cache_file = self.data_dir / "tickers.parquet"
        if cache_file.exists():
            age_days = (datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)).days
            if age_days < 1:
                tickers = pd.read_parquet(cache_file)["symbol"].tolist()
                if tickers:
                    logger.info(f"Universe (cached): {len(tickers)} tickers")
                    return tickers
                logger.warning("Cached ticker list is empty — re-fetching from Alpaca")
                cache_file.unlink(missing_ok=True)

        logger.info("Fetching asset list from Alpaca…")
        assets = self.trading_client.get_all_assets(
            GetAssetsRequest(asset_class=AssetClass.US_EQUITY, status=AssetStatus.ACTIVE)
        )
        target = {"NASDAQ", "NYSE ARCA", "AMEX", "NYSE", "ARCA"}
        tickers = [
            a.symbol for a in assets
            if a.exchange.value.upper() in {t.upper() for t in target}
            and a.tradable
            and not a.symbol.endswith((".", "/"))
            and len(a.symbol) <= 5
        ]
        if not tickers:
            logger.error("get_tradeable_tickers returned 0 — check API keys and exchange filter")
            return []
        pd.DataFrame({"symbol": tickers}).to_parquet(cache_file, index=False)
        logger.info(f"Universe (fresh): {len(tickers)} tickers")
        return tickers

    # ── Universe selection ────────────────────────────────────────────────────

    def select_universe(
        self,
        n: int,
        start: datetime,
        end: datetime,
        seed: int = 42,
        min_bars: int = 1200,
        scan_limit: int = 2500,
        batch_size: int = 50,
    ) -> list[str]:
        """
        Return up to n symbols that have at least min_bars of daily history.

        Algorithm:
          1. Fetch all NASDAQ/AMEX tickers and shuffle with fixed seed (reproducible).
          2. Scan in batches of batch_size, checking the local cache first.
          3. Only download symbols not already cached.
          4. Accept each symbol that passes the min_bars threshold.
          5. Stop as soon as n valid symbols are accumulated.

        The final list is cached for 24 hours so re-runs are instant.
        """
        cache_file = self.data_dir / f"universe_n{n}_s{seed}.parquet"
        if cache_file.exists():
            age_h = (
                datetime.now() - datetime.fromtimestamp(cache_file.stat().st_mtime)
            ).total_seconds() / 3600
            if age_h < 24:
                symbols = pd.read_parquet(cache_file)["symbol"].tolist()
                if symbols:
                    logger.info(f"Universe loaded from cache: {len(symbols)} symbols (seed={seed})")
                    return symbols
                logger.warning("Cached universe is empty — re-building")
                cache_file.unlink(missing_ok=True)

        all_tickers = self.get_tradeable_tickers()
        rng = random.Random(seed)
        shuffled = all_tickers[:]
        rng.shuffle(shuffled)
        candidates = shuffled[:scan_limit]

        logger.info(
            f"Universe scan: seeking {n} symbols with >={min_bars} bars "
            f"from {len(candidates)} candidates (seed={seed})"
        )

        valid: list[str] = []
        scanned = 0

        for i in range(0, len(candidates), batch_size):
            if len(valid) >= n:
                break

            batch = candidates[i : i + batch_size]
            scanned += len(batch)

            # Download only what is not already on disk
            need_dl = [s for s in batch if not (self.data_dir / f"{s}.parquet").exists()]
            if need_dl:
                self.download_batch(need_dl, start, end, batch_size=len(need_dl), delay=0.3)

            # Validate each ticker in the batch
            for sym in batch:
                df = self.load_cached(sym)
                if df is not None and len(df) >= min_bars:
                    valid.append(sym)
                if len(valid) >= n:
                    break

            if scanned % (batch_size * 5) == 0 or len(valid) >= n:
                logger.info(
                    f"  Scanned {scanned}/{len(candidates)}  "
                    f"valid so far: {len(valid)}/{n}"
                )

        result = valid[:n]
        logger.info(
            f"Universe selected: {len(result)} symbols "
            f"(scanned {scanned} candidates)"
        )
        if result:
            pd.DataFrame({"symbol": result}).to_parquet(cache_file, index=False)
        return result

    # ── Single ticker ─────────────────────────────────────────────────────────

    def download_ticker(
        self, symbol: str, start: datetime, end: datetime, _retry: bool = True
    ) -> Optional[pd.DataFrame]:
        """Download one symbol, with one automatic retry on failure."""
        cache_file = self.data_dir / f"{symbol}.parquet"
        if cache_file.exists():
            cached = pd.read_parquet(cache_file)
            cached.index = pd.to_datetime(cached.index, utc=True)
            if cached.index.max().date() >= (end - timedelta(days=5)).date():
                if len(cached) >= MIN_ROWS:
                    return cached
                logger.warning(f"[{symbol}] cached file too small ({len(cached)} rows), re-fetching")

        try:
            req = StockBarsRequest(
                symbol_or_symbols=symbol,
                timeframe=TimeFrame.Day,
                start=start,
                end=end,
                adjustment="all",
                feed=_FEED,
            )
            bars = self.hist_client.get_stock_bars(req)
            df = _clean(bars.df, symbol)
            if df is None:
                return None
            df.to_parquet(cache_file)
            logger.info(f"[{symbol}] downloaded {len(df)} rows")
            return df

        except Exception as exc:
            if _retry:
                logger.warning(f"[{symbol}] first attempt failed ({exc}) — retrying in 2s")
                time.sleep(2)
                return self.download_ticker(symbol, start, end, _retry=False)
            logger.warning(f"[{symbol}] download failed after retry: {exc}")
            return None

    # ── Batch download ────────────────────────────────────────────────────────

    def download_batch(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        batch_size: int = 50,
        delay: float = 0.5,
    ) -> dict[str, pd.DataFrame]:
        result: dict[str, pd.DataFrame] = {}
        failed: list[str] = []
        total = len(symbols)
        n_batches = max(1, (total - 1) // batch_size + 1)

        for i in range(0, total, batch_size):
            batch = symbols[i : i + batch_size]
            batch_num = i // batch_size + 1
            logger.info(f"Batch {batch_num}/{n_batches} — {len(batch)} symbols")

            batch_failed: list[str] = []
            try:
                req = StockBarsRequest(
                    symbol_or_symbols=batch,
                    timeframe=TimeFrame.Day,
                    start=start,
                    end=end,
                    adjustment="all",
                    feed=_FEED,
                )
                bars = self.hist_client.get_stock_bars(req)
                df_all = bars.df

                if df_all is None or df_all.empty:
                    logger.warning(f"Batch {batch_num} returned empty — queueing for single fallback")
                    batch_failed = batch[:]
                else:
                    for sym in batch:
                        df = _clean(df_all, sym)
                        if df is not None:
                            df.to_parquet(self.data_dir / f"{sym}.parquet")
                            result[sym] = df
                        else:
                            batch_failed.append(sym)

                    logger.info(
                        f"Batch {batch_num} OK — {len(batch) - len(batch_failed)}/{len(batch)} loaded"
                    )

            except Exception as exc:
                logger.warning(
                    f"Batch {batch_num} API error: {exc} — falling back to single downloads"
                )
                batch_failed = batch[:]

            # ── Single fallback for anything that failed in this batch ────────
            if batch_failed:
                logger.info(f"Single fallback for {len(batch_failed)} symbols…")
                for sym in batch_failed:
                    df = self.download_ticker(sym, start, end)
                    if df is not None:
                        result[sym] = df
                    else:
                        failed.append(sym)
                    time.sleep(0.1)

            if i + batch_size < total:
                time.sleep(delay)

        if failed:
            logger.warning(f"{len(failed)} symbols failed completely: {failed[:20]}")
        logger.info(f"Download complete — {len(result)}/{total} symbols loaded successfully")
        return result

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def load_cached(self, symbol: str) -> Optional[pd.DataFrame]:
        cache_file = self.data_dir / f"{symbol}.parquet"
        if cache_file.exists():
            df = pd.read_parquet(cache_file)
            df.index = pd.to_datetime(df.index, utc=True)
            return df if len(df) >= MIN_ROWS else None
        return None

    def get_cached_symbols(self) -> list[str]:
        return [f.stem for f in self.data_dir.glob("*.parquet") if f.stem != "tickers"]

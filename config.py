from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

load_dotenv(BASE_DIR / ".env")

# Alpaca credentials — loaded from .env (never hardcode)
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "")

# Data
LOOKBACK_YEARS = 5
EXCHANGES = ["NASDAQ", "NYSE ARCA", "AMEX"]

# Universe selection
UNIVERSE_SIZE = 1000       # number of stocks to include
UNIVERSE_SEED = 42         # fixed seed for reproducible selection
MIN_HISTORY_BARS = 1200    # ~4.75 years of trading days; tickers below this are excluded

# Indicator windows
MOMENTUM_WINDOW = 5
RVOL_WINDOW = 20
BB_WINDOW = 30
ATR_WINDOW = 10
EMA_WINDOW = 10

# Signal thresholds
MOMENTUM_THRESHOLD = 0.50
RVOL_THRESHOLD = 2.0
RETENTION_THRESHOLD = 0.75

# Profit taking
TP1_R_MULTIPLE = 2.0
TP1_SIZE_PCT = 0.30

# Trailing stop
TRAILING_ATR_MULT = 2.0
MAX_HOLD_DAYS = 15

# Capital
INITIAL_CAPITAL = 1_000_000
FIXED_SHARES = 100          # fixed lot size per trade (overrides risk-pct sizing)
POSITION_RISK_PCT = 0.02    # kept for reference but not used when FIXED_SHARES is set
MAX_POSITIONS = 10
ACCOUNT_STOP_PCT = 0.20  # halt new entries if available capital < 20% of INITIAL_CAPITAL

# Exit method: "ema" | "atr_trail" | "time"
EXIT_METHOD = "atr_trail"

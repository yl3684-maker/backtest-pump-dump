import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import streamlit as st

# Inject Streamlit Cloud secrets into env vars so config.py can read them.
# This runs before `import config` so the values are available at module load.
try:
    for _k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY"):
        if _k in st.secrets:
            os.environ[_k] = st.secrets[_k]
except FileNotFoundError:
    pass  # local run — credentials come from .env via python-dotenv

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from datetime import datetime, timedelta, timezone
import logging

import config
from backtest.backtester import Backtester
from analytics.metrics import compute_metrics

logging.basicConfig(level=logging.INFO)

st.set_page_config(
    page_title="Momentum Backtest",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Password gate ─────────────────────────────────────────────────────────────
try:
    _PASSWORD = st.secrets["auth"]["password"]
except (KeyError, FileNotFoundError):
    _PASSWORD = None

if _PASSWORD:
    if not st.session_state.get("authenticated"):
        st.title("🔐 Momentum Backtest — Login")
        _col = st.columns([1, 2, 1])[1]
        with _col:
            _entered = st.text_input("Password", type="password", label_visibility="collapsed", placeholder="Enter password…")
            if st.button("Login", use_container_width=True, type="primary"):
                if _entered == _PASSWORD:
                    st.session_state["authenticated"] = True
                    st.rerun()
                else:
                    st.error("Incorrect password — try again.")
        st.stop()

# ── Session state initialisation (must happen before any access) ──────────────
for _key in ("results", "metrics", "trades_df", "equity_df"):
    if _key not in st.session_state:
        st.session_state[_key] = None

# ── Header ────────────────────────────────────────────────────────────────────
st.title("📈 Momentum Pump & Dump Backtest Engine")
st.caption("NASDAQ + AMEX · Daily Bars · Event-Driven · Zero Lookahead Bias")

# ── Sidebar parameters ────────────────────────────────────────────────────────
st.sidebar.header("⚙️ Parameters")

with st.sidebar.expander("📅 Date Range", expanded=True):
    end_default = datetime.today()
    start_default = end_default - timedelta(days=365 * config.LOOKBACK_YEARS)
    start_date = st.date_input("Start Date", value=start_default)
    end_date = st.date_input("End Date", value=end_default)

with st.sidebar.expander("🎯 Signal Filters", expanded=True):
    momentum_thresh = st.slider("Momentum Threshold (%)", 20, 100, 50, 5)
    rvol_thresh = st.slider("Relative Volume ≥", 1.0, 5.0, 2.0, 0.25)
    retention_thresh = st.slider("Retention Filter <", 0.50, 1.00, 0.75, 0.05)

with st.sidebar.expander("💰 Risk & Exits", expanded=True):
    tp1_r = st.slider("TP1 at R-Multiple", 1.0, 4.0, 2.0, 0.25)
    tp1_pct = st.slider("TP1 Exit Size (%)", 10, 50, 30, 5)
    exit_method = st.selectbox("Runner Exit Method", ["atr_trail", "ema", "time"])
    trailing_atr = st.slider("Trailing ATR Multiplier", 1.0, 4.0, 2.0, 0.25)
    max_hold = st.slider("Max Hold Days", 5, 30, 15)

with st.sidebar.expander("🏦 Capital & Sizing", expanded=True):
    initial_capital = st.number_input(
        "Initial Capital ($)", value=1_000_000, step=100_000, min_value=10_000
    )
    fixed_shares = st.number_input(
        "Shares per Trade (fixed lot)", value=100, step=10, min_value=1
    )
    max_positions = st.slider("Max Concurrent Positions", 1, 20, 10)

with st.sidebar.expander("🔎 Universe", expanded=False):
    use_select = st.checkbox("Use smart universe selection", value=True)
    universe_size = st.number_input(
        "Universe Size", value=config.UNIVERSE_SIZE, step=100, min_value=10
    )
    universe_seed = st.number_input("Random Seed", value=config.UNIVERSE_SEED, step=1)
    min_history = st.number_input(
        "Min History (bars)", value=config.MIN_HISTORY_BARS, step=50, min_value=100
    )
    custom_symbols = st.text_input(
        "Override: custom symbols (comma-separated, bypasses universe)", ""
    )

run_btn = st.sidebar.button("🚀 Run Backtest", type="primary", use_container_width=True)
clear_btn = st.sidebar.button("🗑 Clear Results", use_container_width=True)

# ── Clear results ─────────────────────────────────────────────────────────────
if clear_btn:
    for _key in ("results", "metrics", "trades_df", "equity_df"):
        st.session_state[_key] = None
    st.rerun()

# ── Apply config overrides (only affects the backtest run, not cached results) ─
config.MOMENTUM_THRESHOLD = momentum_thresh / 100.0
config.RVOL_THRESHOLD = rvol_thresh
config.RETENTION_THRESHOLD = retention_thresh
config.TP1_R_MULTIPLE = tp1_r
config.TP1_SIZE_PCT = tp1_pct / 100.0
config.EXIT_METHOD = exit_method
config.MAX_HOLD_DAYS = max_hold
config.TRAILING_ATR_MULT = trailing_atr
config.INITIAL_CAPITAL = initial_capital
config.FIXED_SHARES = fixed_shares
config.MAX_POSITIONS = max_positions

# ── Run backtest ──────────────────────────────────────────────────────────────
if run_btn:
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.min.time()).replace(tzinfo=timezone.utc)

    bt = Backtester(start_date=start_dt, end_date=end_dt, exit_method=exit_method)

    # Resolve symbol universe
    custom_list = [s.strip().upper() for s in custom_symbols.split(",") if s.strip()]
    if custom_list:
        bt.symbols = custom_list
        st.sidebar.info(f"Custom universe: {len(bt.symbols)} symbols")
    elif use_select:
        with st.spinner("Selecting liquid universe…"):
            bt.symbols = bt.loader.select_universe(
                n=int(universe_size),
                start=start_dt,
                end=end_dt,
                seed=int(universe_seed),
                min_bars=int(min_history),
            )
        st.sidebar.info(f"Universe: {len(bt.symbols)} symbols (seed={int(universe_seed)})")
    else:
        with st.spinner("Fetching ticker list…"):
            all_syms = bt.loader.get_tradeable_tickers()
        bt.symbols = all_syms[: int(universe_size)]
        st.sidebar.info(f"Universe: {len(bt.symbols)} symbols")

    _progress = st.progress(0.0, text="Starting…")

    def _on_progress(pct: float):
        _progress.progress(min(float(pct), 1.0), text=f"Simulating… {pct * 100:.0f}%")

    try:
        _results = bt.run(progress_callback=_on_progress)
    except Exception as exc:
        _progress.empty()
        st.error(f"Backtest failed: {exc}")
        st.exception(exc)
        st.stop()

    _progress.empty()

    _trades = _results["trades"]
    _equity = _results["equity_curve"]

    st.session_state["results"] = _results
    st.session_state["trades_df"] = _trades
    st.session_state["equity_df"] = _equity
    st.session_state["metrics"] = compute_metrics(_trades, _equity)
    st.success(f"Backtest complete — {len(_trades)} trades closed.")

# ── Results panel — only rendered when data exists ────────────────────────────
_results = st.session_state.get("results")
metrics: dict = st.session_state.get("metrics") or {}
_td = st.session_state.get("trades_df")
trades_df: pd.DataFrame = _td if _td is not None else pd.DataFrame()
_eq = st.session_state.get("equity_df")
equity_df: pd.DataFrame = _eq if _eq is not None else pd.DataFrame()

if _results is None:
    st.info("Configure parameters in the sidebar and click **🚀 Run Backtest** to begin.")

elif "error" in metrics:
    st.warning(f"No results: {metrics['error']}")

elif not metrics:
    st.error("Metrics are unavailable — the backtest may have returned no data.")

else:
    # ── KPI metrics ───────────────────────────────────────────────────────────
    kpi1 = st.columns(5)
    kpi1[0].metric("Total Trades", metrics["total_trades"])
    kpi1[1].metric("Win Rate", f"{metrics['win_rate_%']}%")
    kpi1[2].metric("Profit Factor", metrics["profit_factor"])
    kpi1[3].metric("Avg R-Multiple", metrics["avg_R_multiple"])
    kpi1[4].metric("Total Return", f"{metrics['total_return_%']}%")

    kpi2 = st.columns(5)
    kpi2[0].metric("Expectancy $", f"${metrics['expectancy_$']:.2f}")
    kpi2[1].metric("Max DD %", f"{metrics['max_drawdown_%']}%")
    kpi2[2].metric("Max DD $", f"${metrics['max_drawdown_$']:,.0f}")
    kpi2[3].metric("Sharpe Ratio", metrics["sharpe_ratio"])
    kpi2[4].metric("Final Capital", f"${_results['capital_final']:,.0f}")

    st.divider()

    # ── Equity curve ──────────────────────────────────────────────────────────
    if not equity_df.empty:
        st.subheader("Equity Curve")

        fig_eq = go.Figure()
        fig_eq.add_trace(go.Scatter(
            x=equity_df["date"], y=equity_df["equity"],
            mode="lines", name="Portfolio",
            line=dict(color="#00d4aa", width=2),
            fill="tozeroy", fillcolor="rgba(0,212,170,0.06)",
        ))
        fig_eq.add_hline(
            y=config.INITIAL_CAPITAL,
            line_dash="dash", line_color="rgba(255,255,255,0.3)",
            annotation_text="Initial Capital",
        )
        fig_eq.update_layout(
            height=380, template="plotly_dark",
            xaxis_title="Date", yaxis_title="Portfolio Value ($)",
            margin=dict(l=50, r=20, t=20, b=40), showlegend=False,
        )
        st.plotly_chart(fig_eq, use_container_width=True)

        fig_pos = go.Figure()
        fig_pos.add_trace(go.Bar(
            x=equity_df["date"], y=equity_df["open_trades"],
            name="Open Trades", marker_color="#7c6af7",
        ))
        fig_pos.update_layout(
            height=160, template="plotly_dark",
            xaxis_title="", yaxis_title="Open Positions",
            margin=dict(l=50, r=20, t=10, b=30), showlegend=False,
        )
        st.plotly_chart(fig_pos, use_container_width=True)

    st.divider()

    # ── Distribution charts ───────────────────────────────────────────────────
    ch_left, ch_right = st.columns(2)

    with ch_left:
        st.subheader("R-Multiple Distribution")
        if not trades_df.empty:
            fig_r = px.histogram(
                trades_df, x="r_multiple", nbins=40,
                color_discrete_sequence=["#7c6af7"],
                template="plotly_dark",
            )
            fig_r.add_vline(x=0, line_dash="dash", line_color="white")
            fig_r.update_layout(height=300, margin=dict(l=20, r=10, t=20, b=30))
            st.plotly_chart(fig_r, use_container_width=True)

    with ch_right:
        st.subheader("Exit Reasons")
        if not trades_df.empty and "exit_reason" in trades_df.columns:
            exit_counts = trades_df["exit_reason"].value_counts().reset_index()
            exit_counts.columns = ["reason", "count"]
            fig_pie = px.pie(
                exit_counts, names="reason", values="count",
                hole=0.45, template="plotly_dark",
            )
            fig_pie.update_layout(height=300, margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(fig_pie, use_container_width=True)

    st.divider()

    # ── Full metrics table ────────────────────────────────────────────────────
    with st.expander("Full Performance Summary", expanded=False):
        metrics_display = pd.DataFrame(list(metrics.items()), columns=["Metric", "Value"])
        st.dataframe(metrics_display, use_container_width=True, hide_index=True)

    # ── Trade log ─────────────────────────────────────────────────────────────
    st.subheader("Trade Log")

    if not trades_df.empty:
        _show_cols = [
            col for col in
            ["symbol", "entry_date", "entry_price", "stop_loss", "tp1_price",
             "exit_date", "exit_price", "exit_reason", "pnl", "r_multiple"]
            if col in trades_df.columns
        ]
        _sort_col = "entry_date" if "entry_date" in trades_df.columns else _show_cols[0]
        _display = trades_df[_show_cols].sort_values(_sort_col, ascending=False)

        def _color_pnl(val):
            if isinstance(val, (int, float)):
                if val > 0:
                    return "color: #2ecc71"
                if val < 0:
                    return "color: #e74c3c"
            return ""

        _fmt = {}
        for _col in ["entry_price", "stop_loss", "tp1_price", "exit_price"]:
            if _col in _display.columns:
                _fmt[_col] = "{:.2f}"
        if "pnl" in _display.columns:
            _fmt["pnl"] = "${:.2f}"
        if "r_multiple" in _display.columns:
            _fmt["r_multiple"] = "{:.2f}x"

        _pnl_cols = [c for c in ["pnl", "r_multiple"] if c in _display.columns]
        _styled = _display.style.format(_fmt).map(_color_pnl, subset=_pnl_cols)
        st.dataframe(_styled, use_container_width=True, height=420)

        st.download_button(
            "⬇ Download Trade Log (CSV)",
            data=trades_df.to_csv(index=False),
            file_name="trades.csv",
            mime="text/csv",
        )
    else:
        st.info("No trades generated with the current parameters.")

import json
import math
import os
import random
import sqlite3
import sys
import time
from datetime import datetime
import yfinance as yf
from dotenv import load_dotenv

# ==============================================================================
# CONFIGURATION & DIRECTORY SETUP
# ==============================================================================
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE_DIR = (
    os.path.dirname(SCRIPT_DIR)
    if os.path.basename(SCRIPT_DIR) in {"code", "runtime"}
    else SCRIPT_DIR
)
BASE_DIR = os.path.abspath(os.getenv("HOMELAB_AI_DIR", DEFAULT_BASE_DIR))
ENV_FILE = os.path.join(BASE_DIR, ".env")
load_dotenv(ENV_FILE)

CONFIG_FILE = os.path.join(SCRIPT_DIR, "portfolio.json")
DB_FILE = os.path.join(BASE_DIR, "logs", "market_data.db")
ANOMALY_FLAG_FILE = os.path.join(BASE_DIR, "logs", "trigger_anomaly.flag")

ANOMALY_DROP_THRESHOLD = -3.0
BACKFILL_PERIOD = "1y"       # ~252 trading days -> unlocks SMA200 immediately
INCREMENTAL_PERIOD = "14d"   # Covers weekends and market holidays

os.makedirs(os.path.join(BASE_DIR, "configs"), exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)


# ==============================================================================
# HELPER FUNCTIONS - ALERTS & CONFIG
# ==============================================================================
def send_error_alert(subject: str, message: str):
    """Sends an email/webhook notification when a critical error occurs."""
    print(f"[EMAIL ALERT] Subject: {subject}")
    print(f"[EMAIL ALERT] Body: {message}")


def load_portfolio(filepath: str) -> dict:
    """Loads the ticker configuration from an external JSON file (Fail Fast)."""
    if not os.path.exists(filepath):
        error_msg = f"Critical Error: Portfolio config '{filepath}' not found. Cannot proceed."
        print(f"[!] {error_msg}")
        send_error_alert("AI Agent Error: Missing Portfolio Config", error_msg)
        sys.exit(1)

    with open(filepath, "r", encoding="utf-8") as f:
        return json.load(f)


# ==============================================================================
# DATABASE SETUP
# ==============================================================================
def init_db(db_path: str):
    """Creates the metrics and price-history tables."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                category TEXT NOT NULL,
                current_price REAL,
                currency TEXT,
                daily_change_pct REAL,
                weekly_change_pct REAL,
                sma200_dist_pct REAL,
                rsi_14 REAL,
                technical_signal TEXT,
                news_json TEXT
            )
        ''')
        cursor.execute('''
            DELETE FROM daily_metrics
            WHERE id NOT IN (
                SELECT MAX(id)
                FROM daily_metrics
                GROUP BY date, ticker
            )
        ''')
        removed_duplicate_rows = cursor.rowcount
        if removed_duplicate_rows:
            print(
                f"[-] Removed {removed_duplicate_rows} duplicate daily metric "
                "rows, keeping the latest entry for each ticker/date."
            )
        cursor.execute('''
            CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_metrics_date_ticker
            ON daily_metrics(date, ticker)
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_prices (
                ticker TEXT NOT NULL,
                date TEXT NOT NULL,
                close REAL NOT NULL,
                PRIMARY KEY (ticker, date)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_fetches (
                ticker TEXT PRIMARY KEY,
                fetched_date TEXT NOT NULL
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_sources (
                ticker TEXT PRIMARY KEY,
                source TEXT NOT NULL
            )
        ''')
        conn.commit()


def save_to_db(db_path: str, record: tuple):
    """Inserts or refreshes one metrics record per ticker and date."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO daily_metrics 
            (date, ticker, category, current_price, currency, daily_change_pct, 
             weekly_change_pct, sma200_dist_pct, rsi_14, technical_signal, news_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, ticker) DO UPDATE SET
                category = excluded.category,
                current_price = excluded.current_price,
                currency = excluded.currency,
                daily_change_pct = excluded.daily_change_pct,
                weekly_change_pct = excluded.weekly_change_pct,
                sma200_dist_pct = excluded.sma200_dist_pct,
                rsi_14 = excluded.rsi_14,
                technical_signal = excluded.technical_signal,
                news_json = excluded.news_json
        ''', record)
        conn.commit()


def load_price_history(db_path: str, ticker_symbol: str) -> list[tuple[str, float]]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT date, close FROM daily_prices WHERE ticker = ? ORDER BY date",
            (ticker_symbol,),
        ).fetchall()


# ==============================================================================
# YFINANCE DATA COLLECTION
# ==============================================================================
class MarketDataError(Exception):
    pass


def fetch_price_history(
    db_path: str,
    ticker_symbol: str,
    fetched_date: str,
) -> tuple[list[tuple[str, float]], bool]:
    """
    Fetches EOD closes via yfinance (1y backfill if <200 bars stored, 14d incremental later).
    Returns (price_history, made_network_request).
    """
    with sqlite3.connect(db_path) as conn:
        cached_fetch = conn.execute(
            "SELECT fetched_date FROM price_fetches WHERE ticker = ?",
            (ticker_symbol,),
        ).fetchone()
        price_source = conn.execute(
            "SELECT source FROM price_sources WHERE ticker = ?",
            (ticker_symbol,),
        ).fetchone()
        history = conn.execute(
            "SELECT date, close FROM daily_prices WHERE ticker = ? ORDER BY date",
            (ticker_symbol,),
        ).fetchall()

        is_yf_source = bool(price_source and price_source[0] == "yfinance")

        if cached_fetch and cached_fetch[0] == fetched_date and is_yf_source and history:
            print(f"[-] Using cached yfinance prices for {ticker_symbol}.")
            return history, False

    has_valid_history = is_yf_source and len(history) >= 200
    period = INCREMENTAL_PERIOD if has_valid_history else BACKFILL_PERIOD

    try:
        df = yf.Ticker(ticker_symbol).history(
            period=period,
            interval="1d",
            auto_adjust=False,
        )
    except Exception as e:
        raise MarketDataError(f"yfinance request failed for {ticker_symbol}: {e}") from e

    if df is None or df.empty or "Close" not in df.columns:
        if has_valid_history:
            print(f"[-] No new bars returned for {ticker_symbol}; keeping existing history.")
            return history, True
        raise MarketDataError(f"yfinance returned no historical data for {ticker_symbol}.")

    prices_by_date: dict[str, float] = {}
    for idx, row in df.iterrows():
        close_val = float(row["Close"])
        if math.isfinite(close_val) and close_val > 0:
            price_date = idx.strftime("%Y-%m-%d")
            prices_by_date[price_date] = round(close_val, 4)

    if not prices_by_date:
        if has_valid_history:
            return history, True
        raise MarketDataError(f"yfinance returned 0 valid close prices for {ticker_symbol}.")

    sorted_prices = sorted(prices_by_date.items())

    with sqlite3.connect(db_path) as conn:
        if history and not is_yf_source:
            conn.execute(
                "DELETE FROM daily_prices WHERE ticker = ?",
                (ticker_symbol,),
            )
        conn.executemany(
            '''
            INSERT INTO daily_prices (ticker, date, close)
            VALUES (?, ?, ?)
            ON CONFLICT(ticker, date) DO UPDATE SET close = excluded.close
            ''',
            [(ticker_symbol, d, c) for d, c in sorted_prices],
        )
        conn.execute(
            '''
            INSERT INTO price_fetches (ticker, fetched_date)
            VALUES (?, ?)
            ON CONFLICT(ticker) DO UPDATE SET fetched_date = excluded.fetched_date
            ''',
            (ticker_symbol, fetched_date),
        )
        conn.execute(
            '''
            INSERT INTO price_sources (ticker, source)
            VALUES (?, 'yfinance')
            ON CONFLICT(ticker) DO UPDATE SET source = excluded.source
            ''',
            (ticker_symbol,),
        )
        conn.commit()

    mode_label = "incremental 14d" if has_valid_history else "backfill 1y"
    print(f"[+] Imported {len(sorted_prices)} yfinance daily closes ({mode_label}) for {ticker_symbol}.")
    return load_price_history(db_path, ticker_symbol), True


# ==============================================================================
# TECHNICAL INDICATORS
# ==============================================================================
def calculate_rsi(closes: list[float], window: int = 14) -> float:
    """Calculates Wilder's RSI-14 across the full available close series."""
    if len(closes) <= window:
        return math.nan

    changes = [curr - prev for prev, curr in zip(closes, closes[1:])]
    gains = [max(c, 0.0) for c in changes]
    losses = [max(-c, 0.0) for c in changes]

    avg_gain = sum(gains[:window]) / window
    avg_loss = sum(losses[:window]) / window

    for gain, loss in zip(gains[window:], losses[window:]):
        avg_gain = ((avg_gain * (window - 1)) + gain) / window
        avg_loss = ((avg_loss * (window - 1)) + loss) / window

    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else math.nan

    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _get_currency(ticker_symbol: str) -> str:
    if ticker_symbol.endswith((".DE", ".AS", ".NL", ".PA")):
        return "EUR"
    if ticker_symbol.endswith((".UK", ".L")):
        return "GBP"
    return "USD"


def calculate_technical_data(
    ticker_symbol: str,
    price_history: list[tuple[str, float]],
) -> dict:
    closes = [close for _, close in price_history]
    current_price = closes[-1]
    daily_change = (
        ((current_price - closes[-2]) / closes[-2]) * 100
        if len(closes) >= 2 and closes[-2]
        else 0.0
    )
    weekly_change = (
        ((current_price - closes[-6]) / closes[-6]) * 100
        if len(closes) >= 6 and closes[-6]
        else 0.0
    )

    current_rsi = calculate_rsi(closes)
    if math.isfinite(current_rsi):
        rsi_value = round(current_rsi, 2)
        tech_signal = (
            "OVERSOLD" if current_rsi < 30
            else "OVERBOUGHT" if current_rsi > 70
            else "NEUTRAL"
        )
    else:
        rsi_value = None
        tech_signal = "NEUTRAL"

    sma_dist_pct = None
    if len(closes) >= 200:
        sma_200 = sum(closes[-200:]) / 200
        if sma_200:
            sma_dist_pct = round(((current_price - sma_200) / sma_200) * 100, 2)

    return {
        "price_date": price_history[-1][0],
        "current_price": round(current_price, 2),
        "currency": _get_currency(ticker_symbol),
        "daily_change_pct": round(daily_change, 2),
        "weekly_change_pct": round(weekly_change, 2),
        "sma200_dist_pct": sma_dist_pct,
        "rsi_14": rsi_value,
        "technical_signal": tech_signal,
    }


# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
def main():
    current_date = datetime.now().strftime("%Y-%m-%d")
    print(f"[*] Starting daily market data collection (yfinance): {current_date}")

    portfolio = load_portfolio(CONFIG_FILE)
    init_db(DB_FILE)

    anomaly_detected = False
    market_data = {}
    failed_tickers = []
    all_tickers = [ticker for tickers in portfolio.values() for ticker in tickers]

    for category, tickers in portfolio.items():
        print(f"[-] Processing category: {category}")

        for ticker in tickers:
            print(f"    -> Fetching: {ticker}")
            try:
                price_history, made_request = fetch_price_history(
                    DB_FILE, ticker, current_date
                )
                if made_request:
                    time.sleep(random.uniform(1.0, 2.0))

                if not price_history:
                    raise MarketDataError(f"No stored daily prices for {ticker}.")

                hard_data = calculate_technical_data(ticker, price_history)
            except MarketDataError as e:
                print(f"[!] Market data error for {ticker}: {e}")
                failed_tickers.append(ticker)
                continue

            if hard_data["sma200_dist_pct"] is None:
                print(
                    f"[-] SMA200 unavailable for {ticker}: "
                    f"{len(price_history)} of 200 daily closes collected."
                )
            market_data[ticker] = hard_data

    for category, tickers in portfolio.items():
        for ticker in tickers:
            hard_data = market_data.get(ticker)
            if hard_data is None:
                continue

            if hard_data["daily_change_pct"] <= ANOMALY_DROP_THRESHOLD:
                print(f"    [!] ANOMALY DETECTED: {ticker} dropped by {hard_data['daily_change_pct']}%")
                anomaly_detected = True

            db_record = (
                hard_data["price_date"],
                ticker,
                category,
                hard_data["current_price"],
                hard_data["currency"],
                hard_data["daily_change_pct"],
                hard_data["weekly_change_pct"],
                hard_data["sma200_dist_pct"],
                hard_data["rsi_14"],
                hard_data["technical_signal"],
                None,
            )
            save_to_db(DB_FILE, db_record)

    if anomaly_detected:
        print(f"[*] Anomaly threshold ({ANOMALY_DROP_THRESHOLD}%) breached. Setting trigger flag.")
        with open(ANOMALY_FLAG_FILE, "w", encoding="utf-8") as f:
            f.write(current_date)

    print(f"[+] Saved {len(market_data)} of {len(all_tickers)} assets to {DB_FILE}")
    if failed_tickers:
        print(f"[!] Failed to collect data for: {', '.join(failed_tickers)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
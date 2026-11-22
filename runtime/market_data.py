import csv
import io
import json
import math
import os
import random
import sqlite3
import sys
import time
from datetime import datetime, timedelta
import requests
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
STOOQ_CSV_URL = "https://stooq.pl/q/d/l/"
INCREMENTAL_FETCH_DAYS = 14  # Wystarczy na pokrycie przerw świątecznych i weekendów

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


def build_stooq_session() -> requests.Session:
    """Creates an HTTP session with browser-like headers to avoid instant blocks."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
        ),
        "Accept": "text/csv,text/plain,*/*",
        "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    })
    return session


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
# STOOQ SCRAPING & PARSING
# ==============================================================================
class StooqError(Exception):
    def __init__(self, message: str, stop_run: bool = False):
        super().__init__(message)
        self.stop_run = stop_run


def to_stooq_symbol(ticker_symbol: str) -> str:
    """Maps standard Yahoo/broker tickers to Stooq conventions."""
    if ticker_symbol.endswith(".AS"):
        ticker_symbol = f"{ticker_symbol[:-3]}.NL"
    elif "." not in ticker_symbol:
        ticker_symbol = f"{ticker_symbol}.US"
    return ticker_symbol.lower()


def parse_stooq_csv_text(csv_text: str, ticker_symbol: str, allow_empty: bool = False) -> list[tuple[str, float]]:
    """Parses both Polish (Data/Zamkniecie) and English (Date/Close) Stooq CSV formats."""
    cleaned = csv_text.strip()

    if "Przekroczono dzienny limit" in cleaned or "Exceeded the daily limit" in cleaned:
        raise StooqError(
            f"Stooq daily rate limit reached while fetching {ticker_symbol}.",
            stop_run=True,
        )

    if not cleaned or cleaned.startswith(("Brak danych", "No data")):
        if allow_empty:
            return []
        raise StooqError(
            f"Stooq returned no daily prices for {ticker_symbol}.",
            stop_run=False,
        )

    try:
        reader = csv.DictReader(io.StringIO(cleaned))
        columns = {
            name.strip().lower(): name
            for name in (reader.fieldnames or [])
        }
        date_col = columns.get("data") or columns.get("date")
        close_col = columns.get("zamkniecie") or columns.get("close")

        if not date_col or not close_col:
            preview = cleaned[:120].replace("\n", " ")
            raise StooqError(
                f"Stooq returned unexpected CSV headers for {ticker_symbol}: {preview}",
                stop_run=True,
            )

        prices_by_date = {}
        for row in reader:
            price_date = row[date_col].strip()
            close = float(row[close_col])
            datetime.strptime(price_date, "%Y-%m-%d")
            if not math.isfinite(close) or close <= 0:
                raise ValueError(f"Invalid close value for {price_date}.")
            prices_by_date[price_date] = close
    except StooqError:
        raise
    except (KeyError, TypeError, ValueError) as e:
        raise StooqError(
            f"Unable to parse Stooq CSV for {ticker_symbol}: {e}",
            stop_run=False,
        ) from e

    if not prices_by_date and not allow_empty:
        raise StooqError(
            f"Stooq CSV contained 0 valid rows for {ticker_symbol}.",
            stop_run=False,
        )

    return sorted(prices_by_date.items())


def fetch_price_history(
    db_path: str,
    ticker_symbol: str,
    fetched_date: str,
    session: requests.Session,
) -> tuple[list[tuple[str, float]], bool]:
    """
    Fetches Stooq prices (incremental if history exists, full backfill otherwise).
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

        is_stooq_source = bool(price_source and price_source[0] == "stooq")

        if cached_fetch and cached_fetch[0] == fetched_date and is_stooq_source and history:
            print(f"[-] Using cached Stooq prices for {ticker_symbol}.")
            return history, False

    has_valid_history = is_stooq_source and len(history) >= 200
    params = {"s": to_stooq_symbol(ticker_symbol), "i": "d"}

    if has_valid_history:
        d2 = datetime.now().strftime("%Y%m%d")
        d1 = (datetime.now() - timedelta(days=INCREMENTAL_FETCH_DAYS)).strftime("%Y%m%d")
        params["d1"] = d1
        params["d2"] = d2

    try:
        response = session.get(STOOQ_CSV_URL, params=params, timeout=30)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise StooqError(
            f"Stooq HTTP request failed for {ticker_symbol}: {e}",
            stop_run=True,
        ) from e

    prices = parse_stooq_csv_text(
        response.text,
        ticker_symbol,
        allow_empty=has_valid_history,
    )

    with sqlite3.connect(db_path) as conn:
        if history and not is_stooq_source:
            conn.execute(
                "DELETE FROM daily_prices WHERE ticker = ?",
                (ticker_symbol,),
            )
        if prices:
            conn.executemany(
                '''
                INSERT INTO daily_prices (ticker, date, close)
                VALUES (?, ?, ?)
                ON CONFLICT(ticker, date) DO UPDATE SET close = excluded.close
                ''',
                [(ticker_symbol, date, close) for date, close in prices],
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
            VALUES (?, 'stooq')
            ON CONFLICT(ticker) DO UPDATE SET source = excluded.source
            ''',
            (ticker_symbol,),
        )
        conn.commit()

    mode_label = "incremental" if has_valid_history else "full history"
    print(f"[+] Imported {len(prices)} Stooq daily closes ({mode_label}) for {ticker_symbol}.")
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
    if ticker_symbol.endswith((".DE", ".AS", ".NL")):
        return "EUR"
    if ticker_symbol.endswith(".UK"):
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
    print(f"[*] Starting daily market data collection: {current_date}")

    portfolio = load_portfolio(CONFIG_FILE)
    init_db(DB_FILE)
    session = build_stooq_session()

    anomaly_detected = False
    market_data = {}
    failed_tickers = []
    stop_scraping = False
    all_tickers = [ticker for tickers in portfolio.values() for ticker in tickers]

    for category, tickers in portfolio.items():
        print(f"[-] Processing category: {category}")

        for ticker in tickers:
            print(f"    -> Scraping: {ticker} (stooq: {to_stooq_symbol(ticker)})")
            try:
                price_history, made_request = fetch_price_history(
                    DB_FILE, ticker, current_date, session
                )
                if made_request:
                    time.sleep(random.uniform(2.5, 4.5))

                if not price_history:
                    raise StooqError(f"No stored daily prices for {ticker}.")

                hard_data = calculate_technical_data(ticker, price_history)
            except StooqError as e:
                print(f"[!] Stooq error for {ticker}: {e}")
                failed_tickers.append(ticker)
                if e.stop_run:
                    print("[!] Stopping early due to critical Stooq error.")
                    stop_scraping = True
                    break
                continue

            if hard_data["sma200_dist_pct"] is None:
                print(
                    f"[-] SMA200 unavailable for {ticker}: "
                    f"{len(price_history)} of 200 daily closes collected."
                )
            market_data[ticker] = hard_data

        if stop_scraping:
            break

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
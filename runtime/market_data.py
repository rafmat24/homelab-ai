import requests
import sqlite3
import json
import math
import os
import sys
import time
from datetime import datetime
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

# Threshold for triggering an immediate LLM analysis (e.g., -3.0%)
ANOMALY_DROP_THRESHOLD = -3.0
ALPHAVANTAGE_API_URL = "https://www.alphavantage.co/query"
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
API_REQUEST_INTERVAL_SECONDS = 12
_last_api_request_at = 0.0

ALPHAVANTAGE_EXCHANGE_SUFFIXES = {
    ".DE": ".DEX",
    ".AS": ".AMS",
}

# Ensure directories exist
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
        
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)

# ==============================================================================
# DATABASE SETUP
# ==============================================================================
def init_db(db_path: str):
    """Initializes the SQLite database and creates the metrics table if it doesn't exist."""
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
        conn.commit()

def save_to_db(db_path: str, record: tuple):
    """Inserts a single daily record into the SQLite database."""
    with sqlite3.connect(db_path) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO daily_metrics 
            (date, ticker, category, current_price, currency, daily_change_pct, 
             weekly_change_pct, sma200_dist_pct, rsi_14, technical_signal, news_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', record)
        conn.commit()

# ==============================================================================
# DATA SCRAPING FUNCTIONS
# ==============================================================================
def calculate_rsi(closes: list[float], window: int = 14) -> float:
    if len(closes) <= window:
        return math.nan

    changes = [current - previous for previous, current in zip(closes, closes[1:])]
    recent_changes = changes[-window:]
    average_gain = sum(max(change, 0) for change in recent_changes) / window
    average_loss = sum(max(-change, 0) for change in recent_changes) / window

    if average_loss == 0:
        return 100.0 if average_gain > 0 else math.nan
    relative_strength = average_gain / average_loss
    return 100 - (100 / (1 + relative_strength))

class AlphaVantageError(Exception):
    def __init__(self, message: str, stop_run: bool = False):
        super().__init__(message)
        self.stop_run = stop_run


def to_alphavantage_symbol(ticker_symbol: str) -> str:
    for source_suffix, target_suffix in ALPHAVANTAGE_EXCHANGE_SUFFIXES.items():
        if ticker_symbol.endswith(source_suffix):
            return f"{ticker_symbol[:-len(source_suffix)]}{target_suffix}"
    return ticker_symbol


def fetch_alphavantage_data(function: str, **params) -> dict:
    global _last_api_request_at

    if not ALPHAVANTAGE_API_KEY:
        raise AlphaVantageError("ALPHAVANTAGE_API_KEY is not configured.")

    elapsed = time.monotonic() - _last_api_request_at
    if _last_api_request_at and elapsed < API_REQUEST_INTERVAL_SECONDS:
        time.sleep(API_REQUEST_INTERVAL_SECONDS - elapsed)

    try:
        response = requests.get(
            ALPHAVANTAGE_API_URL,
            params={
                "function": function,
                "apikey": ALPHAVANTAGE_API_KEY,
                **params,
            },
            timeout=30,
        )
        _last_api_request_at = time.monotonic()
        response.raise_for_status()
        data = response.json()
    except requests.exceptions.RequestException as e:
        error_message = str(e).replace(ALPHAVANTAGE_API_KEY, "[redacted]")
        raise AlphaVantageError(
            f"Alpha Vantage request failed: {error_message}",
            stop_run=True,
        ) from e
    except ValueError as e:
        raise AlphaVantageError("Alpha Vantage returned invalid JSON.") from e

    if not isinstance(data, dict):
        raise AlphaVantageError("Alpha Vantage returned an unexpected response.")

    error_message = data.get("Error Message")
    if error_message:
        message = str(error_message)
        stop_run = any(
            phrase in message.lower()
            for phrase in ("call frequency", "requests per day", "rate limit", "call volume")
        )
        raise AlphaVantageError(message, stop_run=stop_run)

    notice = data.get("Note") or data.get("Information")
    if notice:
        raise AlphaVantageError(str(notice), stop_run=True)

    return data


def _time_series(data: dict, prefix: str) -> dict:
    for key, value in data.items():
        if key.startswith(prefix) and isinstance(value, dict):
            return value
    raise AlphaVantageError(f"Alpha Vantage response is missing {prefix}.")


def _get_currency(ticker_symbol: str) -> str:
    if ticker_symbol.endswith((".DE", ".AS")):
        return "EUR"
    return "USD"


def fetch_technical_data(ticker_symbol: str) -> dict | None:
    try:
        provider_symbol = to_alphavantage_symbol(ticker_symbol)
        daily_response = fetch_alphavantage_data(
            "TIME_SERIES_DAILY",
            symbol=provider_symbol,
            outputsize="compact",
        )
        daily_series = _time_series(daily_response, "Time Series (Daily)")
        closes = [
            float(values["4. close"])
            for _, values in sorted(daily_series.items())
        ]
        if not closes:
            print(f"[!] No daily price data returned for {ticker_symbol}.")
            return None

        current_price = closes[-1]
        
        # Calculate daily change (today vs yesterday)
        if len(closes) >= 2:
            prev_close = closes[-2]
            daily_change = ((current_price - prev_close) / prev_close) * 100
        else:
            daily_change = 0.0

        # Calculate weekly change (today vs 5 sessions ago)
        if len(closes) >= 6:
            weekly_close = closes[-6]
            weekly_change = ((current_price - weekly_close) / weekly_close) * 100
        else:
            weekly_change = 0.0

        current_rsi = calculate_rsi(closes)

        sma_response = fetch_alphavantage_data(
            "SMA",
            symbol=provider_symbol,
            interval="daily",
            time_period=200,
            series_type="close",
        )
        sma_series = _time_series(sma_response, "Technical Analysis: SMA")
        latest_sma_date = max(sma_series)
        sma_200 = float(sma_series[latest_sma_date]["SMA"])
        sma_dist_pct = ((current_price - sma_200) / sma_200) * 100

        if current_rsi < 30:
            tech_signal = "OVERSOLD"
        elif current_rsi > 70:
            tech_signal = "OVERBOUGHT"
        else:
            tech_signal = "NEUTRAL"

        return {
            "current_price": round(current_price, 2),
            "currency": _get_currency(ticker_symbol),
            "daily_change_pct": round(daily_change, 2),
            "weekly_change_pct": round(weekly_change, 2),
            "sma200_dist_pct": round(sma_dist_pct, 2),
            "rsi_14": round(current_rsi, 2),
            "technical_signal": tech_signal
        }
    except AlphaVantageError:
        raise
    except Exception as e:
        print(f"[!] Error fetching technical data for {ticker_symbol}: {e}")
        return None

def fetch_news_data(ticker_symbols: list[str]) -> dict[str, list[str]]:
    if not ticker_symbols:
        return {}

    provider_symbols = {
        to_alphavantage_symbol(ticker_symbol): ticker_symbol
        for ticker_symbol in ticker_symbols
    }

    try:
        response = fetch_alphavantage_data(
            "NEWS_SENTIMENT",
            tickers=",".join(provider_symbols),
            sort="LATEST",
            limit=200,
        )
        news_by_ticker = {ticker: [] for ticker in ticker_symbols}
        for article in response.get("feed", []):
            title = article.get("title")
            if not title:
                continue
            for sentiment in article.get("ticker_sentiment", []):
                source_symbol = sentiment.get("ticker")
                ticker = provider_symbols.get(source_symbol)
                if ticker and title not in news_by_ticker[ticker]:
                    news_by_ticker[ticker].append(title)
        return {
            ticker: headlines[:3] or ["No significant news."]
            for ticker, headlines in news_by_ticker.items()
        }
    except AlphaVantageError as e:
        if e.stop_run:
            raise
        print(f"[!] Error fetching Alpha Vantage news: {e}")
        return {ticker: ["Error fetching news."] for ticker in ticker_symbols}
    except Exception as e:
        print(f"[!] Error fetching Alpha Vantage news: {e}")
        return {ticker: ["Error fetching news."] for ticker in ticker_symbols}

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
def main():
    current_date = datetime.now().strftime('%Y-%m-%d')
    print(f"[*] Starting daily market data collection: {current_date}")

    if not ALPHAVANTAGE_API_KEY:
        print("[!] ALPHAVANTAGE_API_KEY is missing from the environment.")
        sys.exit(1)
    
    portfolio = load_portfolio(CONFIG_FILE)
    init_db(DB_FILE)
    
    anomaly_detected = False
    market_data = {}
    failed_tickers = []
    stop_scraping = False
    all_tickers = [
        ticker
        for tickers in portfolio.values()
        for ticker in tickers
    ]

    for category, tickers in portfolio.items():
        print(f"[-] Processing category: {category}")
        
        for ticker in tickers:
            print(f"    -> Scraping: {ticker}")
            try:
                hard_data = fetch_technical_data(ticker)
            except AlphaVantageError as e:
                print(f"[!] Alpha Vantage error for {ticker}: {e}")
                failed_tickers.append(ticker)
                if e.stop_run:
                    print("[!] Stopping early because an API request failed or Alpha Vantage reported a limit.")
                    stop_scraping = True
                    break
                continue

            if hard_data is None:
                failed_tickers.append(ticker)
                continue
            market_data[ticker] = hard_data

        if stop_scraping:
            break

    valid_tickers = [ticker for ticker in all_tickers if ticker in market_data]
    try:
        news_by_ticker = fetch_news_data(valid_tickers)
    except AlphaVantageError as e:
        print(f"[!] Alpha Vantage news error: {e}")
        if e.stop_run:
            failed_tickers.extend(ticker for ticker in valid_tickers if ticker not in failed_tickers)
        news_by_ticker = {ticker: ["Error fetching news."] for ticker in valid_tickers}

    for category, tickers in portfolio.items():
        for ticker in tickers:
            hard_data = market_data.get(ticker)
            if hard_data is None:
                continue

            if hard_data["daily_change_pct"] <= ANOMALY_DROP_THRESHOLD:
                print(f"    [!] ANOMALY DETECTED: {ticker} dropped by {hard_data['daily_change_pct']}%")
                anomaly_detected = True

            db_record = (
                current_date,
                ticker,
                category,
                hard_data["current_price"],
                hard_data["currency"],
                hard_data["daily_change_pct"],
                hard_data["weekly_change_pct"],
                hard_data["sma200_dist_pct"],
                hard_data["rsi_14"],
                hard_data["technical_signal"],
                json.dumps(news_by_ticker.get(ticker, ["No significant news."]), ensure_ascii=False)
            )
            save_to_db(DB_FILE, db_record)

    # If an anomaly was detected, create a flag file
    if anomaly_detected:
        print(f"[*] Anomaly threshold ({ANOMALY_DROP_THRESHOLD}%) breached. Setting trigger flag.")
        with open(ANOMALY_FLAG_FILE, 'w') as f:
            f.write(current_date)
            
    print(f"[+] Saved {len(market_data)} of {len(all_tickers)} assets to {DB_FILE}")
    if failed_tickers:
        print(f"[!] Failed to collect data for: {', '.join(failed_tickers)}")
        sys.exit(1)

if __name__ == "__main__":
    main()
import yfinance as yf
import pandas as pd
import sqlite3
import json
import os
import sys
from datetime import datetime

# ==============================================================================
# CONFIGURATION & DIRECTORY SETUP
# ==============================================================================
BASE_DIR = os.path.abspath(
    os.getenv("HOMELAB_AI_DIR", os.path.dirname(os.path.abspath(__file__)))
)
CONFIG_FILE = os.path.join(BASE_DIR, "configs", "portfolio.json")
DB_FILE = os.path.join(BASE_DIR, "logs", "market_data.db")
ANOMALY_FLAG_FILE = os.path.join(BASE_DIR, "logs", "trigger_anomaly.flag")

# Threshold for triggering an immediate LLM analysis (e.g., -3.0%)
ANOMALY_DROP_THRESHOLD = -3.0

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
def calculate_rsi(data: pd.DataFrame, window: int = 14) -> pd.Series:
    delta = data['Close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=window).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def fetch_technical_data(ticker_symbol: str) -> dict:
    try:
        ticker = yf.Ticker(ticker_symbol)
        hist = ticker.history(period="1y")
        
        if hist.empty:
            return None

        current_price = float(hist['Close'].iloc[-1])
        
        # Calculate daily change (today vs yesterday)
        if len(hist) >= 2:
            prev_close = float(hist['Close'].iloc[-2])
            daily_change = ((current_price - prev_close) / prev_close) * 100
        else:
            daily_change = 0.0

        # Calculate weekly change (today vs 5 sessions ago)
        if len(hist) >= 6:
            weekly_close = float(hist['Close'].iloc[-6])
            weekly_change = ((current_price - weekly_close) / weekly_close) * 100
        else:
            weekly_change = 0.0

        # Calculate SMA200 distance
        if len(hist) >= 200:
            sma_200 = float(hist['Close'].rolling(window=200).mean().iloc[-1])
            sma_dist_pct = ((current_price - sma_200) / sma_200) * 100
        else:
            sma_dist_pct = 0.0

        rsi_series = calculate_rsi(hist)
        current_rsi = float(rsi_series.iloc[-1])

        if current_rsi < 30:
            tech_signal = "OVERSOLD"
        elif current_rsi > 70:
            tech_signal = "OVERBOUGHT"
        else:
            tech_signal = "NEUTRAL"

        return {
            "current_price": round(current_price, 2),
            "currency": hist.info.get('currency', 'EUR') if hasattr(hist, 'info') else 'N/A',
            "daily_change_pct": round(daily_change, 2),
            "weekly_change_pct": round(weekly_change, 2),
            "sma200_dist_pct": round(sma_dist_pct, 2),
            "rsi_14": round(current_rsi, 2),
            "technical_signal": tech_signal
        }
    except Exception as e:
        print(f"[!] Error fetching technical data for {ticker_symbol}: {e}")
        return None

def fetch_news_data(ticker_symbol: str) -> list:
    try:
        ticker = yf.Ticker(ticker_symbol)
        headlines = [article.get('title', '') for article in ticker.news[:3] if article.get('title')]
        return headlines if headlines else ["No significant news."]
    except Exception as e:
        print(f"[!] Error fetching news data for {ticker_symbol}: {e}")
        return ["Error fetching news."]

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
def main():
    current_date = datetime.now().strftime('%Y-%m-%d')
    print(f"[*] Starting daily market data collection: {current_date}")
    
    portfolio = load_portfolio(CONFIG_FILE)
    init_db(DB_FILE)
    
    anomaly_detected = False

    for category, tickers in portfolio.items():
        print(f"[-] Processing category: {category}")
        
        for ticker in tickers:
            print(f"    -> Scraping: {ticker}")
            hard_data = fetch_technical_data(ticker)
            soft_data = fetch_news_data(ticker)
            
            if hard_data:
                # Check for massive daily drop (Anomaly)
                if hard_data["daily_change_pct"] <= ANOMALY_DROP_THRESHOLD:
                    print(f"    [!] ANOMALY DETECTED: {ticker} dropped by {hard_data['daily_change_pct']}%")
                    anomaly_detected = True

                # Prepare record for SQLite
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
                    json.dumps(soft_data, ensure_ascii=False)
                )
                save_to_db(DB_FILE, db_record)

    # If an anomaly was detected, create a flag file
    if anomaly_detected:
        print(f"[*] Anomaly threshold ({ANOMALY_DROP_THRESHOLD}%) breached. Setting trigger flag.")
        with open(ANOMALY_FLAG_FILE, 'w') as f:
            f.write(current_date)
            
    print(f"[+] Daily data collection finished. Saved to {DB_FILE}")

if __name__ == "__main__":
    main()
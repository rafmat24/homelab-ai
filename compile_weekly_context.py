import sqlite3
import json
import os
from datetime import datetime, timedelta
import sys

# ==============================================================================
# CONFIGURATION & DIRECTORY SETUP
# ==============================================================================
BASE_DIR = os.path.abspath(
    os.getenv("HOMELAB_AI_DIR", os.path.dirname(os.path.abspath(__file__)))
)
DB_FILE = os.path.join(BASE_DIR, "logs", "market_data.db")
OUTPUT_FILE = os.path.join(BASE_DIR, "logs", "latest_market_data.json")

# ==============================================================================
# HELPER FUNCTIONS
# ==============================================================================
def check_db_exists(db_path: str):
    """Ensures the database exists before attempting compilation."""
    if not os.path.exists(db_path):
        print(f"[!] Database file {db_path} not found. Ensure market_data.py has been run.")
        sys.exit(1)

def get_weekly_data(db_path: str) -> dict:
    """Extracts and aggregates data from the last 7 days from the SQLite DB."""
    # Calculate the date 7 days ago
    seven_days_ago = (datetime.now() - timedelta(days=7)).strftime('%Y-%m-%d')
    
    aggregated_data = {
        "report_date": datetime.now().strftime('%Y-%m-%d'),
        "portfolio_data": {}
    }

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            
            # 1. Identify all unique categories and tickers in the DB for the last week
            cursor.execute('''
                SELECT DISTINCT category, ticker 
                FROM daily_metrics 
                WHERE date >= ?
            ''', (seven_days_ago,))
            
            assets = cursor.fetchall()
            
            # Initialize portfolio structure
            for asset in assets:
                cat = asset['category']
                tick = asset['ticker']
                if cat not in aggregated_data["portfolio_data"]:
                    aggregated_data["portfolio_data"][cat] = {}
                aggregated_data["portfolio_data"][cat][tick] = {}

            # 2. Extract and compile data for each asset
            for asset in assets:
                cat = asset['category']
                tick = asset['ticker']
                
                # Get all records for this ticker sorted chronologically
                cursor.execute('''
                    SELECT * 
                    FROM daily_metrics 
                    WHERE ticker = ? AND date >= ? 
                    ORDER BY date ASC
                ''', (tick, seven_days_ago))
                
                records = cursor.fetchall()
                
                if not records:
                    continue
                    
                # Latest record (today's closing)
                latest = records[-1]
                # Oldest record in the window (start of the week)
                oldest = records[0]
                
                # Calculate synthetic weekly view
                price_start = oldest['current_price']
                price_end = latest['current_price']
                actual_weekly_change = ((price_end - price_start) / price_start) * 100 if price_start else 0.0
                
                # Aggregate news (avoiding duplicates)
                all_news = set()
                for rec in records:
                    if rec['news_json']:
                        news_list = json.loads(rec['news_json'])
                        for news_item in news_list:
                            all_news.add(news_item)

                # Format the compiled context
                aggregated_data["portfolio_data"][cat][tick] = {
                    "technical_summary": {
                        "price_start_of_week": price_start,
                        "price_end_of_week": price_end,
                        "currency": latest['currency'],
                        "actual_weekly_change_pct": round(actual_weekly_change, 2),
                        "latest_sma200_dist_pct": latest['sma200_dist_pct'],
                        "latest_rsi_14": latest['rsi_14'],
                        "latest_technical_signal": latest['technical_signal']
                    },
                    "aggregated_news": list(all_news)[:7] # Keep max 7 headlines to save context window
                }
                
        return aggregated_data
        
    except sqlite3.Error as e:
        print(f"[!] SQLite Error during weekly compilation: {e}")
        sys.exit(1)

# ==============================================================================
# MAIN EXECUTION
# ==============================================================================
def main():
    print(f"[*] Starting weekly context compilation: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    
    check_db_exists(DB_FILE)
    
    compiled_data = get_weekly_data(DB_FILE)
    
    if not compiled_data["portfolio_data"]:
        print("[!] No data found for the past week. Compilation aborted.")
        sys.exit(1)
        
    with open(OUTPUT_FILE, 'w', encoding='utf-8') as f:
        json.dump(compiled_data, f, ensure_ascii=False, indent=4)
        
    print(f"[+] Weekly context successfully compiled and saved to {OUTPUT_FILE}")

if __name__ == "__main__":
    main()
"""Archived Alpha Vantage client and news-cache implementation.

This module is retained for reference and is not imported by the active
Stooq-based market-data collector.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
BASE_DIR = os.path.abspath(
    os.getenv("HOMELAB_AI_DIR", os.path.dirname(SCRIPT_DIR))
)
load_dotenv(os.path.join(BASE_DIR, ".env"))

ALPHAVANTAGE_API_URL = "https://www.alphavantage.co/query"
ALPHAVANTAGE_API_KEY = os.getenv("ALPHAVANTAGE_API_KEY")
API_REQUEST_INTERVAL_SECONDS = 12
API_NETWORK_RETRIES = 3
API_RETRY_BACKOFF_SECONDS = (5, 15, 30)
NEWS_CACHE_DAYS = 7
_last_api_request_at = 0.0

ALPHAVANTAGE_EXCHANGE_SUFFIXES = {
    ".DE": ".DEX",
    ".AS": ".AMS",
}


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

    response = None
    for attempt in range(API_NETWORK_RETRIES + 1):
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
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            _last_api_request_at = time.monotonic()
            if attempt < API_NETWORK_RETRIES:
                delay = API_RETRY_BACKOFF_SECONDS[attempt]
                print(
                    f"[-] Alpha Vantage network error; retrying in {delay}s "
                    f"({attempt + 1}/{API_NETWORK_RETRIES})."
                )
                time.sleep(delay)
                continue
            error_message = str(e).replace(ALPHAVANTAGE_API_KEY, "[redacted]")
            raise AlphaVantageError(
                f"Alpha Vantage network request failed after "
                f"{API_NETWORK_RETRIES + 1} attempts: {error_message}",
                stop_run=True,
            ) from e
        except requests.exceptions.RequestException as e:
            _last_api_request_at = time.monotonic()
            error_message = str(e).replace(ALPHAVANTAGE_API_KEY, "[redacted]")
            raise AlphaVantageError(
                f"Alpha Vantage request failed: {error_message}",
                stop_run=True,
            ) from e

    try:
        data = response.json()
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
    except AlphaVantageError:
        raise
    except Exception as e:
        raise AlphaVantageError(f"Error fetching Alpha Vantage news: {e}") from e


def init_news_cache(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS news_cache (
                ticker TEXT PRIMARY KEY,
                fetched_date TEXT NOT NULL,
                news_json TEXT NOT NULL
            )
            """
        )


def load_news_cache(
    db_path: str,
    ticker_symbols: list[str],
    current_date: str,
) -> tuple[dict[str, list[str]], list[str]]:
    init_news_cache(db_path)
    cutoff_date = (
        datetime.strptime(current_date, "%Y-%m-%d") - timedelta(days=NEWS_CACHE_DAYS)
    ).strftime("%Y-%m-%d")
    cached_news = {}
    stale_tickers = []

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            f"""
            SELECT ticker, fetched_date, news_json
            FROM news_cache
            WHERE ticker IN ({",".join("?" for _ in ticker_symbols)})
            """,
            ticker_symbols,
        ).fetchall() if ticker_symbols else []

    for ticker, fetched_date, news_json in rows:
        cached_news[ticker] = json.loads(news_json)
        if fetched_date <= cutoff_date:
            stale_tickers.append(ticker)

    stale_tickers.extend(ticker for ticker in ticker_symbols if ticker not in cached_news)
    return cached_news, stale_tickers


def save_news_cache(
    db_path: str,
    fetched_date: str,
    news_by_ticker: dict[str, list[str]],
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            """
            INSERT INTO news_cache (ticker, fetched_date, news_json)
            VALUES (?, ?, ?)
            ON CONFLICT(ticker) DO UPDATE SET
                fetched_date = excluded.fetched_date,
                news_json = excluded.news_json
            """,
            [
                (ticker, fetched_date, json.dumps(news, ensure_ascii=False))
                for ticker, news in news_by_ticker.items()
            ],
        )
        conn.commit()


def get_news_data(
    db_path: str,
    ticker_symbols: list[str],
    current_date: str,
) -> dict[str, list[str]]:
    cached_news, stale_tickers = load_news_cache(
        db_path,
        ticker_symbols,
        current_date,
    )
    if not stale_tickers:
        print("[-] Using cached news.")
        return cached_news

    try:
        print(f"[-] Fetching news for {len(stale_tickers)} assets.")
        fresh_news = fetch_news_data(stale_tickers)
        save_news_cache(db_path, current_date, fresh_news)
        cached_news.update(fresh_news)
    except AlphaVantageError as e:
        print(f"[!] News refresh failed; retaining cached headlines: {e}")

    return {
        ticker: cached_news.get(ticker, ["News unavailable."])
        for ticker in ticker_symbols
    }

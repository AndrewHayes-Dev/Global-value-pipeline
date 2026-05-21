# pipeline/chart_fetcher.py
# Fetches Yahoo Finance chart data (all 4 periods) for rank 1-20 stocks from
# every index, deduplicates, and uploads charts/daily.json to R2.

import json
import os
import sys
import time
import urllib.parse
from datetime import datetime, timezone

import boto3
import requests
from botocore.config import Config

from uploader import upload_json

# R2 stock files produced by the monthly pipeline, with is_asx flag
_STOCK_FILES = {
    'stocks/sp500.json':       False,
    'stocks/djia.json':        False,
    'stocks/nasdaq.json':      False,
    'stocks/russell2000.json': False,
    'stocks/xao.json':         True,
}

# Yahoo Finance chart periods: (range, interval)
_PERIODS = [
    ('5d',  '1h'),
    ('3mo', '1d'),
    ('1y',  '1d'),
    ('3y',  '1wk'),
]

_MAX_RANK      = 20
_REQUEST_DELAY = 0.2   # seconds between Yahoo requests
_TIMEOUT       = 30    # seconds per HTTP request
_RETRY_DELAY   = 5     # seconds to wait after a 429

_BASE_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Origin': 'https://finance.yahoo.com',
    'Referer': 'https://finance.yahoo.com/',
}


# ── R2 helpers ────────────────────────────────────────────────────────────────

def _r2_client():
    account_id = os.environ['CLOUDFLARE_ACCOUNT_ID']
    return boto3.client(
        's3',
        endpoint_url=f'https://{account_id}.r2.cloudflarestorage.com',
        aws_access_key_id=os.environ['GLOBAL_R2_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['GLOBAL_R2_SECRET_ACCESS_KEY'],
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )


def _download_json(client, bucket: str, key: str):
    try:
        obj = client.get_object(Bucket=bucket, Key=key)
        return json.loads(obj['Body'].read())
    except Exception as e:
        print(f'  R2 read [{key}]: {e}')
        return None


# ── Ticker collection ─────────────────────────────────────────────────────────

def _collect_tickers(client, bucket: str) -> list[tuple[str, str]]:
    """Return [(cache_key, yahoo_symbol), ...] deduplicated by yahoo_symbol.

    cache_key is stored in charts/daily.json and used by the Flutter app.
    yahoo_symbol is what we pass to the Yahoo Finance chart API.
    ASX tickers are stored without .AX in R2 but need .AX for Yahoo.
    """
    seen_yahoo: set[str] = set()
    result: list[tuple[str, str]] = []

    for key, is_asx in _STOCK_FILES.items():
        data = _download_json(client, bucket, key)
        if not data:
            print(f'  Skipping {key} — could not download')
            continue
        for sector in data.get('sectors', []):
            for stock in sector.get('stocks', []):
                ticker = stock.get('ticker', '')
                rank   = stock.get('rank_in_sector')
                if not ticker or rank is None or rank > _MAX_RANK:
                    continue
                yahoo_symbol = f'{ticker}.AX' if is_asx else ticker
                if yahoo_symbol not in seen_yahoo:
                    seen_yahoo.add(yahoo_symbol)
                    result.append((ticker, yahoo_symbol))

    print(f'  Collected {len(result)} unique tickers (rank 1–{_MAX_RANK})')
    return result


# ── Yahoo Finance chart fetch ─────────────────────────────────────────────────

def _fetch_chart(
    session: requests.Session,
    yahoo_symbol: str,
    range_: str,
    interval: str,
) -> list:
    """Return [[timestamp_seconds, price], ...] oldest-first, or [] on failure."""
    try:
        encoded = urllib.parse.quote(yahoo_symbol)
        url = (
            f'https://query1.finance.yahoo.com/v8/finance/chart/{encoded}'
            f'?interval={interval}&range={range_}'
        )
        r = session.get(url, headers=_BASE_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 429:
            print(f'    Rate limited — retrying after {_RETRY_DELAY}s')
            time.sleep(_RETRY_DELAY)
            r = session.get(url, headers=_BASE_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            print(f'    HTTP {r.status_code} [{yahoo_symbol}/{range_}]')
            return []
        data = r.json()
        results = (data.get('chart') or {}).get('result') or []
        if not results:
            return []
        result     = results[0]
        timestamps = result.get('timestamp') or []
        closes     = (
            (result.get('indicators') or {})
            .get('quote', [{}])[0]
            .get('close') or []
        )
        points = []
        for ts, price in zip(timestamps, closes):
            if price is not None:
                points.append([int(ts), round(float(price), 4)])
        return points
    except Exception as e:
        print(f'    Chart error [{yahoo_symbol}/{range_}]: {e}')
        return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    bucket = os.environ.get('GLOBAL_R2_BUCKET_NAME', 'global-investor-data')
    client = _r2_client()

    print('Step 1: Collecting ranked tickers from R2...')
    tickers = _collect_tickers(client, bucket)
    if not tickers:
        print('ERROR: No tickers found — aborting')
        sys.exit(1)

    session = requests.Session()
    stocks_data: dict = {}

    print(
        f'Step 2: Fetching chart data for {len(tickers)} tickers '
        f'× {len(_PERIODS)} periods...'
    )
    for i, (cache_key, yahoo_symbol) in enumerate(tickers, 1):
        print(f'  [{i}/{len(tickers)}] {yahoo_symbol}')
        periods_data: dict = {}
        for range_, interval in _PERIODS:
            time.sleep(_REQUEST_DELAY)
            periods_data[range_] = _fetch_chart(session, yahoo_symbol, range_, interval)
        stocks_data[cache_key] = periods_data

    payload = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'stocks': stocks_data,
    }

    print('Step 3: Uploading charts/daily.json to R2...')
    success = upload_json('charts/daily.json', payload, bucket)
    if not success:
        print('ERROR: Upload failed')
        sys.exit(1)
    print(f'Done — {len(stocks_data)} stocks, {len(_PERIODS)} periods each.')


if __name__ == '__main__':
    main()

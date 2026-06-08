# pipeline/chart_fetcher.py
# Fetches YH Finance chart data (all 4 periods) for rank 1-20 stocks and
# uploads charts/daily-au.json (ASX) or charts/daily-us.json (US) to R2.
# Controlled by the MARKET env var: AU or US.

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

MARKET = os.environ.get('MARKET', 'US').upper()

# R2 stock files produced by the monthly pipeline, with is_asx flag
_AU_STOCK_FILES = {
    'stocks/xao.json': True,
}

_US_STOCK_FILES = {
    'stocks/sp500.json':       False,
    'stocks/djia.json':        False,
    'stocks/nasdaq.json':      False,
    'stocks/russell2000.json': False,
}

_STOCK_FILES = _AU_STOCK_FILES if MARKET == 'AU' else _US_STOCK_FILES
_OUTPUT_KEY  = 'charts/daily-au.json' if MARKET == 'AU' else 'charts/daily-us.json'

# Yahoo Finance chart periods: (range_key, interval, trailing_record_count)
# YH Finance /api/v2/markets/stock/history returns 640 records oldest-first;
# we take the last N entries for the requested time window.
_PERIODS = [
    ('5d',  '1h',  35),   # ~5 trading days × 7 hourly bars
    ('3mo', '1d',  65),   # ~3 months of daily bars
    ('1y',  '1d', 252),   # ~1 year of daily bars
    ('3y',  '1wk',156),   # ~3 years of weekly bars
]

_MAX_RANK      = 20
_REQUEST_DELAY = 0.25  # seconds between RapidAPI requests
_TIMEOUT       = 30    # seconds per HTTP request
_RETRY_DELAY   = 5     # seconds to wait after a 429

_RAPID_KEY  = 'fdb3e64a86msh9c5f4c5e59cf7a6p1dd3dcjsn1f9e68aa290b'
_RAPID_HOST = 'yahoo-finance15.p.rapidapi.com'
_RAPID_BASE = 'https://yahoo-finance15.p.rapidapi.com'
_RAPID_HEADERS = {
    'x-rapidapi-key':  _RAPID_KEY,
    'x-rapidapi-host': _RAPID_HOST,
    'Content-Type':    'application/json',
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

def _collect_tickers(client, bucket: str) -> list:
    """Return [(cache_key, yahoo_symbol), ...] deduplicated by yahoo_symbol.

    cache_key is stored in the chart JSON and used by the Flutter app.
    yahoo_symbol is what we pass to the YH Finance chart API.
    ASX tickers are stored without .AX in R2 but need .AX for YH Finance.
    """
    seen_yahoo: set = set()
    result: list = []

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


# ── YH Finance chart fetch ────────────────────────────────────────────────────

def _fetch_chart(
    session: requests.Session,
    yahoo_symbol: str,
    interval: str,
    keep: int,
) -> list:
    """Return [[timestamp_seconds, price], ...] oldest-first (last `keep` records), or [] on failure."""
    try:
        encoded = urllib.parse.quote(yahoo_symbol)
        url = (f'{_RAPID_BASE}/api/v2/markets/stock/history'
               f'?symbol={encoded}&interval={interval}')
        r = session.get(url, headers=_RAPID_HEADERS, timeout=_TIMEOUT)
        if r.status_code == 429:
            print(f'    Rate limited — retrying after {_RETRY_DELAY}s')
            time.sleep(_RETRY_DELAY)
            r = session.get(url, headers=_RAPID_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            print(f'    HTTP {r.status_code} [{yahoo_symbol}/{interval}]')
            return []
        body = r.json().get('body')
        if not isinstance(body, list) or not body:
            return []
        # body is sorted oldest-first; take the most recent `keep` records
        slice_ = body[-keep:] if len(body) > keep else body
        points = []
        for entry in slice_:
            ts    = entry.get('timestamp_unix')
            price = entry.get('close')
            if ts is not None and price is not None:
                points.append([int(ts), round(float(price), 4)])
        return points
    except Exception as e:
        print(f'    Chart error [{yahoo_symbol}/{interval}]: {e}')
        return []


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    bucket = os.environ.get('GLOBAL_R2_BUCKET_NAME', 'global-investor-data')
    client = _r2_client()

    print(f'MARKET={MARKET}, output={_OUTPUT_KEY}')
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
    # Deduplicate interval calls: 1d is used for both 3mo and 1y.
    # Fetch each unique interval once, then slice to produce each range key.
    for i, (cache_key, yahoo_symbol) in enumerate(tickers, 1):
        print(f'  [{i}/{len(tickers)}] {yahoo_symbol}')
        # Fetch 1h (5d), 1d (3mo + 1y), 1wk (3y) — 3 calls instead of 4
        raw: dict = {}
        for interval in ('1h', '1d', '1wk'):
            time.sleep(_REQUEST_DELAY)
            keep_max = max(
                keep for _, ivl, keep in _PERIODS if ivl == interval
            )
            raw[interval] = _fetch_chart(session, yahoo_symbol, interval, keep_max)

        periods_data: dict = {}
        for range_key, interval, keep in _PERIODS:
            all_pts = raw.get(interval, [])
            periods_data[range_key] = all_pts[-keep:] if len(all_pts) > keep else all_pts

        stocks_data[cache_key] = periods_data

    payload = {
        'generated_at': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'stocks': stocks_data,
    }

    print(f'Step 3: Uploading {_OUTPUT_KEY} to R2...')
    success = upload_json(_OUTPUT_KEY, payload, bucket)
    if not success:
        print('ERROR: Upload failed')
        sys.exit(1)

    # Remove the old combined file on first successful run (idempotent).
    try:
        client.delete_object(Bucket=bucket, Key='charts/daily.json')
        print('Cleaned up legacy charts/daily.json from R2.')
    except Exception:
        pass

    print(f'Done — {len(stocks_data)} stocks, {len(_PERIODS)} periods each.')


if __name__ == '__main__':
    main()

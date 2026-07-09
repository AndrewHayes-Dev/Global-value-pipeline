#!/usr/bin/env python3
# pipeline/patch_dividends.py
# Patches dividend_yield and dividend_rate into existing R2 stock JSONs
# without re-running the full 1.5-hour pipeline.
# Uses the same quote endpoint as quote_summary() to fetch dividend fields.

import json
import os
import sys
import time
import urllib.parse

import boto3
import requests
from botocore.config import Config
from dotenv import load_dotenv

load_dotenv()

_RAPID_KEY  = '24b6bc2e2bmsh347c3730e17abfcp17ef9cjsnf420d70ec43d'
_RAPID_HOST = 'yahoo-finance15.p.rapidapi.com'
_RAPID_BASE = 'https://yahoo-finance15.p.rapidapi.com'
_RAPID_HEADERS = {
    'x-rapidapi-key':  _RAPID_KEY,
    'x-rapidapi-host': _RAPID_HOST,
}

BUCKET            = os.environ.get('GLOBAL_R2_BUCKET_NAME', 'global-investor-data')
STOCK_FILES       = ['sp500', 'djia', 'nasdaq', 'russell2000', 'xao']
INTER_STOCK_DELAY = 0.6
RETRY_DELAY       = 2
RETRY_DELAY_2     = 5


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


def _upload_json(client, key: str, data: dict) -> None:
    import math

    def _sanitize(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, dict):
            return {k: _sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_sanitize(v) for v in obj]
        return obj

    body = json.dumps(_sanitize(data), separators=(',', ':'), default=str).encode('utf-8')
    client.put_object(
        Bucket=BUCKET,
        Key=key,
        Body=body,
        ContentType='application/json',
    )
    print(f'  Uploaded {key} ({len(body):,} bytes)')


def _fetch_dividend(session: requests.Session, symbol: str) -> tuple:
    """Returns (div_yield_fraction, div_rate_annual_usd) or (None, None)."""
    encoded = urllib.parse.quote(symbol)
    url = f'{_RAPID_BASE}/api/yahoo/qu/quote/{encoded}'
    try:
        r = session.get(url, headers=_RAPID_HEADERS, timeout=30)
        if r.status_code == 429:
            print(f'    Rate limit [{symbol}] — waiting {RETRY_DELAY}s')
            time.sleep(RETRY_DELAY)
            r = session.get(url, headers=_RAPID_HEADERS, timeout=30)
        if r.status_code == 429:
            print(f'    Rate limit [{symbol}] — waiting {RETRY_DELAY_2}s')
            time.sleep(RETRY_DELAY_2)
            r = session.get(url, headers=_RAPID_HEADERS, timeout=30)
        if r.status_code != 200:
            return None, None
        body = r.json().get('body')
        q = body[0] if isinstance(body, list) and body else (body if isinstance(body, dict) else None)
        if not q:
            return None, None

        raw_yield = q.get('trailingAnnualDividendYield')
        raw_rate  = q.get('dividendRate')

        div_yield = float(raw_yield) if raw_yield is not None else None
        if div_yield is not None:
            if div_yield > 1.0:
                div_yield /= 100.0
            if div_yield <= 0:
                div_yield = None

        div_rate = float(raw_rate) if raw_rate is not None else None
        if div_rate is not None and div_rate <= 0:
            div_rate = None

        return div_yield, div_rate
    except Exception as e:
        print(f'    Error [{symbol}]: {e}')
        return None, None


def main():
    client  = _r2_client()
    session = requests.Session()
    total_patched   = 0
    total_with_divs = 0

    for index_id in STOCK_FILES:
        key = f'stocks/{index_id}.json'
        print(f'\n── {index_id} ──────────────────────')
        print(f'  Downloading {key}...')

        resp = client.get_object(Bucket=BUCKET, Key=key)
        data = json.loads(resp['Body'].read())

        all_stocks = [s for sec in data['sectors'] for s in sec['stocks']]
        print(f'  {len(all_stocks)} stocks across {len(data["sectors"])} sectors')

        patched   = 0
        with_divs = 0
        for sector in data['sectors']:
            for stock in sector['stocks']:
                ticker = stock['ticker']
                # ASX tickers may or may not already have .AX suffix
                if index_id == 'xao':
                    symbol = ticker if '.' in ticker else f'{ticker}.AX'
                else:
                    symbol = ticker

                div_yield, div_rate = _fetch_dividend(session, symbol)
                stock['dividend_yield'] = div_yield
                stock['dividend_rate']  = div_rate

                if div_yield:
                    print(f'    {ticker}: {div_yield*100:.2f}%  ${div_rate}/yr')
                    with_divs += 1

                patched += 1
                time.sleep(INTER_STOCK_DELAY)

        _upload_json(client, key, data)
        total_patched   += patched
        total_with_divs += with_divs
        print(f'  Patched {patched} stocks — {with_divs} pay dividends')

    print(f'\n═══════════════════════════════════════')
    print(f'Done — {total_patched} stocks patched, {total_with_divs} pay dividends')


if __name__ == '__main__':
    main()

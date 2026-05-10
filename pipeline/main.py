#!/usr/bin/env python3
# pipeline/main.py
# Global Value Pipeline orchestrator.
# Runs on the 1st of every month via GitHub Actions, or manually.

import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from fetcher import YahooFetcher, FmpFetcher, EdgarFetcher, fetch_xao, WikipediaScraper
from scorer import score_from_summary, enrich_from_fmp, enrich_from_edgar
from uploader import upload_json
from generate_stock_spreadsheets import generate_all as generate_spreadsheets
from sector_stocks import (
    INDEX_DEFINITIONS, GICS_TO_SUFFIX, GICS_SECTORS_ORDERED,
    RUSSELL2000_SECTOR_STOCKS, FMP_RATIOS_ALLOWED,
)

load_dotenv()

BUCKET             = os.environ.get('GLOBAL_R2_BUCKET_NAME', 'global-investor-data')
FMP_KEY            = os.environ.get('FMP_API_KEY', '')
INTER_STOCK_DELAY  = 0.6   # seconds between Yahoo quoteSummary calls


def _fmt_vol(n):
    if n is None:
        return '—'
    n = int(n)
    if n >= 1_000_000_000:
        return f'{n / 1e9:.2f}B'
    if n >= 1_000_000:
        return f'{n / 1e6:.2f}M'
    if n >= 1_000:
        return f'{n / 1e3:.1f}K'
    return str(n)


# ── Index quote ───────────────────────────────────────────────────────────────

def process_index_quote(yahoo: YahooFetcher, fmp: FmpFetcher, index_def: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    q = yahoo.chart(index_def['yahoo'])

    if q:
        price   = q.get('regularMarketPrice')
        prev    = q.get('chartPreviousClose') or q.get('previousClose')
        change  = (price - prev) if price and prev else None
        chg_pct = (change / prev * 100) if change and prev else None
        return {
            'index_id':       index_def['id'],
            'name':           index_def['name'],
            'value':          price,
            'change':         change,
            'change_percent': chg_pct,
            'day_high':       q.get('regularMarketDayHigh'),
            'day_low':        q.get('regularMarketDayLow'),
            'week_52_high':   q.get('fiftyTwoWeekHigh'),
            'week_52_low':    q.get('fiftyTwoWeekLow'),
            'volume':         _fmt_vol(q.get('regularMarketVolume')),
            'updated_at':     now,
        }

    if index_def['country'] == 'US' and index_def['fmp']:
        print(f'  Yahoo failed for {index_def["id"]} — trying FMP fallback')
        quote = fmp.index_quote(index_def['fmp'])
        if quote:
            return {
                'index_id':       index_def['id'],
                'name':           index_def['name'],
                'value':          quote.get('price'),
                'change':         quote.get('change'),
                'change_percent': quote.get('changePercentage'),
                'day_high':       quote.get('dayHigh'),
                'day_low':        quote.get('dayLow'),
                'week_52_high':   quote.get('yearHigh'),
                'week_52_low':    quote.get('yearLow'),
                'volume':         _fmt_vol(quote.get('volume')),
                'updated_at':     now,
            }

    print(f'  No quote available for {index_def["id"]}')
    return {'index_id': index_def['id'], 'name': index_def['name'], 'updated_at': now}


# ── Price trend filter ────────────────────────────────────────────────────────

def _has_upward_price_trend(yahoo: YahooFetcher, symbol: str) -> bool:
    """Return True if current price > price ~2 years ago."""
    prices = yahoo.price_trend_check(symbol)
    if prices is None:
        return True  # if data unavailable, don't exclude
    old_price, current_price = prices
    return current_price > old_price


# ── Group constituents by GICS sector ────────────────────────────────────────

def _group_by_sector(constituents: list[dict], index_prefix: str) -> dict:
    """Returns {sector_id: {'name': str, 'stocks': [{ticker, industry}]}}"""
    sector_map = {}
    for c in constituents:
        gics = c.get('gics_sector', '')
        suffix = GICS_TO_SUFFIX.get(gics)
        if not suffix:
            continue
        sector_id = f'{index_prefix}_{suffix}'
        if sector_id not in sector_map:
            sector_map[sector_id] = {'name': gics, 'stocks': []}
        sector_map[sector_id]['stocks'].append({
            'ticker':       c['ticker'],
            'industry':     c.get('gics_industry', ''),
            'company_name': c.get('company_name', ''),
        })
    return sector_map


# ── Process one US sector ─────────────────────────────────────────────────────

def process_us_sector(yahoo: YahooFetcher, fmp: FmpFetcher, edgar: EdgarFetcher,
                      sector_id: str, sector_name: str,
                      sector_constituents: list[dict],
                      index_id: str) -> dict:
    print(f'    Screening {sector_id}: {len(sector_constituents)} candidates')
    scored = []

    for entry in sector_constituents:
        ticker   = entry['ticker']
        industry = entry.get('industry', '')
        try:
            # 2yr price trend filter
            yahoo_symbol = ticker
            if not _has_upward_price_trend(yahoo, yahoo_symbol):
                continue
            time.sleep(0.3)

            summary = yahoo.quote_summary(yahoo_symbol)
            stock = score_from_summary(ticker, summary, sector_id, index_id, industry) if summary else None
            if stock:
                scored.append(stock)
        except Exception as e:
            print(f'      Error [{ticker}]: {e}')
        time.sleep(INTER_STOCK_DELAY)

    scored.sort(key=lambda s: s['score'], reverse=True)
    top20 = scored[:20]

    enriched = []
    for stock in top20:
        # FMP enrichment for well-known large-caps
        if stock['ticker'] in FMP_RATIOS_ALLOWED and FMP_KEY:
            ratios_list = fmp.ratios(stock['ticker'], limit=2)
            if ratios_list:
                km = fmp.key_metrics(stock['ticker'], limit=1)
                stock = enrich_from_fmp(stock, ratios_list, km)
        # EDGAR fallback for any fields still null after FMP
        if (stock.get('interest_coverage') is None or
                stock.get('book_value_growth') is None or
                stock.get('net_net_ratio') is None):
            edgar_data = edgar.get_stock_data(stock['ticker'])
            if edgar_data:
                stock = enrich_from_edgar(stock, edgar_data)
            time.sleep(0.1)
        enriched.append(stock)
    enriched.sort(key=lambda s: s['score'], reverse=True)

    stocks_out = [_format_stock(s, rank) for rank, s in enumerate(enriched, 1)]
    print(f'    Top {len(stocks_out)} for {sector_id}')
    return {'sector_id': sector_id, 'name': sector_name, 'stocks': stocks_out}


# ── Process Russell 2000 sector (curated tickers) ────────────────────────────

def process_r2k_sector(yahoo: YahooFetcher, fmp: FmpFetcher, edgar: EdgarFetcher,
                        sector_id: str, sector_name: str,
                        tickers: list[str]) -> dict:
    print(f'    Screening {sector_id}: {len(tickers)} tickers')
    scored = []

    for ticker in tickers:
        try:
            if not _has_upward_price_trend(yahoo, ticker):
                continue
            time.sleep(0.3)
            summary = yahoo.quote_summary(ticker)
            stock = score_from_summary(ticker, summary, sector_id, 'russell2000') if summary else None
            if stock:
                scored.append(stock)
        except Exception as e:
            print(f'      Error [{ticker}]: {e}')
        time.sleep(INTER_STOCK_DELAY)

    scored.sort(key=lambda s: s['score'], reverse=True)
    top20 = scored[:20]

    enriched = []
    for stock in top20:
        if (stock.get('interest_coverage') is None or
                stock.get('book_value_growth') is None or
                stock.get('net_net_ratio') is None):
            edgar_data = edgar.get_stock_data(stock['ticker'])
            if edgar_data:
                stock = enrich_from_edgar(stock, edgar_data)
            time.sleep(0.1)
        enriched.append(stock)

    stocks_out = [_format_stock(s, rank) for rank, s in enumerate(enriched, 1)]
    print(f'    Top {len(stocks_out)} for {sector_id}')
    return {'sector_id': sector_id, 'name': sector_name, 'stocks': stocks_out,
            'constituents_unavailable': False}


# ── Process XAO sector ────────────────────────────────────────────────────────

def process_xao_sector(yahoo: YahooFetcher, sector_id: str, sector_name: str,
                        tickers: list[str], sector_industries: dict) -> dict:
    print(f'    Screening {sector_id}: {len(tickers)} tickers')
    scored = []

    for ticker in tickers:
        symbol = ticker if '.' in ticker else f'{ticker}.AX'
        industry = sector_industries.get(ticker, '')
        try:
            if not _has_upward_price_trend(yahoo, symbol):
                continue
            time.sleep(0.3)
            summary = yahoo.quote_summary(symbol)
            stock = score_from_summary(ticker, summary, sector_id, 'xao', industry) if summary else None
            if stock:
                scored.append(stock)
        except Exception as e:
            print(f'      Error [{ticker}]: {e}')
        time.sleep(INTER_STOCK_DELAY)

    scored.sort(key=lambda s: s['score'], reverse=True)
    top20 = scored[:20]
    stocks_out = [_format_stock(s, rank) for rank, s in enumerate(top20, 1)]
    print(f'    Top {len(stocks_out)} for {sector_id}')
    return {'sector_id': sector_id, 'name': sector_name, 'stocks': stocks_out}


def _format_stock(s: dict, rank: int) -> dict:
    return {
        'ticker':               s['ticker'],
        'company_name':         s['company_name'],
        'industry':             s.get('industry', ''),
        'rank_in_sector':       rank,
        'score':                round(s['score'], 1),
        'pe_ratio':             s['pe_ratio'],
        'pb_ratio':             s['pb_ratio'],
        'dividend_yield':       s['dividend_yield'],
        'free_cash_flow':       s['free_cash_flow'],
        'debt_equity':          s['debt_equity'],
        'revenue':              s['revenue'],
        'net_income':           s['net_income'],
        'intrinsic_value':      s['intrinsic_value'],
        'margin_of_safety':     s['margin_of_safety'],
        'current_price':        s['current_price'],
        'owner_earnings':       s.get('owner_earnings'),
        'roic':                 s.get('roic'),
        'roe':                  s.get('roe'),
        'peg_ratio':            s.get('peg_ratio'),
        'interest_coverage':    s.get('interest_coverage'),
        'gross_margin':         s.get('gross_margin'),
        'current_ratio':        s.get('current_ratio'),
        'eps_growth':           s.get('eps_growth'),
        'roe_3yr_avg':          s.get('roe_3yr_avg'),
        'earnings_consistency': s.get('earnings_consistency'),
        'book_value_growth':    s.get('book_value_growth'),
        'net_net_ratio':        s.get('net_net_ratio'),
        'revenue_growth':       s.get('revenue_growth'),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    start    = time.time()
    now_utc  = datetime.now(timezone.utc).isoformat()
    print(f'\n=== Global Value Pipeline — {now_utc} ===\n')

    yahoo  = YahooFetcher()
    fmp    = FmpFetcher(FMP_KEY)
    wiki   = WikipediaScraper()
    edgar  = EdgarFetcher()
    print('Loading EDGAR ticker map...')
    edgar.warm_up()

    # ── XAO universe ─────────────────────────────────────────────────────────
    print('Fetching XAO stock universe...')
    xao_raw = fetch_xao()
    xao_sector_map: dict[str, list[str]] = {}       # suffix → [tickers]
    xao_industry_map: dict[str, str]     = {}       # ticker → industry

    if xao_raw:
        for entry in xao_raw.get('stocks', []):
            ticker   = entry.get('ticker')
            gics     = entry.get('sector')
            industry = entry.get('industry', '')
            if ticker and gics:
                suffix = GICS_TO_SUFFIX.get(gics)
                if suffix:
                    xao_sector_map.setdefault(suffix, []).append(ticker)
                    xao_industry_map[ticker] = industry
        count = sum(len(v) for v in xao_sector_map.values())
        print(f'  XAO loaded: {count} stocks across {len(xao_sector_map)} sectors')
        upload_json('xao.json', xao_raw, BUCKET)
    else:
        print('  WARNING: XAO unavailable — XAO sectors will be empty')

    # ── US dynamic constituents ───────────────────────────────────────────────
    print('\nScraping US index constituents from Wikipedia...')
    sp500_constituents   = wiki.sp500()
    djia_constituents    = wiki.djia()
    nasdaq_constituents  = wiki.nasdaq100()

    sp500_by_sector  = _group_by_sector(sp500_constituents,  'sp500')
    djia_by_sector   = _group_by_sector(djia_constituents,   'djia')
    nasdaq_by_sector = _group_by_sector(nasdaq_constituents, 'nasdaq')

    # ── Index quotes ─────────────────────────────────────────────────────────
    print('\nFetching index quotes...')
    indices_out = []
    for idx in INDEX_DEFINITIONS:
        print(f'  {idx["id"]}...')
        indices_out.append(process_index_quote(yahoo, fmp, idx))
    upload_json('indices.json', indices_out, BUCKET)

    # ── Sector stocks per index ───────────────────────────────────────────────
    total_stocks   = 0
    all_index_data = {}

    for idx in INDEX_DEFINITIONS:
        print(f'\nProcessing {idx["name"]} sectors...')
        sectors_out = []

        if idx['id'] == 'xao':
            for gics_name, suffix in GICS_SECTORS_ORDERED:
                sector_id = f'xao_{suffix}'
                tickers   = xao_sector_map.get(suffix, [])
                if not tickers:
                    sectors_out.append({'sector_id': sector_id, 'name': gics_name, 'stocks': []})
                    continue
                sd = process_xao_sector(yahoo, sector_id, gics_name, tickers, xao_industry_map)
                sectors_out.append(sd)
                total_stocks += len(sd['stocks'])

        elif idx['id'] == 'sp500':
            for gics_name, suffix in GICS_SECTORS_ORDERED:
                sector_id    = f'sp500_{suffix}'
                constituents = sp500_by_sector.get(sector_id, {}).get('stocks', [])
                if not constituents:
                    sectors_out.append({'sector_id': sector_id, 'name': gics_name, 'stocks': []})
                    continue
                sd = process_us_sector(yahoo, fmp, edgar, sector_id, gics_name, constituents, 'sp500')
                sectors_out.append(sd)
                total_stocks += len(sd['stocks'])

        elif idx['id'] == 'djia':
            for gics_name, suffix in GICS_SECTORS_ORDERED:
                sector_id    = f'djia_{suffix}'
                constituents = djia_by_sector.get(sector_id, {}).get('stocks', [])
                if not constituents:
                    continue
                sd = process_us_sector(yahoo, fmp, edgar, sector_id, gics_name, constituents, 'djia')
                sectors_out.append(sd)
                total_stocks += len(sd['stocks'])

        elif idx['id'] == 'nasdaq':
            for gics_name, suffix in GICS_SECTORS_ORDERED:
                sector_id    = f'nasdaq_{suffix}'
                constituents = nasdaq_by_sector.get(sector_id, {}).get('stocks', [])
                if not constituents:
                    continue
                sd = process_us_sector(yahoo, fmp, edgar, sector_id, gics_name, constituents, 'nasdaq')
                sectors_out.append(sd)
                total_stocks += len(sd['stocks'])

        elif idx['id'] == 'russell2000':
            for gics_name, suffix in GICS_SECTORS_ORDERED:
                sector_id = f'russell2000_{suffix}'
                tickers   = RUSSELL2000_SECTOR_STOCKS.get(sector_id, [])
                if not tickers:
                    continue
                sd = process_r2k_sector(yahoo, fmp, edgar, sector_id, gics_name, tickers)
                sectors_out.append(sd)
                total_stocks += len(sd['stocks'])

        index_payload = {
            'index_id':     idx['id'],
            'score_version': 1,
            'sectors':       sectors_out,
        }
        upload_json(f'stocks/{idx["id"]}.json', index_payload, BUCKET)
        all_index_data[idx['id']] = index_payload

    # ── Spreadsheets ─────────────────────────────────────────────────────────
    generate_spreadsheets(all_index_data, BUCKET)

    # ── Metadata ─────────────────────────────────────────────────────────────
    elapsed  = round(time.time() - start)
    metadata = {
        'last_updated':    now_utc,
        'version':         '1',
        'indices_count':   len(INDEX_DEFINITIONS),
        'total_stocks':    total_stocks,
        'elapsed_seconds': elapsed,
    }
    upload_json('metadata.json', metadata, BUCKET)

    print(f'\n=== Pipeline complete in {elapsed}s — {total_stocks} stocks ===\n')


if __name__ == '__main__':
    main()

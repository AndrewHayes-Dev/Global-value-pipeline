#!/usr/bin/env python3
# pipeline/main.py
# Global Value Pipeline orchestrator.
# Runs on the 1st of every month via GitHub Actions, or manually.

import os
import time
from datetime import datetime, timezone

from dotenv import load_dotenv

from fetcher import YHFinanceFetcher, FmpFetcher, EdgarFetcher, fetch_ioz_constituents, fetch_iwm_constituents, WikipediaScraper
from scorer import score_from_summary, enrich_from_fmp, enrich_from_edgar
from uploader import upload_json
from generate_stock_spreadsheets import generate_all as generate_spreadsheets
from sector_stocks import (
    INDEX_DEFINITIONS, GICS_TO_SUFFIX, GICS_SECTORS_ORDERED,
    FMP_RATIOS_ALLOWED,
)
from notify import send_data_update_notification

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


# ── Index quote ────────────────────────────────────────────────────────────────────────────────

def process_index_quote(yahoo: YHFinanceFetcher, fmp: FmpFetcher, index_def: dict) -> dict:
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


# ── Group constituents by GICS sector ────────────────────────────────────────────────────────────────────

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


# ── Process one US sector ────────────────────────────────────────────────────────────────────────────────

def process_us_sector(yahoo: YHFinanceFetcher, fmp: FmpFetcher, edgar: EdgarFetcher,
                      sector_id: str, sector_name: str,
                      sector_constituents: list[dict],
                      index_id: str, top_n: int = 3) -> dict:
    print(f'    Screening {sector_id}: {len(sector_constituents)} candidates')
    scored = []

    for entry in sector_constituents:
        ticker   = entry['ticker']
        industry = entry.get('industry', '')
        try:
            yahoo_symbol = ticker
            summary = yahoo.quote_summary(yahoo_symbol)
            stock = score_from_summary(ticker, summary, sector_id, index_id, industry) if summary else None
            if stock:
                scored.append(stock)
        except Exception as e:
            print(f'      Error [{ticker}]: {e}')
        time.sleep(INTER_STOCK_DELAY)

    scored.sort(key=lambda s: s.get('blended_score', s['score']), reverse=True)
    top_survivors = scored[:top_n]

    enriched = []
    for stock in top_survivors:
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


# ── Process XAO sector ────────────────────────────────────────────────────────────────────────────────────

def process_xao_sector(yahoo: YHFinanceFetcher, sector_id: str, sector_name: str,
                        tickers: list[str], sector_industries: dict,
                        top_n: int = 3) -> dict:
    print(f'    Screening {sector_id}: {len(tickers)} tickers')
    scored = []

    for ticker in tickers:
        symbol = ticker if '.' in ticker else f'{ticker}.AX'
        industry = sector_industries.get(ticker, '')
        try:
            summary = yahoo.quote_summary(symbol)
            stock = score_from_summary(ticker, summary, sector_id, 'xao', industry) if summary else None
            if stock:
                scored.append(stock)
        except Exception as e:
            print(f'      Error [{ticker}]: {e}')
        time.sleep(INTER_STOCK_DELAY)

    scored.sort(key=lambda s: s.get('blended_score', s['score']), reverse=True)
    top_survivors = scored[:top_n]
    stocks_out = [_format_stock(s, rank) for rank, s in enumerate(top_survivors, 1)]
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
        'dividend_rate':        s.get('dividend_rate'),
        'free_cash_flow':       s['free_cash_flow'],
        'debt_equity':          s['debt_equity'],
        'revenue':              s['revenue'],
        'net_income':           s['net_income'],
        'intrinsic_value':      s['intrinsic_value'],
        'margin_of_safety':     s['margin_of_safety'],
        'growth_basis':         s.get('growth_basis'),
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
        'quality_score':        s.get('quality_score'),
        'blended_score':        s.get('blended_score'),
        'fcf_margin':           s.get('fcf_margin'),
        'capex_intensity':      s.get('capex_intensity'),
        'shareholder_yield':    s.get('shareholder_yield'),
        'ev_ebit':              s.get('ev_ebit'),
        'ps_ratio':             s.get('ps_ratio'),
        'operating_margin':     s.get('operating_margin'),
        'gross_margin_trend':   s.get('gross_margin_trend'),
    }


# ── Main ──────────────────────────────────────────────────────────────────────────────────────────────

def main():
    start    = time.time()
    now_utc  = datetime.now(timezone.utc).isoformat()
    print(f'\n=== Global Value Pipeline — {now_utc} ===\n')

    yahoo  = YHFinanceFetcher()
    fmp    = FmpFetcher(FMP_KEY)
    wiki   = WikipediaScraper()
    edgar  = EdgarFetcher()
    print('Loading EDGAR ticker map...')
    edgar.warm_up()

    # ── XAO dynamic constituents ──────────────────────────────────────────────────────────────────────────
    print('Fetching XAO stock universe from iShares IOZ...')
    ioz_constituents = fetch_ioz_constituents(top_n=200)
    ioz_by_sector    = _group_by_sector(ioz_constituents, 'xao')
    count = sum(len(v['stocks']) for v in ioz_by_sector.values())
    print(f'  IOZ loaded: {count} stocks across {len(ioz_by_sector)} sectors')

    # ── US dynamic constituents ───────────────────────────────────────────────────────────────────────
    print('\nScraping US index constituents from Wikipedia...')
    sp500_constituents   = wiki.sp500()
    djia_constituents    = wiki.djia()
    nasdaq_constituents  = wiki.nasdaq100()

    sp500_by_sector  = _group_by_sector(sp500_constituents,  'sp500')
    djia_by_sector   = _group_by_sector(djia_constituents,   'djia')
    nasdaq_by_sector = _group_by_sector(nasdaq_constituents, 'nasdaq')

    # ── Russell 2000 dynamic constituents ───────────────────────────────────────────────────────────────
    print('\nFetching Russell 2000 constituents from iShares IWM...')
    try:
        r2k_constituents = fetch_iwm_constituents(top_per_sector=10)
    except Exception as e:
        print(f'  WARNING: IWM fetch failed — Russell 2000 sectors will be empty: {e}')
        r2k_constituents = []
    r2k_by_sector = _group_by_sector(r2k_constituents, 'russell2000')

    # ── Index quotes ──────────────────────────────────────────────────────────────────────────────────
    print('\nFetching index quotes...')
    indices_out = []
    for idx in INDEX_DEFINITIONS:
        print(f'  {idx["id"]}...')
        indices_out.append(process_index_quote(yahoo, fmp, idx))
    upload_json('indices.json', indices_out, BUCKET)

    # ── Sector stocks per index ─────────────────────────────────────────────────────────────────────────────
    total_stocks   = 0
    all_index_data = {}

    for idx in INDEX_DEFINITIONS:
        print(f'\nProcessing {idx["name"]} sectors...')
        sectors_out = []

        if idx['id'] == 'xao':
            for gics_name, suffix in GICS_SECTORS_ORDERED:
                sector_id        = f'xao_{suffix}'
                sector_data      = ioz_by_sector.get(sector_id, {})
                sector_stocks_list = sector_data.get('stocks', [])
                if not sector_stocks_list:
                    sectors_out.append({'sector_id': sector_id, 'name': gics_name, 'stocks': []})
                    continue
                tickers      = [s['ticker'] for s in sector_stocks_list]
                industry_map = {s['ticker']: s.get('industry', '') for s in sector_stocks_list}
                sd = process_xao_sector(yahoo, sector_id, gics_name, tickers, industry_map)
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
                sector_id    = f'russell2000_{suffix}'
                constituents = r2k_by_sector.get(sector_id, {}).get('stocks', [])
                if not constituents:
                    continue
                sd = process_us_sector(yahoo, fmp, edgar, sector_id, gics_name, constituents, 'russell2000', top_n=3)
                sectors_out.append(sd)
                total_stocks += len(sd['stocks'])

        index_payload = {
            'index_id':     idx['id'],
            'score_version': 1,
            'sectors':       sectors_out,
        }
        upload_json(f'stocks/{idx["id"]}.json', index_payload, BUCKET)
        all_index_data[idx['id']] = index_payload

    # ── Spreadsheets ──────────────────────────────────────────────────────────────────────────────────
    generate_spreadsheets(all_index_data, BUCKET)

    # ── Metadata ───────────────────────────────────────────────────────────────────────────────────────
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

    send_data_update_notification(
        total_stocks=total_stocks,
        indices_count=len(INDEX_DEFINITIONS),
    )


if __name__ == '__main__':
    main()

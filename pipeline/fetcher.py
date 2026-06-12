# pipeline/fetcher.py
# YH Finance (yahoo-finance15) + FMP HTTP clients, Wikipedia constituent scraper, XAO fetcher.

import calendar
import concurrent.futures
import time
import urllib.parse
from datetime import date as _date
from typing import Optional

import requests

_RAPID_KEY  = 'fdb3e64a86msh9c5f4c5e59cf7a6p1dd3dcjsn1f9e68aa290b'
_RAPID_HOST = 'yahoo-finance15.p.rapidapi.com'
_RAPID_BASE = 'https://yahoo-finance15.p.rapidapi.com'
_RAPID_HEADERS = {
    'x-rapidapi-key':  _RAPID_KEY,
    'x-rapidapi-host': _RAPID_HOST,
    'Content-Type':    'application/json',
}

_TIMEOUT     = 30
_RETRY_DELAY = 2


# ── YH Finance (yahoo-finance15) ──────────────────────────────────────────────

class YHFinanceFetcher:
    def __init__(self):
        self.session = requests.Session()

    def _fetch_module(self, ticker: str, module: str) -> Optional[dict]:
        """Fetch a single YH Finance module. Returns the module body dict or None."""
        try:
            encoded = urllib.parse.quote(ticker)
            url = (f'{_RAPID_BASE}/api/v1/markets/stock/modules'
                   f'?ticker={encoded}&module={module}')
            r = self.session.get(url, headers=_RAPID_HEADERS, timeout=_TIMEOUT)
            if r.status_code == 429:
                print(f'  YHFinance rate limit [{ticker}/{module}] — waiting {_RETRY_DELAY}s')
                time.sleep(_RETRY_DELAY)
                r = self.session.get(url, headers=_RAPID_HEADERS, timeout=_TIMEOUT)
            if r.status_code != 200:
                return None
            data = r.json()
            body = data.get('body')
            if not body or not isinstance(body, dict):
                return None
            return body
        except Exception as e:
            print(f'  YHFinance module [{ticker}/{module}]: {e}')
        return None

    def _v2_to_annual_list(
        self,
        v2_data: Optional[dict],
        field_map: dict,
        max_years: int = 4,
    ) -> list:
        """
        Convert YH Finance v2 flat dict to Yahoo-style list of annual dicts.

        v2_data  — {field_name: {'TTM': val, '2025-09-27': val, ...}}
        field_map — {yh_v2_field: yahoo_scorer_field}
        Returns  — newest-first list of {endDate: {raw: unix_ts}, field: {raw: val}, ...}
        """
        if not v2_data:
            return []
        # Collect all YYYY-MM-DD keys (skip 'TTM')
        date_keys: set = set()
        for field_vals in v2_data.values():
            if isinstance(field_vals, dict):
                date_keys.update(
                    k for k in field_vals
                    if isinstance(k, str) and len(k) == 10 and k[4] == '-'
                )
        if not date_keys:
            return []
        sorted_dates = sorted(date_keys, reverse=True)[:max_years]
        result = []
        for date_str in sorted_dates:
            try:
                dt = _date.fromisoformat(date_str)
                ts = int(calendar.timegm(dt.timetuple()))
            except Exception:
                ts = 0
            row: dict = {'endDate': {'raw': ts, 'fmt': date_str}}
            for yh_field, yahoo_field in field_map.items():
                field_data = v2_data.get(yh_field)
                if isinstance(field_data, dict):
                    val = field_data.get(date_str)
                    if val is not None:
                        row[yahoo_field] = {'raw': val}
            result.append(row)
        return result

    def quote_summary(self, symbol: str) -> Optional[dict]:
        """
        Fetch and adapt YH Finance data to Yahoo quoteSummary format for scorer.py.
        Fetches standard + v2 modules concurrently; adapts v2 to Yahoo list format.
        Returns None if financial-data and default-key-statistics are both empty.
        """
        all_modules = [
            'financial-data',
            'default-key-statistics',
            'income-statement',
            'asset-profile',
            'income-statement-v2',
            'cashflow-statement-v2',
            'balance-sheet-v2',
        ]

        raw: dict = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            futures = {ex.submit(self._fetch_module, symbol, m): m for m in all_modules}
            for future in concurrent.futures.as_completed(futures):
                mod = futures[future]
                try:
                    raw[mod] = future.result()
                except Exception as e:
                    print(f'  YHFinance [{symbol}/{mod}] thread: {e}')
                    raw[mod] = None

        fin        = raw.get('financial-data') or {}
        stats      = raw.get('default-key-statistics') or {}
        income_std = raw.get('income-statement') or {}
        profile    = raw.get('asset-profile') or {}
        income_v2  = raw.get('income-statement-v2') or {}
        cf_v2      = raw.get('cashflow-statement-v2') or {}
        bs_v2      = raw.get('balance-sheet-v2') or {}

        if not fin and not stats:
            return None

        # ── income-statement-v2 → incomeStatementHistory ──────────────────────
        inc_field_map = {
            'revenue':         'totalRevenue',
            'grossProfit':     'grossProfit',
            'netIncome':       'netIncome',
            'interestExpense': 'interestExpense',
        }
        inc_rows = self._v2_to_annual_list(income_v2, inc_field_map)
        # ebit maps to both 'ebit' and 'operatingIncome' in scorer
        if income_v2:
            ebit_data = income_v2.get('ebit', {})
            for row in inc_rows:
                date_str = row.get('endDate', {}).get('fmt', '')
                val = ebit_data.get(date_str) if isinstance(ebit_data, dict) else None
                if val is not None:
                    row['ebit'] = {'raw': val}
                    row['operatingIncome'] = {'raw': val}

        # ── cashflow-statement-v2 → cashflowStatementHistory ──────────────────
        cf_field_map = {
            'ncfo':  'totalCashFromOperatingActivities',
            'capex': 'capitalExpenditures',
            'cash_flow_statement_depreciation_and_amortization': 'depreciation',
            'commonrepurchased': 'repurchaseOfStock',
        }
        cf_rows = self._v2_to_annual_list(cf_v2, cf_field_map)

        # ── balance-sheet-v2 → balanceSheetHistory ────────────────────────────
        bs_field_map = {
            'equity':       'totalStockholderEquity',
            'assetsc':      'totalCurrentAssets',
            'liabilitiesc': 'totalCurrentLiabilities',
            'liabilities':  'totalLiab',
            'debt':         'longTermDebt',
            'assets':       'totalAssets',
        }
        bs_rows = self._v2_to_annual_list(bs_v2, bs_field_map)
        # scorer accesses both field names for equity
        for row in bs_rows:
            if 'totalStockholderEquity' in row:
                row['stockholdersEquity'] = row['totalStockholderEquity']

        # ── Fallback: standard modules for ASX stocks (v2 returns empty) ──────────
        if not inc_rows:
            fallback = income_std.get('incomeStatementHistory')
            if isinstance(fallback, list):
                inc_rows = fallback
            elif isinstance(fallback, dict):
                inc_rows = fallback.get('incomeStatementHistory') or []

        if not bs_rows:
            bs_std = self._fetch_module(symbol, 'balance-sheet')
            if bs_std:
                fallback = bs_std.get('balanceSheetHistory')
                if isinstance(fallback, list):
                    bs_rows = fallback
                elif isinstance(fallback, dict):
                    bs_rows = fallback.get('balanceSheetStatements') or []
                for row in bs_rows:
                    if 'totalStockholderEquity' in row:
                        row['stockholdersEquity'] = row['totalStockholderEquity']

        if not cf_rows:
            cf_std = self._fetch_module(symbol, 'cash-flow-statement')
            if cf_std:
                fallback = cf_std.get('cashflowStatementHistory')
                if isinstance(fallback, list):
                    cf_rows = fallback
                elif isinstance(fallback, dict):
                    cf_rows = fallback.get('cashflowStatements') or []

        # ── price / summaryDetail from fin + stats ────────────────────────────
        long_name = profile.get('longName') or ''
        price_dict = {
            'shortName':  long_name,
            'longName':   long_name,
            'symbol':     symbol,
            'marketCap':  stats.get('marketCap'),
            'currency':   fin.get('financialCurrency') or '',
        }
        summary_detail = {
            'trailingPE':                  stats.get('trailingPE'),
            'dividendYield':               stats.get('dividendYield'),
            'trailingAnnualDividendYield': stats.get('dividendYield'),
            'priceToBook':                 stats.get('priceToBook'),
        }

        return {
            'financialData':           fin,
            'defaultKeyStatistics':    stats,
            'price':                   price_dict,
            'summaryDetail':           summary_detail,
            'summaryProfile':          profile,
            'incomeStatementHistory':  {'incomeStatementHistory': inc_rows},
            'cashflowStatementHistory': {'cashflowStatements': cf_rows},
            'balanceSheetHistory':     {'balanceSheetStatements': bs_rows},
        }

    def chart(self, symbol: str) -> Optional[dict]:
        """Fetch current quote for an index or stock. Returns Yahoo-compatible dict."""
        try:
            encoded = urllib.parse.quote(symbol)
            url = f'{_RAPID_BASE}/api/yahoo/qu/quote/{encoded}'
            r = self.session.get(url, headers=_RAPID_HEADERS, timeout=_TIMEOUT)
            if r.status_code != 200:
                return None
            data = r.json()
            body = data.get('body')
            q = body[0] if isinstance(body, list) and body else (body if isinstance(body, dict) else None)
            if q is None:
                return None
            # Expose regularMarketPreviousClose as chartPreviousClose for main.py
            if 'regularMarketPreviousClose' in q and 'chartPreviousClose' not in q:
                q = {**q, 'chartPreviousClose': q['regularMarketPreviousClose']}
            return q
        except Exception as e:
            print(f'  YHFinance chart [{symbol}]: {e}')
        return None

    def price_trend_check(self, symbol: str) -> Optional[tuple]:
        """Return (price_2yr_ago, current_price) from a 2yr monthly history call."""
        try:
            encoded = urllib.parse.quote(symbol)
            url = (f'{_RAPID_BASE}/api/v2/markets/stock/history'
                   f'?symbol={encoded}&interval=1mo')
            r = self.session.get(url, headers=_RAPID_HEADERS, timeout=_TIMEOUT)
            if r.status_code == 429:
                time.sleep(_RETRY_DELAY)
                r = self.session.get(url, headers=_RAPID_HEADERS, timeout=_TIMEOUT)
            if r.status_code != 200:
                return None
            data = r.json()
            body = data.get('body')
            if not isinstance(body, list) or len(body) < 2:
                return None
            # body is sorted oldest-first; take last 24 months
            slice_24 = body[-24:] if len(body) >= 24 else body
            old_close = next(
                (float(e['close']) for e in slice_24 if e.get('close') is not None),
                None,
            )
            current_close = next(
                (float(e['close']) for e in reversed(slice_24) if e.get('close') is not None),
                None,
            )
            if old_close is None or current_close is None:
                return None
            return (old_close, current_close)
        except Exception as e:
            print(f'  YHFinance price trend [{symbol}]: {e}')
        return None


# ── SEC EDGAR ─────────────────────────────────────────────────────────────────

class EdgarFetcher:
    """
    Free, unlimited fallback for US balance sheet and income statement data.
    Uses official SEC EDGAR XBRL API — no API key required.
    Call warm_up() once at pipeline start to load the ticker→CIK map.
    """
    _TICKER_URL = 'https://www.sec.gov/files/company_tickers.json'
    _FACTS_BASE = 'https://data.sec.gov/api/xbrl/companyfacts'
    _HEADERS    = {
        'User-Agent': 'GlobalValuePipeline/1.0 andrew.hayes.australia@gmail.com',
        'Accept-Encoding': 'gzip, deflate, br',
    }

    def __init__(self):
        self.session  = requests.Session()
        self._cik_map: dict = {}

    def warm_up(self):
        """Load ticker→CIK mapping. Call once at pipeline start."""
        try:
            r = self.session.get(self._TICKER_URL, headers=self._HEADERS, timeout=30)
            if r.status_code == 200:
                self._cik_map = {
                    str(v['ticker']).upper(): str(v['cik_str']).zfill(10)
                    for v in r.json().values()
                }
                print(f'  EDGAR: {len(self._cik_map)} tickers loaded')
            else:
                print(f'  EDGAR warm-up: HTTP {r.status_code}')
        except Exception as e:
            print(f'  EDGAR warm-up failed: {e}')

    def get_stock_data(self, ticker: str) -> Optional[dict]:
        """
        Return dict of annual XBRL values for the fields needed by enrich_from_edgar.
        Each value is a list (newest annual period first). Returns None on failure.
        """
        cik = self._cik_map.get(ticker.upper())
        if not cik:
            return None
        try:
            r = self.session.get(
                f'{self._FACTS_BASE}/CIK{cik}.json',
                headers=self._HEADERS,
                timeout=30,
            )
            if r.status_code != 200:
                return None
            gaap = (r.json().get('facts') or {}).get('us-gaap') or {}

            def annual_vals(concepts: list, n: int = 2) -> list:
                for concept in concepts:
                    data = gaap.get(concept)
                    if not data:
                        continue
                    entries = (data.get('units') or {}).get('USD') or []
                    fy_entries = sorted(
                        [e for e in entries
                         if e.get('form') == '10-K' and e.get('fp') in ('FY', 'Q4')],
                        key=lambda x: x.get('end', ''),
                        reverse=True,
                    )
                    seen, result = set(), []
                    for e in fy_entries:
                        fy = e.get('fy')
                        if fy and fy not in seen:
                            seen.add(fy)
                            result.append(float(e['val']))
                            if len(result) >= n:
                                break
                    if result:
                        return result
                return []

            return {
                'interest_expense': annual_vals([
                    'InterestExpense', 'InterestExpenseBorrowings',
                    'InterestExpenseDebt', 'InterestExpenseShortTermBorrowings',
                ], 1),
                'operating_income': annual_vals(['OperatingIncomeLoss'], 1),
                'ebt': annual_vals([
                    'IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest',
                    'IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments',
                ], 1),
                'equity': annual_vals([
                    'StockholdersEquity',
                    'StockholdersEquityAttributableToParent',
                    'StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest',
                    'CommonStockholdersEquity',
                ], 2),
                'current_assets':    annual_vals(['AssetsCurrent'], 1),
                'total_liabilities': annual_vals(['Liabilities'], 1),
                'total_assets': annual_vals(['Assets', 'LiabilitiesAndStockholdersEquity'], 1),
            }
        except Exception as e:
            print(f'  EDGAR [{ticker}]: {e}')
        return None


# ── FMP ───────────────────────────────────────────────────────────────────────

class FmpFetcher:
    _BASE = 'https://financialmodelingprep.com/stable'

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session = requests.Session()

    def index_quote(self, symbol: str) -> Optional[dict]:
        if not self.api_key or not symbol:
            return None
        try:
            r = self.session.get(
                f'{self._BASE}/quote?symbol={symbol}&apikey={self.api_key}',
                timeout=_TIMEOUT,
            )
            if r.status_code != 200:
                return None
            data = r.json()
            if isinstance(data, list) and data:
                return data[0]
        except Exception as e:
            print(f'  FMP index quote [{symbol}]: {e}')
        return None

    def ratios(self, ticker: str, limit: int = 2) -> list:
        """Return list of annual ratio dicts (newest first). Two periods needed for book value growth."""
        if not self.api_key:
            return []
        try:
            r = self.session.get(
                f'{self._BASE}/ratios?symbol={ticker}&limit={limit}&apikey={self.api_key}',
                timeout=_TIMEOUT,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            if isinstance(data, list):
                return data[:limit]
        except Exception as e:
            print(f'  FMP ratios [{ticker}]: {e}')
        return []

    def key_metrics(self, ticker: str, limit: int = 1) -> list:
        """Return list of annual key-metric dicts. Provides netCurrentAssetValue for net-net ratio."""
        if not self.api_key:
            return []
        try:
            r = self.session.get(
                f'{self._BASE}/key-metrics?symbol={ticker}&period=annual'
                f'&limit={limit}&apikey={self.api_key}',
                timeout=_TIMEOUT,
            )
            if r.status_code != 200:
                return []
            data = r.json()
            if isinstance(data, list):
                return data[:limit]
        except Exception as e:
            print(f'  FMP key metrics [{ticker}]: {e}')
        return []


# ── XAO data source ───────────────────────────────────────────────────────────

def fetch_xao(session: Optional[requests.Session] = None) -> Optional[dict]:
    """Fetch the XAO constituent list from the AndrewHayes-Dev/XAO-data GitHub repo."""
    url = 'https://raw.githubusercontent.com/AndrewHayes-Dev/XAO-data/main/data/xao.json'
    try:
        s = session or requests.Session()
        r = s.get(url, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f'  XAO fetch failed: {e}')
    return None


# ── Wikipedia constituent scraper ─────────────────────────────────────────────



# ── IWM constituent fetcher ───────────────────────────────────────────────────

_IWM_PRODUCT_URL = 'https://www.ishares.com/us/products/239710/ishares-russell-2000-etf'
_IWM_CSV_URL     = (
    'https://www.ishares.com/us/products/239710/ishares-russell-2000-etf'
    '?fileType=csv&fileName=IWM_holdings&dataType=fund'
)
_IWM_HEADERS = {
    'User-Agent':      ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/125.0.0.0 Safari/537.36'),
    'Accept':          ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                        'image/avif,image/webp,*/*;q=0.8'),
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer':         'https://www.ishares.com/us/products/239710/ishares-russell-2000-etf',
    'DNT':             '1',
    'Connection':      'keep-alive',
}


def fetch_iwm_constituents(top_n: int = 150) -> list:
    """
    Downloads the IWM holdings CSV from BlackRock iShares.
    Pre-fetches the product page to acquire session cookies, uses browser headers
    to avoid 403. Filters to equity rows only and returns the top_n by market value.
    Returns list of dicts: {ticker, company_name, gics_sector, market_value}
    Raises on any fetch or parse failure (no fallback by design).
    """
    import csv as _csv

    session = requests.Session()

    # Visit the product page first to acquire session cookies
    pre = session.get(_IWM_PRODUCT_URL, headers=_IWM_HEADERS, timeout=30)
    pre.raise_for_status()
    time.sleep(1.5)

    # Download the holdings CSV
    r = session.get(_IWM_CSV_URL, headers=_IWM_HEADERS, timeout=60)
    r.raise_for_status()

    # Locate the data header row — iShares prepends fund metadata rows before it
    lines = r.text.splitlines()
    header_idx = next(
        (i for i, line in enumerate(lines) if line.startswith('Ticker')),
        None,
    )
    if header_idx is None:
        raise ValueError('IWM CSV: could not locate Ticker header row')

    # Parse from the header row onward; DictReader handles quoted commas in numbers
    reader = _csv.DictReader(lines[header_idx:])
    holdings = []
    for row in reader:
        if row.get('Asset Class', '').strip() != 'Equity':
            continue
        ticker = row.get('Ticker', '').strip()
        if not ticker or ticker == '-':
            continue
        try:
            market_value = float(row.get('Market Value', '0').replace(',', ''))
        except (ValueError, AttributeError):
            continue
        holdings.append({
            'ticker':       ticker,
            'company_name': row.get('Name', '').strip(),
            'gics_sector':  row.get('Sector', '').strip(),
            'market_value': market_value,
        })

    holdings.sort(key=lambda x: x['market_value'], reverse=True)
    result = holdings[:top_n]
    print(f'  IWM: {len(result)} of {len(holdings)} equity holdings selected (top {top_n} by market value)')
    return result

class WikipediaScraper:
    """
    Scrapes S&P 500, DJIA, and NASDAQ-100 constituents from Wikipedia monthly.
    Returns list of dicts: {ticker, company_name, gics_sector, gics_industry}
    """

    _WIKI_HEADERS = {
        'User-Agent': (
            'Mozilla/5.0 (compatible; GlobalValuePipeline/1.0; '
            '+https://github.com/AndrewHayes-Dev/Global-value-pipeline)'
        )
    }

    def __init__(self):
        self.session = requests.Session()

    def _fetch_tables(self, url: str) -> list:
        try:
            import pandas as pd
            from io import StringIO
            r = self.session.get(url, headers=self._WIKI_HEADERS, timeout=30)
            if r.status_code != 200:
                print(f'  Wikipedia fetch failed [{url}]: HTTP {r.status_code}')
                return []
            return pd.read_html(StringIO(r.text))
        except Exception as e:
            print(f'  Wikipedia parse error [{url}]: {e}')
            return []

    def sp500(self) -> list:
        """Returns all S&P 500 constituents with GICS sector and sub-industry."""
        tables = self._fetch_tables(
            'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        )
        if not tables:
            return []
        try:
            df = tables[0]
            results = []
            for _, row in df.iterrows():
                ticker = str(row.get('Symbol', '')).strip().replace('.', '-')
                name   = str(row.get('Security', '')).strip()
                sector = str(row.get('GICS Sector', '')).strip()
                industry = str(row.get('GICS Sub-Industry', '')).strip()
                if ticker and sector:
                    results.append({
                        'ticker':        ticker,
                        'company_name':  name,
                        'gics_sector':   sector,
                        'gics_industry': industry,
                    })
            print(f'  Wikipedia S&P 500: {len(results)} constituents')
            return results
        except Exception as e:
            print(f'  S&P 500 parse error: {e}')
            return []

    def djia(self) -> list:
        """Returns all 30 DJIA constituents."""
        tables = self._fetch_tables(
            'https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average'
        )
        if not tables:
            return []
        try:
            for df in tables:
                sym_col  = next((c for c in df.columns if 'symbol' in str(c).lower()), None)
                name_col = next((c for c in df.columns if 'company' in str(c).lower()), None)
                ind_col  = next((c for c in df.columns if 'industry' in str(c).lower()), None)
                if sym_col is None:
                    continue
                results = []
                for _, row in df.iterrows():
                    ticker = str(row.get(sym_col, '')).strip().replace('.', '-')
                    name   = str(row.get(name_col, ticker)).strip() if name_col else ticker
                    industry = str(row.get(ind_col, '')).strip() if ind_col else ''
                    if ticker and len(ticker) <= 6 and ticker.isalpha():
                        results.append({
                            'ticker':        ticker,
                            'company_name':  name,
                            'gics_sector':   _djia_sector_lookup(ticker),
                            'gics_industry': industry,
                        })
                if results:
                    print(f'  Wikipedia DJIA: {len(results)} constituents')
                    return results
        except Exception as e:
            print(f'  DJIA parse error: {e}')
        print('  DJIA Wikipedia parse failed — using static fallback')
        return _DJIA_FALLBACK

    def nasdaq100(self) -> list:
        """Returns NASDAQ-100 constituents."""
        tables = self._fetch_tables(
            'https://en.wikipedia.org/wiki/Nasdaq-100'
        )
        if not tables:
            return []
        _ICB_TO_GICS = {
            'Technology':         'Information Technology',
            'Telecommunications': 'Communication Services',
            'Basic Materials':    'Materials',
        }
        try:
            for df in tables:
                sym_col  = next((c for c in df.columns if 'ticker' in str(c).lower() or
                                 'symbol' in str(c).lower()), None)
                name_col = next((c for c in df.columns if 'company' in str(c).lower()), None)
                ind_col  = next((c for c in df.columns if 'industry' in str(c).lower()), None)
                sec_col  = next((c for c in df.columns if 'sector' in str(c).lower()), None)
                classify_col = ind_col or sec_col
                if sym_col is None:
                    continue
                results = []
                for _, row in df.iterrows():
                    ticker     = str(row.get(sym_col, '')).strip().replace('.', '-')
                    name       = str(row.get(name_col, ticker)).strip() if name_col else ticker
                    raw_sector = str(row.get(classify_col, '')).strip() if classify_col else ''
                    sector     = _ICB_TO_GICS.get(raw_sector, raw_sector)
                    if ticker and len(ticker) <= 6:
                        results.append({
                            'ticker':        ticker,
                            'company_name':  name,
                            'gics_sector':   sector,
                            'gics_industry': '',
                        })
                if results:
                    print(f'  Wikipedia NASDAQ-100: {len(results)} constituents')
                    return results
        except Exception as e:
            print(f'  NASDAQ-100 parse error: {e}')
        return []


def _djia_sector_lookup(ticker: str) -> str:
    """Fallback GICS sector for DJIA tickers."""
    _MAP = {
        'AAPL': 'Information Technology', 'CSCO': 'Information Technology',
        'IBM':  'Information Technology', 'MSFT': 'Information Technology',
        'NVDA': 'Information Technology', 'CRM':  'Information Technology',
        'AXP':  'Financials',  'GS':  'Financials', 'JPM': 'Financials',
        'TRV':  'Financials',  'V':   'Financials',
        'AMGN': 'Health Care', 'JNJ': 'Health Care', 'MRK': 'Health Care',
        'UNH':  'Health Care',
        'MMM':  'Industrials', 'BA':  'Industrials', 'CAT': 'Industrials',
        'HON':  'Industrials',
        'AMZN': 'Consumer Discretionary', 'HD': 'Consumer Discretionary',
        'MCD':  'Consumer Discretionary', 'NKE': 'Consumer Discretionary',
        'KO':   'Consumer Staples', 'PG': 'Consumer Staples', 'WMT': 'Consumer Staples',
        'DIS':  'Communication Services', 'VZ': 'Communication Services',
        'CVX':  'Energy', 'XOM': 'Energy',
        'SHW':  'Materials',
    }
    return _MAP.get(ticker, 'Industrials')


# Static DJIA fallback with GICS sectors
_DJIA_FALLBACK = [
    {'ticker': 'AAPL', 'company_name': 'Apple Inc.',                  'gics_sector': 'Information Technology', 'gics_industry': 'Technology Hardware, Storage & Peripherals'},
    {'ticker': 'AMGN', 'company_name': 'Amgen Inc.',                  'gics_sector': 'Health Care',            'gics_industry': 'Biotechnology'},
    {'ticker': 'AMZN', 'company_name': 'Amazon.com Inc.',             'gics_sector': 'Consumer Discretionary', 'gics_industry': 'Broadline Retail'},
    {'ticker': 'AXP',  'company_name': 'American Express Co.',        'gics_sector': 'Financials',             'gics_industry': 'Consumer Finance'},
    {'ticker': 'BA',   'company_name': 'Boeing Co.',                  'gics_sector': 'Industrials',            'gics_industry': 'Aerospace & Defense'},
    {'ticker': 'CAT',  'company_name': 'Caterpillar Inc.',            'gics_sector': 'Industrials',            'gics_industry': 'Construction Machinery & Heavy Transportation Equipment'},
    {'ticker': 'CRM',  'company_name': 'Salesforce Inc.',             'gics_sector': 'Information Technology', 'gics_industry': 'Software'},
    {'ticker': 'CSCO', 'company_name': 'Cisco Systems Inc.',          'gics_sector': 'Information Technology', 'gics_industry': 'Communications Equipment'},
    {'ticker': 'CVX',  'company_name': 'Chevron Corp.',               'gics_sector': 'Energy',                 'gics_industry': 'Integrated Oil & Gas'},
    {'ticker': 'DIS',  'company_name': 'Walt Disney Co.',             'gics_sector': 'Communication Services', 'gics_industry': 'Movies & Entertainment'},
    {'ticker': 'GS',   'company_name': 'Goldman Sachs Group Inc.',    'gics_sector': 'Financials',             'gics_industry': 'Investment Banking & Brokerage'},
    {'ticker': 'HD',   'company_name': 'Home Depot Inc.',             'gics_sector': 'Consumer Discretionary', 'gics_industry': 'Home Improvement Retail'},
    {'ticker': 'HON',  'company_name': 'Honeywell International Inc.','gics_sector': 'Industrials',            'gics_industry': 'Industrial Conglomerates'},
    {'ticker': 'IBM',  'company_name': 'IBM Corp.',                   'gics_sector': 'Information Technology', 'gics_industry': 'IT Consulting & Other Services'},
    {'ticker': 'JNJ',  'company_name': 'Johnson & Johnson',           'gics_sector': 'Health Care',            'gics_industry': 'Pharmaceuticals'},
    {'ticker': 'JPM',  'company_name': 'JPMorgan Chase & Co.',        'gics_sector': 'Financials',             'gics_industry': 'Diversified Banks'},
    {'ticker': 'KO',   'company_name': 'Coca-Cola Co.',               'gics_sector': 'Consumer Staples',       'gics_industry': 'Soft Drinks & Non-alcoholic Beverages'},
    {'ticker': 'MCD',  'company_name': "McDonald's Corp.",            'gics_sector': 'Consumer Discretionary', 'gics_industry': 'Restaurants'},
    {'ticker': 'MMM',  'company_name': '3M Co.',                      'gics_sector': 'Industrials',            'gics_industry': 'Industrial Conglomerates'},
    {'ticker': 'MRK',  'company_name': 'Merck & Co. Inc.',            'gics_sector': 'Health Care',            'gics_industry': 'Pharmaceuticals'},
    {'ticker': 'MSFT', 'company_name': 'Microsoft Corp.',             'gics_sector': 'Information Technology', 'gics_industry': 'Systems Software'},
    {'ticker': 'NKE',  'company_name': 'Nike Inc.',                   'gics_sector': 'Consumer Discretionary', 'gics_industry': 'Apparel, Accessories & Luxury Goods'},
    {'ticker': 'NVDA', 'company_name': 'NVIDIA Corp.',                'gics_sector': 'Information Technology', 'gics_industry': 'Semiconductors'},
    {'ticker': 'PG',   'company_name': 'Procter & Gamble Co.',        'gics_sector': 'Consumer Staples',       'gics_industry': 'Personal Care Products'},
    {'ticker': 'SHW',  'company_name': 'Sherwin-Williams Co.',        'gics_sector': 'Materials',              'gics_industry': 'Specialty Chemicals'},
    {'ticker': 'TRV',  'company_name': 'Travelers Companies Inc.',    'gics_sector': 'Financials',             'gics_industry': 'Property & Casualty Insurance'},
    {'ticker': 'UNH',  'company_name': 'UnitedHealth Group Inc.',     'gics_sector': 'Health Care',            'gics_industry': 'Managed Health Care'},
    {'ticker': 'V',    'company_name': 'Visa Inc.',                   'gics_sector': 'Financials',             'gics_industry': 'Transaction & Payment Processing Services'},
    {'ticker': 'VZ',   'company_name': 'Verizon Communications Inc.', 'gics_sector': 'Communication Services', 'gics_industry': 'Integrated Telecommunication Services'},
    {'ticker': 'WMT',  'company_name': 'Walmart Inc.',                'gics_sector': 'Consumer Staples',       'gics_industry': 'Consumer Staples Merchandise Retail'},
]

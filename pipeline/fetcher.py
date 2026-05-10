# pipeline/fetcher.py
# Yahoo Finance + FMP HTTP clients, Wikipedia constituent scraper, XAO fetcher.

import time
import urllib.parse
from typing import Optional

import requests

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

_SUMMARY_MODULES = (
    'price,financialData,defaultKeyStatistics,summaryDetail,'
    'incomeStatementHistory,cashflowStatementHistory,balanceSheetHistory,'
    'balanceSheetHistoryQuarterly,summaryProfile'
)

_YAHOO_BASES = [
    'https://query1.finance.yahoo.com',
    'https://query2.finance.yahoo.com',
]

_TIMEOUT      = 30
_RETRY_DELAY  = 2


# ── Yahoo Finance ─────────────────────────────────────────────────────────────

class YahooFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.crumb: Optional[str] = None
        self._init_session()

    def _init_session(self):
        try:
            self.session.get('https://fc.yahoo.com', headers=_BASE_HEADERS, timeout=_TIMEOUT)
        except Exception:
            try:
                self.session.get('https://finance.yahoo.com/', headers=_BASE_HEADERS, timeout=_TIMEOUT)
            except Exception:
                pass
        try:
            r = self.session.get(
                f'{_YAHOO_BASES[0]}/v1/test/getcrumb',
                headers=_BASE_HEADERS,
                timeout=_TIMEOUT,
            )
            if r.status_code == 200 and r.text and not r.text.startswith('{'):
                self.crumb = r.text.strip()
                print(f'  Yahoo session OK (crumb acquired)')
            else:
                print(f'  Yahoo session: no crumb (status {r.status_code})')
        except Exception as e:
            print(f'  Yahoo session init failed: {e}')

    def _reset_session(self):
        self.crumb = None
        self._init_session()

    def quote_summary(self, symbol: str) -> Optional[dict]:
        crumb_param = f'&crumb={urllib.parse.quote(self.crumb)}' if self.crumb else ''
        for attempt in range(2):
            for base in _YAHOO_BASES:
                try:
                    url = (
                        f'{base}/v10/finance/quoteSummary/{urllib.parse.quote(symbol)}'
                        f'?modules={_SUMMARY_MODULES}{crumb_param}'
                    )
                    r = self.session.get(url, headers=_BASE_HEADERS, timeout=_TIMEOUT)
                    if r.status_code in (401, 403):
                        self._reset_session()
                        crumb_param = f'&crumb={urllib.parse.quote(self.crumb)}' if self.crumb else ''
                        continue
                    if r.status_code == 429:
                        print(f'  Yahoo rate limit [{symbol}] — waiting {_RETRY_DELAY}s')
                        time.sleep(_RETRY_DELAY)
                        continue
                    if r.status_code != 200:
                        continue
                    data = r.json()
                    if data.get('quoteSummary', {}).get('error'):
                        continue
                    result = data.get('quoteSummary', {}).get('result') or []
                    if result:
                        return result[0]
                except Exception as e:
                    print(f'  Yahoo summary [{symbol}/{base}]: {e}')
        return None

    def chart(self, symbol: str) -> Optional[dict]:
        try:
            encoded = urllib.parse.quote(symbol)
            url = f'{_YAHOO_BASES[0]}/v8/finance/chart/{encoded}?interval=1d&range=1d'
            r = self.session.get(url, headers=_BASE_HEADERS, timeout=_TIMEOUT)
            if r.status_code != 200:
                return None
            data = r.json()
            result = (data.get('chart') or {}).get('result') or []
            if result:
                return result[0].get('meta')
        except Exception as e:
            print(f'  Yahoo chart [{symbol}]: {e}')
        return None

    def price_trend_check(self, symbol: str) -> Optional[tuple[float, float]]:
        """Return (price_2yr_ago, current_price) from a single 2yr monthly chart call."""
        try:
            encoded = urllib.parse.quote(symbol)
            url = (
                f'{_YAHOO_BASES[0]}/v8/finance/chart/{encoded}'
                f'?interval=1mo&range=2y'
            )
            r = self.session.get(url, headers=_BASE_HEADERS, timeout=_TIMEOUT)
            if r.status_code == 429:
                time.sleep(_RETRY_DELAY)
                r = self.session.get(url, headers=_BASE_HEADERS, timeout=_TIMEOUT)
            if r.status_code != 200:
                return None
            data = r.json()
            result = (data.get('chart') or {}).get('result') or []
            if not result:
                return None
            # Current price is in the meta object of every chart response
            meta = result[0].get('meta') or {}
            current = meta.get('regularMarketPrice')
            closes = (result[0].get('indicators') or {}).get('quote', [{}])[0].get('close') or []
            # First non-null close is ~2 years ago (closes are oldest-first)
            old_price = next((float(c) for c in closes if c is not None), None)
            if current is None or old_price is None:
                return None
            return (old_price, float(current))
        except Exception as e:
            print(f'  Yahoo price trend [{symbol}]: {e}')
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

    def sp500(self) -> list[dict]:
        """Returns all S&P 500 constituents with GICS sector and sub-industry."""
        tables = self._fetch_tables(
            'https://en.wikipedia.org/wiki/List_of_S%26P_500_companies'
        )
        if not tables:
            return []
        try:
            df = tables[0]
            # Columns: Symbol, Security, GICS Sector, GICS Sub-Industry, ...
            results = []
            for _, row in df.iterrows():
                ticker = str(row.get('Symbol', '')).strip().replace('.', '-')
                name   = str(row.get('Security', '')).strip()
                sector = str(row.get('GICS Sector', '')).strip()
                industry = str(row.get('GICS Sub-Industry', '')).strip()
                if ticker and sector:
                    results.append({
                        'ticker':       ticker,
                        'company_name': name,
                        'gics_sector':  sector,
                        'gics_industry': industry,
                    })
            print(f'  Wikipedia S&P 500: {len(results)} constituents')
            return results
        except Exception as e:
            print(f'  S&P 500 parse error: {e}')
            return []

    def djia(self) -> list[dict]:
        """Returns all 30 DJIA constituents."""
        tables = self._fetch_tables(
            'https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average'
        )
        if not tables:
            return []
        try:
            # The DJIA Wikipedia page has the components in a table
            # Try each table until we find one with a Symbol/Ticker column
            for df in tables:
                cols = [str(c).lower() for c in df.columns]
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
                            'ticker':       ticker,
                            'company_name': name,
                            'gics_sector':  _djia_sector_lookup(ticker),
                            'gics_industry': industry,
                        })
                if results:
                    print(f'  Wikipedia DJIA: {len(results)} constituents')
                    return results
        except Exception as e:
            print(f'  DJIA parse error: {e}')
        # Hard fallback — DJIA is stable enough for a static list
        print('  DJIA Wikipedia parse failed — using static fallback')
        return _DJIA_FALLBACK

    def nasdaq100(self) -> list[dict]:
        """Returns NASDAQ-100 constituents."""
        tables = self._fetch_tables(
            'https://en.wikipedia.org/wiki/Nasdaq-100'
        )
        if not tables:
            return []
        # Wikipedia NASDAQ-100 uses ICB Industry classification, not GICS.
        # Map the differing names to their GICS equivalents.
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
                # Prefer the broader Industry column over Subsector
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
    {'ticker': 'AAPL', 'company_name': 'Apple Inc.',                 'gics_sector': 'Information Technology', 'gics_industry': 'Technology Hardware, Storage & Peripherals'},
    {'ticker': 'AMGN', 'company_name': 'Amgen Inc.',                 'gics_sector': 'Health Care',            'gics_industry': 'Biotechnology'},
    {'ticker': 'AMZN', 'company_name': 'Amazon.com Inc.',            'gics_sector': 'Consumer Discretionary', 'gics_industry': 'Broadline Retail'},
    {'ticker': 'AXP',  'company_name': 'American Express Co.',       'gics_sector': 'Financials',             'gics_industry': 'Consumer Finance'},
    {'ticker': 'BA',   'company_name': 'Boeing Co.',                 'gics_sector': 'Industrials',            'gics_industry': 'Aerospace & Defense'},
    {'ticker': 'CAT',  'company_name': 'Caterpillar Inc.',           'gics_sector': 'Industrials',            'gics_industry': 'Construction Machinery & Heavy Transportation Equipment'},
    {'ticker': 'CRM',  'company_name': 'Salesforce Inc.',            'gics_sector': 'Information Technology', 'gics_industry': 'Software'},
    {'ticker': 'CSCO', 'company_name': 'Cisco Systems Inc.',         'gics_sector': 'Information Technology', 'gics_industry': 'Communications Equipment'},
    {'ticker': 'CVX',  'company_name': 'Chevron Corp.',              'gics_sector': 'Energy',                 'gics_industry': 'Integrated Oil & Gas'},
    {'ticker': 'DIS',  'company_name': 'Walt Disney Co.',            'gics_sector': 'Communication Services', 'gics_industry': 'Movies & Entertainment'},
    {'ticker': 'GS',   'company_name': 'Goldman Sachs Group Inc.',   'gics_sector': 'Financials',             'gics_industry': 'Investment Banking & Brokerage'},
    {'ticker': 'HD',   'company_name': 'Home Depot Inc.',            'gics_sector': 'Consumer Discretionary', 'gics_industry': 'Home Improvement Retail'},
    {'ticker': 'HON',  'company_name': 'Honeywell International Inc.','gics_sector': 'Industrials',           'gics_industry': 'Industrial Conglomerates'},
    {'ticker': 'IBM',  'company_name': 'IBM Corp.',                  'gics_sector': 'Information Technology', 'gics_industry': 'IT Consulting & Other Services'},
    {'ticker': 'JNJ',  'company_name': 'Johnson & Johnson',          'gics_sector': 'Health Care',            'gics_industry': 'Pharmaceuticals'},
    {'ticker': 'JPM',  'company_name': 'JPMorgan Chase & Co.',       'gics_sector': 'Financials',             'gics_industry': 'Diversified Banks'},
    {'ticker': 'KO',   'company_name': 'Coca-Cola Co.',              'gics_sector': 'Consumer Staples',       'gics_industry': 'Soft Drinks & Non-alcoholic Beverages'},
    {'ticker': 'MCD',  'company_name': "McDonald's Corp.",           'gics_sector': 'Consumer Discretionary', 'gics_industry': 'Restaurants'},
    {'ticker': 'MMM',  'company_name': '3M Co.',                     'gics_sector': 'Industrials',            'gics_industry': 'Industrial Conglomerates'},
    {'ticker': 'MRK',  'company_name': 'Merck & Co. Inc.',           'gics_sector': 'Health Care',            'gics_industry': 'Pharmaceuticals'},
    {'ticker': 'MSFT', 'company_name': 'Microsoft Corp.',            'gics_sector': 'Information Technology', 'gics_industry': 'Systems Software'},
    {'ticker': 'NKE',  'company_name': 'Nike Inc.',                  'gics_sector': 'Consumer Discretionary', 'gics_industry': 'Apparel, Accessories & Luxury Goods'},
    {'ticker': 'NVDA', 'company_name': 'NVIDIA Corp.',               'gics_sector': 'Information Technology', 'gics_industry': 'Semiconductors'},
    {'ticker': 'PG',   'company_name': 'Procter & Gamble Co.',       'gics_sector': 'Consumer Staples',       'gics_industry': 'Personal Care Products'},
    {'ticker': 'SHW',  'company_name': 'Sherwin-Williams Co.',       'gics_sector': 'Materials',              'gics_industry': 'Specialty Chemicals'},
    {'ticker': 'TRV',  'company_name': 'Travelers Companies Inc.',   'gics_sector': 'Financials',             'gics_industry': 'Property & Casualty Insurance'},
    {'ticker': 'UNH',  'company_name': 'UnitedHealth Group Inc.',    'gics_sector': 'Health Care',            'gics_industry': 'Managed Health Care'},
    {'ticker': 'V',    'company_name': 'Visa Inc.',                  'gics_sector': 'Financials',             'gics_industry': 'Transaction & Payment Processing Services'},
    {'ticker': 'VZ',   'company_name': 'Verizon Communications Inc.','gics_sector': 'Communication Services', 'gics_industry': 'Integrated Telecommunication Services'},
    {'ticker': 'WMT',  'company_name': 'Walmart Inc.',               'gics_sector': 'Consumer Staples',       'gics_industry': 'Consumer Staples Merchandise Retail'},
]

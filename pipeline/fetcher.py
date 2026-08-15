# pipeline/fetcher.py
# YH Finance (yahoo-finance15) + FMP HTTP clients, Wikipedia constituent scraper.
# IOZ PCF + ASX CSV for XAO constituents; SSGA SPSM XLSX for Russell 2000 proxy.

import calendar
import concurrent.futures
import re
import time
import urllib.parse
from datetime import date as _date
from typing import Optional

import requests

_RAPID_KEY  = '24b6bc2e2bmsh347c3730e17abfcp17ef9cjsnf420d70ec43d'
_RAPID_HOST = 'yahoo-finance15.p.rapidapi.com'
_RAPID_BASE = 'https://yahoo-finance15.p.rapidapi.com'
_RAPID_HEADERS = {
    'x-rapidapi-key':  _RAPID_KEY,
    'x-rapidapi-host': _RAPID_HOST,
    'Content-Type':    'application/json',
}

_TIMEOUT     = 30
_RETRY_DELAY = 2
_RETRY_DELAY_2 = 5  # second retry backoff

# ── Cheap pre-filter thresholds (screened from a single quote-endpoint call,
#    before the 7-module fetch) — loose defaults meant to exclude broken/junk
#    data, not to pick "good" stocks; final scoring still ranks survivors. ──
_PREFILTER_MIN_MARKET_CAP = 50_000_000    # $50M floor
_PREFILTER_MIN_AVG_VOLUME = 50_000        # shares/day floor
_PREFILTER_MAX_PE         = 150           # excludes distressed/bubble P/E

# cashflow-statement-v2 field name → the Yahoo field scorer.py reads.
# Module-level so the test suite asserts against the mapping the fetcher
# actually uses: D&A and buybacks were previously spelled
# 'cash_flow_statement_depreciation_and_amortization' and 'commonrepurchased',
# names the API never returns, so both silently mapped to nothing.
_CF_V2_FIELD_MAP = {
    'ncfo':              'totalCashFromOperatingActivities',
    'capex':             'capitalExpenditures',
    'totalDepAmorCF':    'depreciation',
    'commonRepurchased': 'repurchaseOfStock',
}


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
            if r.status_code == 429:
                print(f'  YHFinance rate limit [{ticker}/{module}] — waiting {_RETRY_DELAY_2}s')
                time.sleep(_RETRY_DELAY_2)
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

    def _fetch_quote(self, symbol: str) -> Optional[dict]:
        """Fetch the quote endpoint: price, marketCap, trailingPE, volume, dividend fields."""
        try:
            encoded = urllib.parse.quote(symbol)
            qr = self.session.get(
                f'{_RAPID_BASE}/api/yahoo/qu/quote/{encoded}',
                headers=_RAPID_HEADERS, timeout=_TIMEOUT,
            )
            if qr.status_code == 429:
                time.sleep(_RETRY_DELAY)
                qr = self.session.get(
                    f'{_RAPID_BASE}/api/yahoo/qu/quote/{encoded}',
                    headers=_RAPID_HEADERS, timeout=_TIMEOUT,
                )
            if qr.status_code == 429:
                time.sleep(_RETRY_DELAY_2)
                qr = self.session.get(
                    f'{_RAPID_BASE}/api/yahoo/qu/quote/{encoded}',
                    headers=_RAPID_HEADERS, timeout=_TIMEOUT,
                )
            if qr.status_code == 200:
                qbody = qr.json().get('body')
                if isinstance(qbody, list) and qbody:
                    return qbody[0]
                elif isinstance(qbody, dict):
                    return qbody
        except Exception as e:
            print(f'  YHFinance quote [{symbol}]: {e}')
        return None

    @staticmethod
    def _passes_prefilter(q: dict) -> bool:
        """
        Cheap screen on quote-endpoint fields, before the 7-module fetch.
        Loose defaults meant to exclude broken/junk data (unprofitable, illiquid,
        distressed P/E) — not a quality bar. Final scoring ranks survivors.
        """
        eps = q.get('epsTrailingTwelveMonths')
        if eps is None or eps <= 0:
            return False

        market_cap = q.get('marketCap')
        if market_cap is None or market_cap < _PREFILTER_MIN_MARKET_CAP:
            return False

        volume = q.get('averageDailyVolume3Month') or q.get('regularMarketVolume')
        if volume is None or volume < _PREFILTER_MIN_AVG_VOLUME:
            return False

        pe = q.get('trailingPE') or q.get('forwardPE')
        if pe is None:
            price = q.get('regularMarketPrice')
            if price and eps:
                pe = price / eps
        if pe is not None and (pe <= 0 or pe > _PREFILTER_MAX_PE):
            return False

        return True

    def quote_summary(self, symbol: str) -> Optional[dict]:
        """
        Fetch and adapt YH Finance data to Yahoo quoteSummary format for scorer.py.
        Cheap pre-filter first: a single quote-endpoint call screens out weak
        candidates (unprofitable, illiquid, distressed P/E, sub-$50M market cap)
        before spending the 7 module calls. Fetches standard + v2 modules
        concurrently for survivors; adapts v2 to Yahoo list format.
        Returns None if the pre-filter rejects, or if financial-data and
        default-key-statistics are both empty after the full fetch.
        """
        quote_data = self._fetch_quote(symbol)
        if not quote_data or not self._passes_prefilter(quote_data):
            return None

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
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
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
        cf_rows = self._v2_to_annual_list(cf_v2, _CF_V2_FIELD_MAP)

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

        # ── Fallback: standard income statement for ASX stocks (v2 returns empty) ──
        # Only income survives as a fallback. It is already in all_modules above,
        # so it costs no extra request, and it is genuinely populated (24 fields
        # for .AX and US alike).
        if not inc_rows:
            fallback = income_std.get('incomeStatementHistory')
            if isinstance(fallback, list):
                inc_rows = fallback
            elif isinstance(fallback, dict):
                inc_rows = fallback.get('incomeStatementHistory') or []

        # No balance-sheet fallback: the standard 'balance-sheet' module returns
        # rows of nothing but endDate/maxAge — not one financial field — for every
        # ticker checked (4 ASX and 2 US, AAPL and MSFT included). It is emptier
        # than the cash-flow module, which at least carries netIncome. Requesting
        # it spent a request per ASX stock and yielded 4 rows the scorer reads
        # nothing out of, so bal_list was non-empty but every _extract returned
        # None. Dropping it is behaviour-neutral: roe_3yr_avg and
        # book_value_growth already resolved to None down that path.
        #
        # ASX has no -v2 coverage, so bs_rows stays empty for ASX and scorer.py
        # covers the single-period figures from other modules it already fetches:
        # debt_equity from financialData.debtToEquity, current_ratio from
        # financialData.currentRatio, total_debt from financialData.totalDebt, and
        # equity from defaultKeyStatistics.bookValue x sharesOutstanding.
        # book_value_growth and net_net_ratio need multi-year equity and current
        # assets/total liabilities, which nothing here reports for ASX — they stay
        # None rather than being invented.

        # No cash-flow fallback: the standard module is a dead end. It was being
        # requested as 'cash-flow-statement', which is not a module name — the API
        # answers HTTP 200 with an HTML error page, so r.json() raised once per ASX
        # stock. The real name is 'cashflow-statement', but that module carries only
        # endDate/maxAge/netIncome for every ticker checked (8 ASX and 6 US,
        # AAPL included) — none of the _CF_V2_FIELD_MAP targets. Calling it would
        # spend a request per ASX stock to learn nothing.
        #
        # ASX has no -v2 coverage at all (income, balance-sheet and cashflow v2 all
        # return body: null), so cf_rows stays empty for ASX. scorer.py already
        # falls back to financialData.operatingCashflow and financialData.freeCashflow,
        # which are populated for ASX. capex, D&A and buybacks have no ASX source
        # here, so owner_earnings and the buyback half of shareholder_yield stay
        # None for ASX rather than being guessed at.

        # ── price / summaryDetail from quote (authoritative) + fin/stats fallback ──
        long_name = profile.get('longName') or ''
        price_dict = {
            'shortName':  long_name,
            'longName':   long_name,
            'symbol':     symbol,
            'marketCap':  quote_data.get('marketCap') or stats.get('marketCap'),
            'currency':   fin.get('financialCurrency') or '',
        }
        summary_detail = {
            'trailingPE':                  quote_data.get('trailingPE') or stats.get('trailingPE'),
            'dividendYield':               quote_data.get('trailingAnnualDividendYield'),
            'trailingAnnualDividendYield': quote_data.get('trailingAnnualDividendYield'),
            'dividendRate':                quote_data.get('dividendRate'),
            'priceToBook':                 quote_data.get('priceToBook') or stats.get('priceToBook'),
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



# ── Wikipedia constituent scraper ─────────────────────────────────────────────


def _enrich_with_yf_sectors(stocks: list, *, max_workers: int = 2) -> list:
    """
    Fetch GICS sector from YH Finance asset-profile for each stock.
    Falls back to _djia_sector_lookup for any ticker where Yahoo returns empty.
    """
    session = requests.Session()

    def _fetch_one(stock: dict) -> dict:
        ticker = stock['ticker']
        try:
            encoded = urllib.parse.quote(ticker)
            url = (f'{_RAPID_BASE}/api/v1/markets/stock/modules'
                   f'?ticker={encoded}&module=asset-profile')
            resp = session.get(url, headers=_RAPID_HEADERS, timeout=20)
            if resp.status_code == 429:
                time.sleep(_RETRY_DELAY)
                resp = session.get(url, headers=_RAPID_HEADERS, timeout=20)
            if resp.status_code == 429:
                time.sleep(_RETRY_DELAY_2)
                resp = session.get(url, headers=_RAPID_HEADERS, timeout=20)
            if resp.status_code == 200:
                body = resp.json().get('body') or {}
                yf_sector = body.get('sector', '')
                gics_sector = _YF_TO_GICS_SECTOR.get(yf_sector, '')
                if gics_sector:
                    return {**stock, 'gics_sector': gics_sector}
        except Exception as e:
            print(f'  DJIA sector [{ticker}]: {e}')
        return {**stock, 'gics_sector': _djia_sector_lookup(ticker)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_fetch_one, stocks))


# ── SPSM constituent fetcher (SSGA S&P 600 small-cap; Russell 2000 proxy) ────
# iShares IWM is reliably blocked by Akamai on GitHub Actions; SSGA SPSM XLSX
# is freely accessible and provides equivalent small-cap US sector coverage.

_SPSM_XLSX_URL = (
    'https://www.ssga.com/library-content/products/fund-data/etfs/us'
    '/holdings-daily-us-en-spsm.xlsx'
)
_SPSM_HEADERS = {
    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                   'AppleWebKit/537.36 (KHTML, like Gecko) '
                   'Chrome/125.0.0.0 Safari/537.36'),
    'Accept': ('application/vnd.openxmlformats-officedocument'
               '.spreadsheetml.sheet,*/*;q=0.8'),
}

# Yahoo Finance sector names → GICS sector names used by the pipeline
_YF_TO_GICS_SECTOR = {
    'Technology':             'Information Technology',
    'Healthcare':             'Health Care',
    'Financial Services':     'Financials',
    'Consumer Cyclical':      'Consumer Discretionary',
    'Consumer Defensive':     'Consumer Staples',
    'Industrials':            'Industrials',
    'Energy':                 'Energy',
    'Basic Materials':        'Materials',
    'Utilities':              'Utilities',
    'Real Estate':            'Real Estate',
    'Communication Services': 'Communication Services',
}


def _parse_spsm_xlsx(content: bytes) -> list:
    """
    Parse SSGA SPSM holdings XLSX using stdlib zipfile + xml.etree.
    Handles sparse cell references correctly (empty cells may be absent).
    No openpyxl dependency required.
    """
    import io
    import re
    import zipfile
    import xml.etree.ElementTree as ET

    ns = {'ns': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}

    def _col_idx(ref: str) -> int:
        letters = re.sub(r'\d', '', ref).upper()
        n = 0
        for ch in letters:
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        shared: list = []
        if 'xl/sharedStrings.xml' in zf.namelist():
            with zf.open('xl/sharedStrings.xml') as f:
                ss = ET.parse(f)
            for si in ss.getroot().findall('.//ns:si', ns):
                shared.append(''.join(t.text or '' for t in si.findall('.//ns:t', ns)))

        with zf.open('xl/worksheets/sheet1.xml') as f:
            ws = ET.parse(f)

        rows: list = []
        for row_el in ws.getroot().findall('.//ns:row', ns):
            cells: dict = {}
            for c in row_el.findall('ns:c', ns):
                ci    = _col_idx(c.get('r', ''))
                ctype = c.get('t', '')
                v     = c.find('ns:v', ns)
                if v is None or v.text is None:
                    val: object = None
                elif ctype == 's':
                    idx = int(v.text)
                    val = shared[idx] if idx < len(shared) else ''
                else:
                    try:
                        val = float(v.text)
                    except (ValueError, TypeError):
                        val = v.text
                cells[ci] = val
            if cells:
                mx = max(cells.keys())
                rows.append([cells.get(i) for i in range(mx + 1)])

    return rows


def fetch_iwm_constituents(top_per_sector: int = 10) -> list:
    """
    Small-cap US constituents using SSGA SPSM (S&P 600) as a Russell 2000 proxy.
    Downloads the SSGA holdings XLSX (no bot-detection), then enriches each
    holding with Yahoo Finance sector data. Returns the top top_per_sector
    stocks per GICS sector sorted by ETF weight.
    """
    from collections import defaultdict

    # ── Step 1: Download and parse SSGA SPSM holdings XLSX ───────────────────
    print('  Fetching SSGA SPSM holdings XLSX (S&P 600 small-cap universe)...')
    r = requests.get(_SPSM_XLSX_URL, headers=_SPSM_HEADERS, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f'SPSM XLSX: HTTP {r.status_code}')

    rows = _parse_spsm_xlsx(r.content)

    # Columns: Name=0, Ticker=1, Identifier=2, SEDOL=3, Weight=4
    hdr_idx = next(
        (i for i, row in enumerate(rows) if row and row[0] == 'Name'),
        None,
    )
    if hdr_idx is None:
        raise ValueError('SPSM XLSX: cannot find header row')

    candidates = []
    for row in rows[hdr_idx + 1:]:
        if not row or len(row) < 5:
            continue
        ticker = row[1]
        weight = row[4]
        if not ticker or ticker == '-' or not isinstance(weight, float):
            continue
        candidates.append({
            'ticker':       str(ticker).strip(),
            'company_name': str(row[0] or '').strip(),
            'weight':       weight,
        })

    candidates.sort(key=lambda x: x['weight'], reverse=True)
    universe = candidates[:150]  # top 150 by weight covers all 11 GICS sectors
    print(f'  SPSM: {len(candidates)} holdings; enriching top {len(universe)} with Yahoo Finance sector data')

    # ── Step 2: Fetch Yahoo Finance sector for each stock (parallelised) ──────
    session = requests.Session()

    def _enrich(stock: dict) -> dict:
        ticker = stock['ticker']
        try:
            encoded = urllib.parse.quote(ticker)
            url = (f'{_RAPID_BASE}/api/v1/markets/stock/modules'
                   f'?ticker={encoded}&module=asset-profile')
            resp = session.get(url, headers=_RAPID_HEADERS, timeout=20)
            if resp.status_code == 429:
                time.sleep(_RETRY_DELAY)
                resp = session.get(url, headers=_RAPID_HEADERS, timeout=20)
            if resp.status_code == 429:
                time.sleep(_RETRY_DELAY_2)
                resp = session.get(url, headers=_RAPID_HEADERS, timeout=20)
            if resp.status_code == 200:
                body = resp.json().get('body') or {}
                yf_sector   = body.get('sector', '')
                gics_sector = _YF_TO_GICS_SECTOR.get(yf_sector, '')
                return {
                    **stock,
                    'gics_sector':   gics_sector,
                    'gics_industry': body.get('industry', ''),
                }
        except Exception as e:
            print(f'  SPSM sector [{ticker}]: {e}')
        return {**stock, 'gics_sector': '', 'gics_industry': ''}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        enriched = list(ex.map(_enrich, universe))

    # ── Step 3: Group by GICS sector; top N per sector by weight ─────────────
    by_sector: dict = defaultdict(list)
    for stock in enriched:
        sec = stock['gics_sector']
        if sec:
            by_sector[sec].append(stock)

    result = []
    for sec, stocks in by_sector.items():
        stocks.sort(key=lambda x: x['weight'], reverse=True)
        result.extend(stocks[:top_per_sector])

    print(f'  SPSM: {len(result)} stocks selected (top {top_per_sector} per sector '
          f'across {len(by_sector)} sectors)')
    return result


# ── IOZ constituent fetcher ───────────────────────────────────────────────────

_IOZ_PRODUCT_URL  = 'https://www.blackrock.com/au/individual/products/251852/ishares-core-s-and-p-asx-200-etf'
_IOZ_PCF_URL      = 'https://www.blackrock.com/au/literature/pcf/pcf-ioz-en_au.csv'
_IOZ_HEADERS = {
    'User-Agent':      ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                        'AppleWebKit/537.36 (KHTML, like Gecko) '
                        'Chrome/125.0.0.0 Safari/537.36'),
    'Accept':          ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                        'image/avif,image/webp,*/*;q=0.8'),
    'Accept-Language': 'en-AU,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Referer':         'https://www.blackrock.com/au/individual/products/251852/ishares-core-s-and-p-asx-200-etf',
    'DNT':             '1',
    'Connection':      'keep-alive',
}

# Holdings enriched with Yahoo sector data, by market value. Mirrors the 150
# used for SPSM — enough to populate all 11 GICS sectors without over-spending
# asset-profile calls on the long tail of the index.
_IOZ_UNIVERSE_SIZE = 150


def _isin_to_asx_ticker(isin: str) -> Optional[str]:
    """
    Extract ASX ticker from a 12-char AU ISIN (e.g. AU000000BHP1 → BHP).
    Returns None for numeric NSINs, which carry no ticker to extract.
    """
    if not isin or not isin.startswith('AU') or len(isin) != 12:
        return None
    ticker = isin[2:11].lstrip('0')
    if not ticker or not ticker[0].isalpha() or not ticker.isalnum():
        return None
    return ticker


def fetch_ioz_constituents(top_per_sector: int = 10) -> list:
    """
    ASX constituents from the iShares IOZ (S&P/ASX 200) PCF CSV, enriched with
    Yahoo Finance sector data. Mirrors fetch_iwm_constituents: build a universe
    from the ETF holdings, resolve each holding's GICS sector from YH Finance
    asset-profile, then return the top top_per_sector stocks per sector.

    Sectors previously came from the ASX Listed Companies CSV, but that endpoint
    is now behind an Imperva WAF that answers HTTP 200 with a "Request Rejected"
    HTML body — which silently yielded an empty sector map and dropped every
    holding. Yahoo is the same source the US small-cap path already relies on.

    IOZ PCF columns: PCF Date, Fund Name, Sedol, Security Name, Number of Shares,
                     Security Price, ISIN  (all but first have a leading space).

    Returns list of {ticker, company_name, gics_sector, gics_industry, market_value}.
    Raises on any fetch or parse failure — no fallback.
    """
    import csv as _csv
    from collections import defaultdict

    # ── Step 1: Pre-fetch IOZ product page for session cookies ────────────────
    session = requests.Session()
    pre = session.get(_IOZ_PRODUCT_URL, headers=_IOZ_HEADERS, timeout=30)
    pre.raise_for_status()
    time.sleep(1.5)

    # ── Step 2: Download IOZ PCF CSV ──────────────────────────────────────────
    r = session.get(_IOZ_PCF_URL, headers=_IOZ_HEADERS, timeout=60)
    r.raise_for_status()

    # ── Step 3: Find header row — contains both "ISIN" and "Sedol" ────────────
    lines = r.text.splitlines()
    hdr = next(
        (i for i, line in enumerate(lines)
         if 'ISIN' in line and 'Sedol' in line),
        None,
    )
    if hdr is None:
        raise ValueError('IOZ PCF: could not locate ISIN/Sedol header row')

    # skipinitialspace strips the leading space from " Fund Name", " ISIN", etc.
    reader = _csv.DictReader(lines[hdr:], skipinitialspace=True)
    candidates: list = []
    skipped_no_ticker = 0

    for row in reader:
        isin     = (row.get('ISIN') or '').strip()
        pcf_name = (row.get('Security Name') or '').strip()
        try:
            shares = float((row.get('Number of Shares') or '0').replace(',', ''))
            price  = float((row.get('Security Price')   or '0').replace(',', ''))
        except (ValueError, AttributeError):
            continue
        market_value = shares * price
        if market_value <= 0:
            continue

        # Alpha ISIN → direct ticker. Numeric NSINs needed the ASX CSV name
        # index to resolve, so they are now unresolvable and get skipped.
        ticker = _isin_to_asx_ticker(isin)
        if not ticker:
            skipped_no_ticker += 1
            continue

        candidates.append({
            'ticker':       ticker,
            'company_name': pcf_name,
            'market_value': market_value,
        })

    if not candidates:
        raise ValueError('IOZ PCF: no resolvable tickers in holdings')

    candidates.sort(key=lambda x: x['market_value'], reverse=True)
    universe = candidates[:_IOZ_UNIVERSE_SIZE]
    print(f'  IOZ: {len(candidates)} holdings ({skipped_no_ticker} no-ticker); '
          f'enriching top {len(universe)} with Yahoo Finance sector data')

    # ── Step 4: Fetch Yahoo Finance sector for each stock (parallelised) ──────
    yf_session = requests.Session()   # separate from the BlackRock CDN session

    def _enrich(stock: dict) -> dict:
        # ASX tickers are quoted on Yahoo with a .AX suffix (e.g. BHP.AX).
        symbol = stock['ticker'] if '.' in stock['ticker'] else f'{stock["ticker"]}.AX'
        try:
            encoded = urllib.parse.quote(symbol)
            url = (f'{_RAPID_BASE}/api/v1/markets/stock/modules'
                   f'?ticker={encoded}&module=asset-profile')
            resp = yf_session.get(url, headers=_RAPID_HEADERS, timeout=20)
            if resp.status_code == 429:
                time.sleep(_RETRY_DELAY)
                resp = yf_session.get(url, headers=_RAPID_HEADERS, timeout=20)
            if resp.status_code == 429:
                time.sleep(_RETRY_DELAY_2)
                resp = yf_session.get(url, headers=_RAPID_HEADERS, timeout=20)
            if resp.status_code == 200:
                body = resp.json().get('body') or {}
                yf_sector   = body.get('sector', '')
                gics_sector = _YF_TO_GICS_SECTOR.get(yf_sector, '')
                return {
                    **stock,
                    'gics_sector':   gics_sector,
                    'gics_industry': body.get('industry', ''),
                }
        except Exception as e:
            print(f'  IOZ sector [{symbol}]: {e}')
        return {**stock, 'gics_sector': '', 'gics_industry': ''}

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        enriched = list(ex.map(_enrich, universe))

    # ── Step 5: Group by GICS sector; top N per sector by market value ────────
    by_sector: dict = defaultdict(list)
    for stock in enriched:
        if stock['gics_sector']:
            by_sector[stock['gics_sector']].append(stock)

    result = []
    for sec, stocks in by_sector.items():
        stocks.sort(key=lambda x: x['market_value'], reverse=True)
        result.extend(stocks[:top_per_sector])

    print(f'  IOZ: {len(result)} stocks selected (top {top_per_sector} per sector '
          f'across {len(by_sector)} sectors)')
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
                            'gics_sector':   '',
                            'gics_industry': industry,
                        })
                if results:
                    print(f'  Wikipedia DJIA: {len(results)} constituents — enriching sectors via Yahoo Finance...')
                    results = _enrich_with_yf_sectors(results)
                    print(f'  Wikipedia DJIA: {len(results)} constituents')
                    return results
        except Exception as e:
            print(f'  DJIA parse error: {e}')
        print('  DJIA Wikipedia parse failed — using static fallback')
        return _DJIA_FALLBACK

    def nasdaq100(self) -> list:
        """
        Returns NASDAQ-100 constituents.
        Wikipedia's Nasdaq-100 article no longer embeds a components table
        (it now only links out to nasdaq.com) — this uses Nasdaq's own
        public quote-list API instead, then enriches with GICS sector via
        Yahoo Finance (same pattern as djia()'s Yahoo sector enrichment).
        """
        try:
            # api.nasdaq.com's bot protection resets the connection for the
            # pipeline's self-identifying User-Agent (used everywhere else in
            # this class) — a browser UA is required here.
            r = self.session.get(
                'https://api.nasdaq.com/api/quote/list-type/nasdaq100',
                headers={
                    'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                                    'AppleWebKit/537.36 (KHTML, like Gecko) '
                                    'Chrome/125.0.0.0 Safari/537.36'),
                    'Accept': 'application/json, text/plain, */*',
                },
                timeout=30,
            )
            if r.status_code != 200:
                print(f'  NASDAQ-100 api.nasdaq.com fetch failed: HTTP {r.status_code}')
                return _NASDAQ100_FALLBACK
            rows = r.json()['data']['data']['rows']
            results = []
            for row in rows:
                ticker = str(row.get('symbol', '')).strip().replace('.', '-')
                name = re.sub(r'\s+Common Stock.*$', '', str(row.get('companyName', ticker))).strip()
                if ticker:
                    results.append({
                        'ticker':        ticker,
                        'company_name':  name or ticker,
                        'gics_sector':   '',
                        'gics_industry': '',
                    })
            if not results:
                print('  NASDAQ-100 api.nasdaq.com returned no constituents — using static fallback')
                return _NASDAQ100_FALLBACK
            print(f'  api.nasdaq.com NASDAQ-100: {len(results)} constituents — enriching sectors via Yahoo Finance...')
            results = _enrich_with_yf_sectors(results)
            print(f'  api.nasdaq.com NASDAQ-100: {len(results)} constituents')
            return results
        except Exception as e:
            print(f'  NASDAQ-100 parse error: {e} — using static fallback')
            return _NASDAQ100_FALLBACK


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

# Static fallback for nasdaq100() if api.nasdaq.com is ever unreachable
# (e.g. blocked from GitHub Actions runner IPs). Snapshot taken 2026-07-13 —
# will drift from actual NASDAQ-100 membership over time; refresh periodically.
_NASDAQ100_FALLBACK = [
    {'ticker': 'AAPL', 'company_name': 'Apple Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'ABNB', 'company_name': 'Airbnb, Inc.', 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'ADBE', 'company_name': 'Adobe Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'ADI', 'company_name': 'Analog Devices, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'ADP', 'company_name': 'Automatic Data Processing, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'ADSK', 'company_name': 'Autodesk, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'AEP', 'company_name': 'American Electric Power Company, Inc.', 'gics_sector': 'Utilities', 'gics_industry': ''},
    {'ticker': 'ALAB', 'company_name': 'Astera Labs, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'ALNY', 'company_name': 'Alnylam Pharmaceuticals, Inc.', 'gics_sector': 'Health Care', 'gics_industry': ''},
    {'ticker': 'AMAT', 'company_name': 'Applied Materials, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'AMD', 'company_name': 'Advanced Micro Devices, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'AMGN', 'company_name': 'Amgen Inc.', 'gics_sector': 'Health Care', 'gics_industry': ''},
    {'ticker': 'AMZN', 'company_name': 'Amazon.com, Inc.', 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'APP', 'company_name': 'Applovin Corporation', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'ARM', 'company_name': 'Arm Holdings plc American Depositary Shares', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'ASML', 'company_name': 'ASML Holding N.V. New York Registry Shares', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'AVGO', 'company_name': 'Broadcom Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'AXON', 'company_name': 'Axon Enterprise, Inc.', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'BKNG', 'company_name': 'Booking Holdings Inc.', 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'BKR', 'company_name': 'Baker Hughes Company', 'gics_sector': 'Energy', 'gics_industry': ''},
    {'ticker': 'CCEP', 'company_name': 'Coca-Cola Europacific Partners plc', 'gics_sector': 'Consumer Staples', 'gics_industry': ''},
    {'ticker': 'CDNS', 'company_name': 'Cadence Design Systems, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'CEG', 'company_name': 'Constellation Energy Corporation', 'gics_sector': 'Utilities', 'gics_industry': ''},
    {'ticker': 'CMCSA', 'company_name': 'Comcast Corporation', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'COST', 'company_name': 'Costco Wholesale Corporation', 'gics_sector': 'Consumer Staples', 'gics_industry': ''},
    {'ticker': 'CPRT', 'company_name': 'Copart, Inc. (DE)', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'CRWD', 'company_name': 'CrowdStrike Holdings, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'CRWV', 'company_name': 'CoreWeave, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'CSCO', 'company_name': 'Cisco Systems, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'CSX', 'company_name': 'CSX Corporation', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'CTAS', 'company_name': 'Cintas Corporation', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'DASH', 'company_name': 'DoorDash, Inc.', 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'DDOG', 'company_name': 'Datadog, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'DXCM', 'company_name': 'DexCom, Inc.', 'gics_sector': 'Health Care', 'gics_industry': ''},
    {'ticker': 'EA', 'company_name': 'Electronic Arts Inc.', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'EXC', 'company_name': 'Exelon Corporation', 'gics_sector': 'Utilities', 'gics_industry': ''},
    {'ticker': 'FANG', 'company_name': 'Diamondback Energy, Inc.', 'gics_sector': 'Energy', 'gics_industry': ''},
    {'ticker': 'FAST', 'company_name': 'Fastenal Company', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'FER', 'company_name': 'Ferrovial N.V.', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'FTNT', 'company_name': 'Fortinet, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'GEHC', 'company_name': 'GE HealthCare Technologies Inc.', 'gics_sector': 'Health Care', 'gics_industry': ''},
    {'ticker': 'GILD', 'company_name': 'Gilead Sciences, Inc.', 'gics_sector': 'Health Care', 'gics_industry': ''},
    {'ticker': 'GOOG', 'company_name': 'Alphabet Inc.', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'GOOGL', 'company_name': 'Alphabet Inc.', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'HON', 'company_name': 'Honeywell International Inc.', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'HONA', 'company_name': 'Honeywell Aerospace Inc.', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'IDXX', 'company_name': 'IDEXX Laboratories, Inc.', 'gics_sector': 'Health Care', 'gics_industry': ''},
    {'ticker': 'INTC', 'company_name': 'Intel Corporation', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'INTU', 'company_name': 'Intuit Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'ISRG', 'company_name': 'Intuitive Surgical, Inc.', 'gics_sector': 'Health Care', 'gics_industry': ''},
    {'ticker': 'KDP', 'company_name': 'Keurig Dr Pepper Inc.', 'gics_sector': 'Consumer Staples', 'gics_industry': ''},
    {'ticker': 'KHC', 'company_name': 'The Kraft Heinz Company', 'gics_sector': 'Consumer Staples', 'gics_industry': ''},
    {'ticker': 'KLAC', 'company_name': 'KLA Corporation', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'LIN', 'company_name': 'Linde plc', 'gics_sector': 'Materials', 'gics_industry': ''},
    {'ticker': 'LITE', 'company_name': 'Lumentum Holdings Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'LRCX', 'company_name': 'Lam Research Corporation', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'MAR', 'company_name': 'Marriott International', 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'MCHP', 'company_name': 'Microchip Technology Incorporated', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'MDLZ', 'company_name': 'Mondelez International, Inc.', 'gics_sector': 'Consumer Staples', 'gics_industry': ''},
    {'ticker': 'MELI', 'company_name': 'MercadoLibre, Inc.', 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'META', 'company_name': 'Meta Platforms, Inc.', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'MNST', 'company_name': 'Monster Beverage Corporation', 'gics_sector': 'Consumer Staples', 'gics_industry': ''},
    {'ticker': 'MPWR', 'company_name': 'Monolithic Power Systems, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'MRVL', 'company_name': 'Marvell Technology, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'MSFT', 'company_name': 'Microsoft Corporation', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'MSTR', 'company_name': 'Strategy Inc', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'MU', 'company_name': 'Micron Technology, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'NBIS', 'company_name': 'Nebius Group N.V.', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'NFLX', 'company_name': 'Netflix, Inc.', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'NVDA', 'company_name': 'NVIDIA Corporation', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'NXPI', 'company_name': 'NXP Semiconductors N.V.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'ODFL', 'company_name': 'Old Dominion Freight Line, Inc.', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'ORLY', 'company_name': "O'Reilly Automotive, Inc.", 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'PANW', 'company_name': 'Palo Alto Networks, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'PAYX', 'company_name': 'Paychex, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'PCAR', 'company_name': 'PACCAR Inc.', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'PDD', 'company_name': 'PDD Holdings Inc. American Depositary Shares', 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'PEP', 'company_name': 'PepsiCo, Inc.', 'gics_sector': 'Consumer Staples', 'gics_industry': ''},
    {'ticker': 'PLTR', 'company_name': 'Palantir Technologies Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'PYPL', 'company_name': 'PayPal Holdings, Inc.', 'gics_sector': 'Financials', 'gics_industry': ''},
    {'ticker': 'QCOM', 'company_name': 'QUALCOMM Incorporated', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'REGN', 'company_name': 'Regeneron Pharmaceuticals, Inc.', 'gics_sector': 'Health Care', 'gics_industry': ''},
    {'ticker': 'RKLB', 'company_name': 'Rocket Lab Corporation', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'ROP', 'company_name': 'Roper Technologies, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'ROST', 'company_name': 'Ross Stores, Inc.', 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'SBUX', 'company_name': 'Starbucks Corporation', 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'SHOP', 'company_name': 'Shopify Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'SNDK', 'company_name': 'Sandisk Corporation', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'SNPS', 'company_name': 'Synopsys, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'SPCX', 'company_name': 'Space Exploration Technologies Corp.', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'STX', 'company_name': 'Seagate Technology Holdings PLC', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'TER', 'company_name': 'Teradyne, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'TMUS', 'company_name': 'T-Mobile US, Inc.', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'TRI', 'company_name': 'Thomson Reuters Corporation Common Shares', 'gics_sector': 'Industrials', 'gics_industry': ''},
    {'ticker': 'TSLA', 'company_name': 'Tesla, Inc.', 'gics_sector': 'Consumer Discretionary', 'gics_industry': ''},
    {'ticker': 'TTWO', 'company_name': 'Take-Two Interactive Software, Inc.', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'TXN', 'company_name': 'Texas Instruments Incorporated', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'VRTX', 'company_name': 'Vertex Pharmaceuticals Incorporated', 'gics_sector': 'Health Care', 'gics_industry': ''},
    {'ticker': 'WBD', 'company_name': 'Warner Bros. Discovery, Inc. Series A', 'gics_sector': 'Communication Services', 'gics_industry': ''},
    {'ticker': 'WDAY', 'company_name': 'Workday, Inc.', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'WDC', 'company_name': 'Western Digital Corporation', 'gics_sector': 'Information Technology', 'gics_industry': ''},
    {'ticker': 'WMT', 'company_name': 'Walmart Inc.', 'gics_sector': 'Consumer Staples', 'gics_industry': ''},
    {'ticker': 'XEL', 'company_name': 'Xcel Energy Inc.', 'gics_sector': 'Utilities', 'gics_industry': ''},
]

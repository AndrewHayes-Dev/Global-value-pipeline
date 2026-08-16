"""Pipeline test suite — scorer, uploader, spreadsheet formatter, bug checks."""
import json
import math
import sys

# Test labels contain arrows and box-drawing characters. On a cp1252 console
# printing a failure would itself raise UnicodeEncodeError, hiding the very
# result we need to read.
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, 'pipeline')

import scorer
from scorer import (
    buffett_score, quality_score, score_from_summary,
    enrich_from_edgar, enrich_from_fmp, _extract,
    _first_not_none, _is_bank_industry, _normalise_industry,
    _earnings_cagr,
)
from uploader import _sanitize
from generate_stock_spreadsheets import _fmt

PASS = 0
FAIL = 0


def check(label, got, expected, tol=None):
    global PASS, FAIL
    if tol is not None:
        ok = got is not None and abs(got - expected) <= tol
    else:
        ok = (got == expected)
    if ok:
        PASS += 1
    else:
        FAIL += 1
        print(f'  FAIL [{label}]: got {got!r}, expected {expected!r}')


# ────────────────────────────────────────────────────────────────────────────
# _extract
# ────────────────────────────────────────────────────────────────────────────
check('_extract raw dict',       _extract({'x': {'raw': 3.5}}, ['x']),       3.5)
check('_extract nested',         _extract({'a': {'b': 1.0}}, ['a', 'b']),    1.0)
check('_extract None value',     _extract({'x': None}, ['x']),               None)
check('_extract missing key',    _extract({}, ['x']),                        None)
check('_extract str int',        _extract({'x': '42'}, ['x']),               42.0)
check('_extract non-dict middle',_extract({'a': 'not-dict'}, ['a', 'b']),    None)
check('_extract int direct',     _extract({'x': 7}, ['x']),                  7.0)


# ────────────────────────────────────────────────────────────────────────────
# buffett_score — non-bank
# ────────────────────────────────────────────────────────────────────────────
perfect = buffett_score(
    pe=5, pb=0.5, margin_of_safety=50, fcf_yield=0.10, debt_equity=0.2,
    owner_earnings=1e9, roic=0.20, div_yield=0.05,
    roe=0.25, peg_ratio=0.5, interest_coverage=15,
    gross_margin=0.50, current_ratio=3.0, eps_growth=0.25,
    roe_3yr_avg=0.20, earnings_consistency=4,
    book_value_growth=0.15, net_net_ratio=2.0, revenue_growth=0.20,
    ps_ratio=0.5, industry='Semiconductors',
)
check('buffett perfect non-bank = 100', perfect, 100.0)

zero_score = buffett_score(
    pe=None, pb=None, margin_of_safety=None, fcf_yield=None,
    debt_equity=None, owner_earnings=None, roic=None, div_yield=None,
)
check('buffett all-None = 0', zero_score, 0.0)

# Verify max raw score denominators: non-bank=172, bank=142
# All-max non-bank already checked = 100.
# Bank: same inputs, industry='Diversified Banks' -> still 100 (numerator adjusts with denominator)
bank_perfect = buffett_score(
    pe=5, pb=0.5, margin_of_safety=50, fcf_yield=0.10, debt_equity=0.2,
    owner_earnings=1e9, roic=0.20, div_yield=0.05,
    roe=0.25, peg_ratio=0.5, interest_coverage=15,
    gross_margin=0.50, current_ratio=3.0, eps_growth=0.25,
    roe_3yr_avg=0.20, earnings_consistency=4,
    book_value_growth=0.15, net_net_ratio=2.0, revenue_growth=0.20,
    ps_ratio=0.5, industry='Diversified Banks',
)
check('buffett bank perfect = 100', bank_perfect, 100.0)

# Bank skips fcf_yield/debt_equity/gross_margin: same numerator but smaller denominator
# → higher score than non-bank when those fields are None
nonbank_missing = buffett_score(
    pe=5, pb=0.5, margin_of_safety=50, fcf_yield=None, debt_equity=None,
    owner_earnings=1e9, roic=0.20, div_yield=0.05, gross_margin=None,
    roe=0.25, peg_ratio=0.5, interest_coverage=15, current_ratio=3.0,
    eps_growth=0.25, roe_3yr_avg=0.20, earnings_consistency=4,
    book_value_growth=0.15, net_net_ratio=2.0, revenue_growth=0.20,
    ps_ratio=0.5, industry='Semiconductors',
)
bank_missing = buffett_score(
    pe=5, pb=0.5, margin_of_safety=50, fcf_yield=None, debt_equity=None,
    owner_earnings=1e9, roic=0.20, div_yield=0.05, gross_margin=None,
    roe=0.25, peg_ratio=0.5, interest_coverage=15, current_ratio=3.0,
    eps_growth=0.25, roe_3yr_avg=0.20, earnings_consistency=4,
    book_value_growth=0.15, net_net_ratio=2.0, revenue_growth=0.20,
    ps_ratio=0.5, industry='Diversified Banks',
)
if bank_missing > nonbank_missing:
    PASS += 1
else:
    FAIL += 1
    print(f'  FAIL [bank>nonbank when fcf/de/gm=None]: bank={bank_missing}, nonbank={nonbank_missing}')

# High D/E penalises non-bank but not bank
score_bank_highde = buffett_score(
    pe=15, pb=1.0, margin_of_safety=10, fcf_yield=0.05, debt_equity=2.0,
    owner_earnings=1e8, roic=0.10, div_yield=0.02, industry='Diversified Banks',
)
score_nonbank_highde = buffett_score(
    pe=15, pb=1.0, margin_of_safety=10, fcf_yield=0.05, debt_equity=2.0,
    owner_earnings=1e8, roic=0.10, div_yield=0.02, industry='Software',
)
if score_bank_highde > score_nonbank_highde:
    PASS += 1
else:
    FAIL += 1
    print(f'  FAIL [bank higher score with D/E=2.0]: bank={score_bank_highde}, nonbank={score_nonbank_highde}')

# score must be clamped to [0, 100]
edge_pe = buffett_score(
    pe=-5, pb=-1, margin_of_safety=-50, fcf_yield=-0.1, debt_equity=5.0,
    owner_earnings=None, roic=None, div_yield=None,
)
check('buffett negative inputs → 0', edge_pe, 0.0)


# ────────────────────────────────────────────────────────────────────────────
# quality_score
# ────────────────────────────────────────────────────────────────────────────
qmax = quality_score(
    fcf_margin=0.25, capex_intensity=0.01, gross_margin=0.55,
    shareholder_yield=0.10, roe_avg=0.30, ev_ebit=6,
    op_margin=0.30, gm_trend=0.10,
)
check('quality perfect = 100', qmax, 100.0)

qzero = quality_score(
    fcf_margin=None, capex_intensity=None, gross_margin=None,
    shareholder_yield=None, roe_avg=None, ev_ebit=None,
)
check('quality all-None = 0', qzero, 0.0)

# Negative capex_intensity should score 0 for that component
q_neg_capex = quality_score(
    fcf_margin=None, capex_intensity=-0.1, gross_margin=None,
    shareholder_yield=None, roe_avg=None, ev_ebit=None,
)
check('quality negative capex_intensity = 0', q_neg_capex, 0.0)


# ────────────────────────────────────────────────────────────────────────────
# score_from_summary
# ────────────────────────────────────────────────────────────────────────────
mock_neg_income = {
    'financialData': {'currentPrice': {'raw': 100.0}},
    'defaultKeyStatistics': {},
    'price': {},
    'summaryDetail': {},
    'summaryProfile': {},
    'incomeStatementHistory': {'incomeStatementHistory': [{'netIncome': {'raw': -1000}}]},
    'cashflowStatementHistory': {'cashflowStatements': []},
    'balanceSheetHistory': {'balanceSheetStatements': []},
}
# A loss-making year is now scored rather than erased: the company survives
# into the dataset, keeps its reported loss, and simply earns no points for the
# metrics that require positive earnings.
_neg = score_from_summary('T', mock_neg_income, 's', 'sp500')
check('negative net_income is scored, not dropped', _neg is not None, True)
check('negative net_income preserved',    _neg['net_income'],           -1000.0)
check('negative net_income scores 0',     _neg['score'],                 0.0)
check('negative net_income no owner earnings', _neg['owner_earnings'],   None)
check('negative net_income no intrinsic value', _neg['intrinsic_value'], None)
check('empty summary → None', score_from_summary('T', {}, 's', 'sp500'), None)
check('None summary → None', score_from_summary('T', None, 's', 'sp500'), None)

mock_good = {
    'financialData': {
        'currentPrice': {'raw': 50.0}, 'totalRevenue': {'raw': 1e9},
        'freeCashflow': {'raw': 1e8}, 'debtToEquity': {'raw': 30.0},
        'returnOnEquity': {'raw': 0.18}, 'operatingMargins': {'raw': 0.15},
        'currentRatio': {'raw': 2.0}, 'earningsGrowth': {'raw': 0.12},
        'totalCash': {'raw': 5e8},
    },
    'defaultKeyStatistics': {
        'trailingEps': {'raw': 3.0}, 'bookValue': {'raw': 20.0},
        'sharesOutstanding': {'raw': 1e7}, 'marketCap': {'raw': 5e8},
        'priceToBook': {'raw': 2.5}, 'trailingPE': {'raw': 16.7},
        'dividendYield': {'raw': 0.025},
    },
    'price': {'shortName': 'Test Corp', 'marketCap': {'raw': 5e8}},
    'summaryDetail': {'trailingPE': {'raw': 16.7}, 'dividendYield': {'raw': 0.025}},
    'summaryProfile': {'industry': 'Semiconductors'},
    'incomeStatementHistory': {'incomeStatementHistory': [
        {'netIncome': {'raw': 6e7}, 'totalRevenue': {'raw': 1e9},
         'grossProfit': {'raw': 4e8}, 'interestExpense': {'raw': -1e7}},
        {'netIncome': {'raw': 5e7}, 'totalRevenue': {'raw': 9e8},
         'grossProfit': {'raw': 3.5e8}},
    ]},
    'cashflowStatementHistory': {'cashflowStatements': [
        {'totalCashFromOperatingActivities': {'raw': 1.2e8},
         'capitalExpenditures': {'raw': -2e7}, 'depreciation': {'raw': 1e7},
         'repurchaseOfStock': {'raw': -5e6}},
    ]},
    'balanceSheetHistory': {'balanceSheetStatements': [
        {'totalStockholderEquity': {'raw': 2e8}, 'totalCurrentAssets': {'raw': 3e8},
         'totalCurrentLiabilities': {'raw': 1e8}, 'totalLiab': {'raw': 2.5e8},
         'longTermDebt': {'raw': 1.5e8}},
        {'totalStockholderEquity': {'raw': 1.8e8}},
    ]},
}
s = score_from_summary('TEST', mock_good, 'sp500_tech', 'sp500', 'Semiconductors')
assert s is not None
assert 0 <= s['score'] <= 100, f"score out of range: {s['score']}"
assert 0 <= s['blended_score'] <= 100
assert s['ticker'] == 'TEST'
assert s['company_name'] == 'Test Corp'
assert s['gross_margin'] is not None
assert s['book_value_growth'] is not None
assert s['revenue_growth'] is not None
assert s['interest_coverage'] is not None
PASS += 1
print(f'  score_from_summary smoke: score={s["score"]}, blended={s["blended_score"]}, mos={s["margin_of_safety"]:.1f}%')

# blended_score formula
expected_blended = round(0.6 * s['score'] + 0.4 * s['quality_score'], 1)
check('blended_score = 0.6*score + 0.4*q_score', s['blended_score'], expected_blended)

# intrinsic value: Graham's growth-adjusted formula EPS * (8.5 + 2g), g clamped to [0, 15]
expected_iv = 3.0 * (8.5 + 2 * 12.0)
check('intrinsic_value Graham growth formula', s['intrinsic_value'], expected_iv, tol=0.01)

# margin of safety: (IV - price) / IV * 100
expected_mos = (expected_iv - 50.0) / expected_iv * 100
check('margin_of_safety formula', s['margin_of_safety'], expected_mos, tol=0.01)

# interest_coverage: ebit / abs(interest_expense)
# ebit derived from op_margin * revenue = 0.15 * 1e9 = 1.5e8; interest = 1e7
check('interest_coverage > 0', s['interest_coverage'] is not None and s['interest_coverage'] > 0, True)

# dividend yield normalisation: 0.025 is < 1.0 so used as-is
check('div_yield not divided by 100', s['dividend_yield'], 0.025)

# book_value_growth: (2e8 - 1.8e8) / 1.8e8
expected_bvg = (2e8 - 1.8e8) / 1.8e8
check('book_value_growth', s['book_value_growth'], expected_bvg, tol=1e-9)

# revenue_growth: (1e9 - 9e8) / 9e8
expected_rg = (1e9 - 9e8) / 9e8
check('revenue_growth', s['revenue_growth'], expected_rg, tol=1e-9)

# debt_equity from debtToEquity=30.0 -> /100 = 0.30
check('debt_equity /100 normalisation', s['debt_equity'], 0.30, tol=1e-9)


# ────────────────────────────────────────────────────────────────────────────
# Bug: enrich_from_edgar / enrich_from_fmp missing industry= in buffett_score
# ────────────────────────────────────────────────────────────────────────────
# High D/E bank stock: D/E=2.0 penalises non-bank treatment (-0 for bank, -0 for nonbank at this level)
# Better: use D/E=0.4 which gives non-bank 3pts but bank 0pts
score_correct_bank = buffett_score(
    pe=12, pb=1.0, margin_of_safety=15, fcf_yield=0.04, debt_equity=0.4,
    owner_earnings=1e8, roic=0.10, div_yield=0.02, industry='Diversified Banks',
)
score_wrong_nonbank = buffett_score(
    pe=12, pb=1.0, margin_of_safety=15, fcf_yield=0.04, debt_equity=0.4,
    owner_earnings=1e8, roic=0.10, div_yield=0.02, industry='',
)
# Non-bank gets extra debt_equity + fcf_yield + gross_margin pts, but also larger denominator
# The scores may or may not differ at this exact input; what matters is the treatment IS different
# for high D/E cases
# Bank vs non-bank should score differently (industry matters)
if score_correct_bank != score_wrong_nonbank:
    PASS += 1
else:
    # Try more extreme D/E where the difference is guaranteed
    s_b2 = buffett_score(pe=12, pb=1.0, margin_of_safety=15, fcf_yield=0.04,
                         debt_equity=2.0, owner_earnings=1e8, roic=0.10,
                         div_yield=0.02, industry='Diversified Banks')
    s_nb2 = buffett_score(pe=12, pb=1.0, margin_of_safety=15, fcf_yield=0.04,
                          debt_equity=2.0, owner_earnings=1e8, roic=0.10,
                          div_yield=0.02, industry='')
    if s_b2 != s_nb2:
        PASS += 1
    else:
        FAIL += 1
        print(f'  FAIL [bank/nonbank scores should differ with different industry]')

# After fix: enrich_from_edgar with a bank stock should use bank scoring
_bank_for_industry_test = {
    'ticker': 'JPM', 'industry': 'Diversified Banks',
    'pe_ratio': 10.0, 'pb_ratio': 1.2, 'margin_of_safety': 20.0,
    'debt_equity': 1.5, 'free_cash_flow': 1e10, 'market_cap': 5e11,
    'owner_earnings': 1e10, 'roic': 0.12, 'dividend_yield': 0.03,
    'roe': 0.14, 'peg_ratio': 1.2, 'interest_coverage': None,
    'gross_margin': None, 'current_ratio': 1.2, 'eps_growth': 0.08,
    'roe_3yr_avg': None, 'earnings_consistency': None,
    'book_value_growth': None, 'net_net_ratio': None, 'revenue_growth': 0.05,
    'ps_ratio': None, 'score': 0.0, 'quality_score': 50.0, 'blended_score': 0.0,
}
_edgar_for_industry_test = {
    'interest_expense': [1e8], 'operating_income': [1.5e9], 'ebt': [1.4e9],
    'equity': [2e9, 1.8e9], 'current_assets': [5e9],
    'total_liabilities': [3e9], 'total_assets': [8e9],
}
post_edgar_bank = enrich_from_edgar(_bank_for_industry_test, _edgar_for_industry_test)
post_edgar_nonbank = enrich_from_edgar({**_bank_for_industry_test, 'industry': 'Software'}, _edgar_for_industry_test)
# Bank (high D/E=1.5) should score higher than non-bank after EDGAR enrichment
if post_edgar_bank['score'] >= post_edgar_nonbank['score']:
    PASS += 1
else:
    FAIL += 1
    print(f'  FAIL [bank should score >= nonbank with D/E=1.5 after edgar]: '
          f'bank={post_edgar_bank["score"]}, nonbank={post_edgar_nonbank["score"]}')

# Bug: blended_score stale after enrich_from_edgar
stock_pre = {
    'ticker': 'JPM', 'industry': 'Diversified Banks',
    'pe_ratio': 10.0, 'pb_ratio': 1.2, 'margin_of_safety': 20.0,
    'debt_equity': 1.5, 'free_cash_flow': 1e10, 'market_cap': 5e11,
    'owner_earnings': 1e10, 'roic': 0.12, 'dividend_yield': 0.03,
    'roe': 0.14, 'peg_ratio': 1.2, 'interest_coverage': None,
    'gross_margin': None, 'current_ratio': 1.2, 'eps_growth': 0.08,
    'roe_3yr_avg': None, 'earnings_consistency': None,
    'book_value_growth': None, 'net_net_ratio': None, 'revenue_growth': 0.05,
    'ps_ratio': None, 'score': 0.0, 'quality_score': 50.0, 'blended_score': 0.0,
}
edgar_data = {
    'interest_expense': [1e8],
    'operating_income': [1.5e9],
    'ebt': [1.4e9],
    'equity': [2e9, 1.8e9],
    'current_assets': [5e9],
    'total_liabilities': [3e9],
    'total_assets': [8e9],
}
post_edgar = enrich_from_edgar(stock_pre, edgar_data)
correct_blended = round(0.6 * post_edgar['score'] + 0.4 * post_edgar.get('quality_score', 0.0), 1)
check('blended_score updated after edgar enrich', post_edgar.get('blended_score'), correct_blended)

# Bug: same for enrich_from_fmp
fmp_ratios = [{
    'priceEarningsRatio': 12.0, 'priceToBookRatio': 1.5,
    'dividendYield': 2.5, 'debtToEquityRatio': 0.3,
    'returnOnCapitalEmployed': 0.15, 'interestCoverageRatio': 8.0,
    'bookValuePerShare': 22.0,
}]
fmp_ratios_2 = [fmp_ratios[0], {'bookValuePerShare': 18.0}]
fmp_km = [{'netCurrentAssetValue': 1e9, 'marketCap': 5e11}]
stock_pre2 = dict(stock_pre)
stock_pre2.update({'interest_coverage': None, 'book_value_growth': None, 'net_net_ratio': None})
post_fmp = enrich_from_fmp(stock_pre2, fmp_ratios_2, fmp_km)
correct_blended_fmp = round(0.6 * post_fmp['score'] + 0.4 * post_fmp.get('quality_score', 0.0), 1)
check('blended_score updated after fmp enrich', post_fmp.get('blended_score'), correct_blended_fmp)


# ────────────────────────────────────────────────────────────────────────────
# _sanitize
# ────────────────────────────────────────────────────────────────────────────
check('sanitize NaN→None',    _sanitize(float('nan')),    None)
check('sanitize inf→None',    _sanitize(float('inf')),    None)
check('sanitize -inf→None',   _sanitize(float('-inf')),   None)
check('sanitize 0 untouched', _sanitize(0.0),             0.0)
check('sanitize str',         _sanitize('hello'),         'hello')
check('sanitize nested',      _sanitize({'a': float('nan'), 'b': 1.0}), {'a': None, 'b': 1.0})
check('sanitize list',        _sanitize([float('nan'), 2.0, None]), [None, 2.0, None])
try:
    json.dumps(_sanitize({'x': float('nan'), 'y': [float('inf'), 1.0]}))
    PASS += 1
except Exception as e:
    FAIL += 1
    print(f'  FAIL [sanitize json serializable]: {e}')


# ────────────────────────────────────────────────────────────────────────────
# _fmt
# ────────────────────────────────────────────────────────────────────────────
check('fmt None→None',         _fmt(None, 'f2'),     None)
check('fmt int',               _fmt(3.7, 'int'),     4)
check('fmt int4',              _fmt(3.0, 'int4'),    '3/4')
check('fmt f1',                _fmt(3.456, 'f1'),    3.5)
check('fmt f2',                _fmt(3.456, 'f2'),    3.46)
check('fmt $',                 _fmt(12.345, '$'),    '$12.35')
check('fmt $bm trillion',      _fmt(1.5e12, '$bm'),  '$1.50T')
check('fmt $bm billion',       _fmt(2.3e9, '$bm'),   '$2.30B')
check('fmt $bm million',       _fmt(456e6, '$bm'),   '$456.00M')
check('fmt $bm negative',      _fmt(-2.5e9, '$bm'),  '-$2.50B')
check('fmt pct',               _fmt(0.1234, 'pct'),  '12.3%')
check('fmt pct_raw',           _fmt(25.5, 'pct_raw'),'25.5%')
check('fmt str',               _fmt(42, 'str'),      '42')
check('fmt nan→None',          _fmt(float('nan'), 'f2'), None)
check('fmt inf→None',          _fmt(float('inf'), 'f2'), None)


# ────────────────────────────────────────────────────────────────────────────
# Industry normalisation / bank detection
# Industry arrives as GICS sub-industry (S&P 500) or Yahoo label (all other
# indices). Both dialects must resolve to the same bank/non-bank answer.
# ────────────────────────────────────────────────────────────────────────────
check('normalise dash',        _normalise_industry('Banks - Regional'), 'banks regional')
check('normalise ampersand',   _normalise_industry('Thrifts & Mortgage Finance'),
                                                  'thrifts and mortgage finance')
check('normalise em-dash',     _normalise_industry('Banks—Diversified'), 'banks diversified')
check('normalise empty',       _normalise_industry(''), '')

check('bank GICS regional',    _is_bank_industry('Regional Banks'),        True)
check('bank GICS diversified', _is_bank_industry('Diversified Banks'),     True)
check('bank GICS thrifts',     _is_bank_industry('Thrifts & Mortgage Finance'), True)
check('bank GICS consumer fin',_is_bank_industry('Consumer Finance'),      True)
check('bank Yahoo regional',   _is_bank_industry('Banks - Regional'),      True)
check('bank Yahoo diversified',_is_bank_industry('Banks - Diversified'),   True)
check('bank GICS thrifts yahoo',_is_bank_industry('Mortgage Finance'),     True)
# Yahoo's 'Credit Services' conflates lenders with payment networks (V, PYPL),
# so it must NOT trigger bank treatment — see the note on _BANK_INDUSTRIES.
check('bank not credit services',_is_bank_industry('Credit Services'),     False)
check('bank not software',     _is_bank_industry('Software - Application'), False)
check('bank not insurance',    _is_bank_industry('Property & Casualty Insurance'), False)
check('bank not biotech',      _is_bank_industry('Biotechnology'),         False)
check('bank empty industry',   _is_bank_industry(''),                      False)

# The Yahoo dialect must now reach the reduced 142 denominator, exactly as the
# GICS dialect already did. Same inputs, two spellings, one score.
_bank_args = dict(
    pe=12, pb=1.2, margin_of_safety=30, fcf_yield=None, debt_equity=None,
    owner_earnings=1e9, roic=0.12, div_yield=0.03,
    roe=0.18, peg_ratio=1.2, interest_coverage=8,
    gross_margin=None, current_ratio=1.6, eps_growth=0.12,
    roe_3yr_avg=0.16, earnings_consistency=4,
    book_value_growth=0.08, net_net_ratio=0.5, revenue_growth=0.09,
    ps_ratio=2.5,
)
gics_bank  = buffett_score(**_bank_args, industry='Regional Banks')
yahoo_bank = buffett_score(**_bank_args, industry='Banks - Regional')
non_bank   = buffett_score(**_bank_args, industry='Software - Application')
check('bank dialects agree',   yahoo_bank, gics_bank)
check('bank beats non-bank on missing bank-irrelevant fields',
      yahoo_bank > non_bank, True)


# ────────────────────────────────────────────────────────────────────────────
# _first_not_none — 0.0 is a value, not a miss
# ────────────────────────────────────────────────────────────────────────────
check('first_not_none keeps 0',    _first_not_none(0.0, 5.0),    0.0)
check('first_not_none skips None', _first_not_none(None, 5.0),   5.0)
check('first_not_none all None',   _first_not_none(None, None),  None)
check('first_not_none keeps neg',  _first_not_none(-1.0, 5.0),  -1.0)


# ────────────────────────────────────────────────────────────────────────────
# Negative debt/equity must not score as a pristine balance sheet
# ────────────────────────────────────────────────────────────────────────────
_de_args = dict(
    pe=None, pb=None, margin_of_safety=None, fcf_yield=None,
    owner_earnings=None, roic=None, div_yield=None,
)
de_zero     = buffett_score(**_de_args, debt_equity=0.0)
de_negative = buffett_score(**_de_args, debt_equity=-2.0)
de_none     = buffett_score(**_de_args, debt_equity=None)
check('debt-free scores best',      de_zero > 0,          True)
check('negative equity scores 0',   de_negative,          de_none)
check('negative equity != debt-free', de_negative == de_zero, False)


# ────────────────────────────────────────────────────────────────────────────
# Share issuance must not be rewarded as if it were a buyback
# ────────────────────────────────────────────────────────────────────────────
def _summary_with_repurchase(repurchase):
    return {
        'price': {'marketCap': 1e10},
        'financialData': {'currentPrice': 100.0, 'totalRevenue': 1e9},
        'defaultKeyStatistics': {'trailingEps': 5.0, 'sharesOutstanding': 1e8},
        'summaryDetail': {'dividendYield': 0.02},
        'incomeStatementHistory': {'incomeStatementHistory': [
            {'totalRevenue': 1e9, 'netIncome': 2e8, 'grossProfit': 6e8},
        ]},
        'cashflowStatementHistory': {'cashflowStatements': [
            {'repurchaseOfStock': repurchase,
             'totalCashFromOperatingActivities': 3e8,
             'capitalExpenditures': -5e7},
        ]},
        'balanceSheetHistory': {'balanceSheetStatements': [
            {'totalStockholderEquity': 5e9, 'totalDebt': 1e9},
        ]},
    }

buyback  = score_from_summary('BUY', _summary_with_repurchase(-5e8), 's', 'i')
issuance = score_from_summary('ISS', _summary_with_repurchase(5e8),  's', 'i')
check('buyback yield positive',  buyback['shareholder_yield'] > 0.02,  True)
check('issuance yield negative', issuance['shareholder_yield'] < 0.02, True)
check('issuance quality <= buyback quality',
      issuance['quality_score'] <= buyback['quality_score'], True)


# ────────────────────────────────────────────────────────────────────────────
# Market cap must not be fabricated from price alone
# ────────────────────────────────────────────────────────────────────────────
_no_mc = {
    'financialData': {'currentPrice': 50.0, 'totalRevenue': 1e9,
                      'freeCashflow': 1e8},
    'defaultKeyStatistics': {'trailingEps': 4.0},
    'incomeStatementHistory': {'incomeStatementHistory': [
        {'totalRevenue': 1e9, 'netIncome': 1e8},
    ]},
}
no_mc = score_from_summary('NOMC', _no_mc, 's', 'i')
check('no market cap → no ps_ratio',   no_mc['ps_ratio'],          None)
check('no market cap → no net_net',    no_mc['net_net_ratio'],     None)
check('no market cap → no shareholder yield from buybacks',
      no_mc['shareholder_yield'],                                  None)

# Without a market cap there is no denominator, so free cash flow cannot yield
# a fcf_yield — the score must be identical to the same company with no FCF
# reported at all. Under the old `price * 1e9` fallback it was not.
_no_mc_no_fcf = json.loads(json.dumps(_no_mc))
del _no_mc_no_fcf['financialData']['freeCashflow']
no_mc_no_fcf = score_from_summary('NOMC2', _no_mc_no_fcf, 's', 'i')
check('no market cap → FCF cannot inflate score',
      no_mc['score'], no_mc_no_fcf['score'])


# ────────────────────────────────────────────────────────────────────────────
# Graham growth rate — multi-year CAGR, not a single year-over-year jump
# ────────────────────────────────────────────────────────────────────────────
def _income(*net_incomes):
    """Annual income statements, newest first."""
    return [{'netIncome': ni, 'totalRevenue': 1e9} for ni in net_incomes]

# 100 -> 133.1 over three years is exactly 10%/yr.
check('cagr 10pct',        _earnings_cagr(_income(133.1, 121.0, 110.0, 100.0)), 0.10, tol=1e-9)
check('cagr flat',         _earnings_cagr(_income(100, 100, 100)),              0.0,  tol=1e-9)
check('cagr declining',    _earnings_cagr(_income(81, 90, 100))  < 0,           True)
check('cagr too few years',_earnings_cagr(_income(120, 100)),                   None)
check('cagr empty',        _earnings_cagr([]),                                  None)
check('cagr negative start',_earnings_cagr(_income(100, 50, -10)),              None)
check('cagr negative end',  _earnings_cagr(_income(-10, 50, 100)),              None)
check('cagr missing field', _earnings_cagr([{'netIncome': 100}, {}, {'netIncome': 50}]), None)

# A single spike year must no longer drive the valuation. Same company, same
# latest earnings; one has a flat history, one a one-off jump.
def _summary_for_growth(incomes):
    return {
        'price': {'marketCap': 1e10},
        'financialData': {'currentPrice': 100.0, 'totalRevenue': 1e9},
        'defaultKeyStatistics': {'trailingEps': 5.0},
        'incomeStatementHistory': {'incomeStatementHistory': _income(*incomes)},
    }

steady = score_from_summary('STDY', _summary_for_growth([110, 105, 100, 95]), 's', 'i')
spike  = score_from_summary('SPIK', _summary_for_growth([110, 55, 52, 50]),   's', 'i')
check('steady grower has intrinsic value', steady['intrinsic_value'] is not None, True)
# The spike company's CAGR (~30%) clamps to 15; the steady one's is ~5%.
check('spike valued above steady',  spike['intrinsic_value'] > steady['intrinsic_value'], True)
# Clamp holds: 15% growth caps the multiplier at 8.5 + 30 = 38.5.
check('growth multiplier clamped',  spike['intrinsic_value'], 5.0 * 38.5, tol=1e-6)

# growth_basis records which rate was actually used, so the next pipeline run
# reveals how often multi-year history was available.
check('growth_basis cagr', steady['growth_basis'], 'cagr')
check('growth_basis yoy',
      score_from_summary('YOY', _summary_for_growth([120, 100]), 's', 'i')['growth_basis'],
      'yoy')


# ────────────────────────────────────────────────────────────────────────────
# Graham AAA bond-yield adjustment
# ────────────────────────────────────────────────────────────────────────────
check('default yield is a no-op', scorer.AAA_BOND_YIELD, 4.4)

_baseline_iv = steady['intrinsic_value']
_saved_yield = scorer.AAA_BOND_YIELD
try:
    scorer.AAA_BOND_YIELD = 8.8  # double the base yield
    halved = score_from_summary('STDY', _summary_for_growth([110, 105, 100, 95]), 's', 'i')
    check('doubling the AAA yield halves intrinsic value',
          halved['intrinsic_value'], _baseline_iv / 2, tol=1e-6)
    check('higher yield lowers margin of safety',
          halved['margin_of_safety'] < steady['margin_of_safety'], True)
finally:
    scorer.AAA_BOND_YIELD = _saved_yield


# ────────────────────────────────────────────────────────────────────────────
# Owner earnings must be measured, not assumed
# ────────────────────────────────────────────────────────────────────────────
_oe_base = {
    'price': {'marketCap': 1e10},
    'financialData': {'currentPrice': 100.0, 'totalRevenue': 1e9},
    'defaultKeyStatistics': {'trailingEps': 5.0},
    'incomeStatementHistory': {'incomeStatementHistory': _income(2e8, 1.9e8, 1.8e8)},
}
oe_missing = score_from_summary('OEM', _oe_base, 's', 'i')
check('owner earnings None when D&A and capex missing', oe_missing['owner_earnings'], None)

_oe_full = json.loads(json.dumps(_oe_base))
_oe_full['cashflowStatementHistory'] = {'cashflowStatements': [
    {'depreciation': 5e7, 'capitalExpenditures': -3e7,
     'totalCashFromOperatingActivities': 2.5e8},
]}
oe_present = score_from_summary('OEP', _oe_full, 's', 'i')
check('owner earnings computed when inputs present',
      oe_present['owner_earnings'], 2e8 + 5e7 - 3e7, tol=1.0)
check('measured owner earnings scores above unmeasured',
      oe_present['score'] > oe_missing['score'], True)


# ────────────────────────────────────────────────────────────────────────────
# Gross margin trend in percentage points
# ────────────────────────────────────────────────────────────────────────────
def _gm_summary(*pairs):
    """pairs: (gross_profit, revenue), newest first."""
    return {
        'price': {'marketCap': 1e10},
        'financialData': {'currentPrice': 100.0, 'totalRevenue': pairs[0][1]},
        'defaultKeyStatistics': {'trailingEps': 5.0},
        'incomeStatementHistory': {'incomeStatementHistory': [
            {'grossProfit': gp, 'totalRevenue': rv, 'netIncome': 1e8}
            for gp, rv in pairs
        ]},
    }

# 52.5% now vs 50.0% then = +2.5 percentage points (relative change was +5%).
expanding = score_from_summary('EXP', _gm_summary((525, 1000), (500, 1000)), 's', 'i')
check('gm trend is percentage points', expanding['gross_margin_trend'], 0.025, tol=1e-9)

# A low-margin business moving 4.0% -> 4.2% is +0.2pp, not the +5% the old
# relative measure reported — it must no longer earn the top band.
thin = score_from_summary('THIN', _gm_summary((42, 1000), (40, 1000)), 's', 'i')
check('thin margin move is small in points', thin['gross_margin_trend'], 0.002, tol=1e-9)
check('thin margin scores below real expansion',
      thin['quality_score'] < expanding['quality_score'], True)


# ────────────────────────────────────────────────────────────────────────────
# Gross margin comes from financialData, and a reported zero is not a value
#
# The income-statement module carries no gross profit; the adapter surfaced the
# absence as 0, so `0 / revenue` made gross_margin 0.0 for every stock ever
# published. That is not merely cosmetic — it zeroed 20 points of quality_score
# and 5 of buffett_score across the whole universe.
# ────────────────────────────────────────────────────────────────────────────
def _fin_gm_summary(fin_extra: dict, gross_profit=None):
    return {
        'price': {'marketCap': 1e10},
        'financialData': {'currentPrice': 100.0, 'totalRevenue': 1000.0, **fin_extra},
        'defaultKeyStatistics': {'trailingEps': 5.0},
        'incomeStatementHistory': {'incomeStatementHistory': [
            {'grossProfit': gross_profit, 'totalRevenue': 1000.0, 'netIncome': 1e8},
        ]},
    }

# financialData wins, and the raw-wrapped shape the provider actually sends is
# unwrapped rather than stringified.
_fd = score_from_summary('FD', _fin_gm_summary({'grossMargins': {'raw': 0.51614}}), 's', 'i')
check('gross margin reads financialData.grossMargins', _fd['gross_margin'], 0.51614, tol=1e-9)

# Second choice: grossProfits (plural) over revenue, still without the income statement.
_gp = score_from_summary('GP', _fin_gm_summary({'grossProfits': 400.0}), 's', 'i')
check('gross margin falls back to grossProfits/revenue', _gp['gross_margin'], 0.4, tol=1e-9)

# The live regression: nothing in financialData and a zero from the income
# statement must read as unknown, never as a real 0.0.
_zero = score_from_summary('ZERO', _fin_gm_summary({}, gross_profit=0), 's', 'i')
check('a reported zero gross profit is not a margin', _zero['gross_margin'], None)
check('and its trend is not a fabricated 0.0', _zero['gross_margin_trend'], None)

# The whole point: a real margin has to move the quality score off the floor
# the dead value pinned every stock to.
check('a real gross margin outscores the dead zero',
      _fd['quality_score'] > _zero['quality_score'], True)


# ────────────────────────────────────────────────────────────────────────────
# Earnings consistency scaled to available history
# ────────────────────────────────────────────────────────────────────────────
short_clean = score_from_summary('SHRT', _summary_for_growth([100, 95]), 's', 'i')
long_clean  = score_from_summary('LONG', _summary_for_growth([100, 95, 90, 85]), 's', 'i')
check('two clean years score as consistent', short_clean['earnings_consistency'], 4)
check('four clean years score as consistent', long_clean['earnings_consistency'], 4)

mixed = score_from_summary('MIXD', _summary_for_growth([100, -5, 90, 85]), 's', 'i')
check('one loss year reduces consistency', mixed['earnings_consistency'], 3)


# ────────────────────────────────────────────────────────────────────────────
# cashflow-statement-v2 field mapping
#
# The v2 keys for D&A and buybacks were spelled
# 'cash_flow_statement_depreciation_and_amortization' and 'commonrepurchased',
# neither of which the API returns. Both silently mapped to nothing, so every
# US stock lost `depreciation` and `repurchaseOfStock` — and with D&A missing,
# owner_earnings fell back to None for the entire universe.
# ────────────────────────────────────────────────────────────────────────────
from fetcher import YHFinanceFetcher, _CF_V2_FIELD_MAP   # noqa: E402

# Keys and values are real AAPL figures from the live cashflow-statement-v2 body.
_cf_v2 = {
    'ncfo':              {'TTM': 146724000000, '2025-09-27': 111482000000},
    'capex':             {'TTM': -10041000000, '2025-09-27': -12715000000},
    'totalDepAmorCF':    {'TTM': 13100000000,  '2025-09-27': 11698000000},
    'commonRepurchased': {'TTM': -88929000000, '2025-09-27': -96671000000},
}
# Deliberately the production map, so a renamed key fails here.
_cf_rows = YHFinanceFetcher()._v2_to_annual_list(_cf_v2, _CF_V2_FIELD_MAP)
check('v2 cashflow yields one annual row', len(_cf_rows), 1)
check('v2 maps operating cash flow',
      _cf_rows[0].get('totalCashFromOperatingActivities'), {'raw': 111482000000})
check('v2 maps capex',
      _cf_rows[0].get('capitalExpenditures'), {'raw': -12715000000})
check('v2 maps D&A (totalDepAmorCF)',
      _cf_rows[0].get('depreciation'), {'raw': 11698000000})
check('v2 maps buybacks (commonRepurchased)',
      _cf_rows[0].get('repurchaseOfStock'), {'raw': -96671000000})

# With D&A and capex now mapped, owner earnings is computable end to end.
_oe_v2 = json.loads(json.dumps(_oe_base))
_oe_v2['cashflowStatementHistory'] = {'cashflowStatements': _cf_rows}
_oe_from_v2 = score_from_summary('OEV2', _oe_v2, 's', 'i')
check('owner earnings computed from v2 cashflow map',
      _oe_from_v2['owner_earnings'], 2e8 + 11698000000 - 12715000000, tol=1.0)


# ────────────────────────────────────────────────────────────────────────────
# ASX shape: no balance-sheet or cash-flow rows at all
#
# This API has no -v2 coverage for .AX tickers, and the standard balance-sheet
# and cashflow modules carry no financial fields, so an ASX summary reaches the
# scorer with both statement lists empty. The single-period figures must still
# come through from financialData / defaultKeyStatistics, and the genuinely
# unavailable ones must stay None rather than be invented.
# ────────────────────────────────────────────────────────────────────────────
_asx_base = {
    'financialData': {
        'currentPrice': 40.0, 'totalDebt': 29195999232, 'currentRatio': 1.649,
        'debtToEquity': 52.639, 'returnOnEquity': 0.247,
        'operatingCashflow': 19747000320, 'freeCashflow': 8137124864,
        'totalRevenue': 53987999744,
    },
    'defaultKeyStatistics': {
        'bookValue': 14.19057, 'sharesOutstanding': 5080690184, 'trailingEps': 3.0,
    },
    'price': {'marketCap': 203227607360},
    'summaryDetail': {'trailingPE': 13.3},
    'incomeStatementHistory': {'incomeStatementHistory': [
        {'netIncome': 1.5e10, 'totalRevenue': 5.4e10, 'grossProfit': 2e10},
        {'netIncome': 1.4e10, 'totalRevenue': 5.2e10, 'grossProfit': 1.9e10},
    ]},
    'cashflowStatementHistory': {'cashflowStatements': []},
    'balanceSheetHistory': {'balanceSheetStatements': []},
}
_asx = score_from_summary('ASX', json.loads(json.dumps(_asx_base)), 's', 'i')
check('ASX debt_equity from financialData', _asx['debt_equity'], 0.52639, tol=1e-9)
check('ASX current_ratio from financialData', _asx['current_ratio'], 1.649, tol=1e-9)
check('ASX roe_3yr_avg falls back to roe', _asx['roe_3yr_avg'], 0.247, tol=1e-9)
check('ASX free_cash_flow from financialData', _asx['free_cash_flow'], 8137124864.0, tol=1.0)
check('ASX owner_earnings stays None', _asx['owner_earnings'], None)
check('ASX book_value_growth stays None', _asx['book_value_growth'], None)
check('ASX net_net_ratio stays None', _asx['net_net_ratio'], None)

# The dropped balance-sheet fallback returned rows carrying only endDate/maxAge.
# Removing the call must not change a single scored field.
_asx_stub_rows = json.loads(json.dumps(_asx_base))
_asx_stub_rows['balanceSheetHistory'] = {'balanceSheetStatements': [
    {'endDate': {'raw': i}, 'maxAge': 1} for i in range(4)
]}
_asx_stub = score_from_summary('ASX', _asx_stub_rows, 's', 'i')
check('dropping the field-less balance rows changes nothing',
      _asx_stub, _asx)


# ────────────────────────────────────────────────────────────────────────────
# Batched quotes and analyst extras
# ────────────────────────────────────────────────────────────────────────────
class _FakeResp:
    def __init__(self, rows): self._rows = rows; self.status_code = 200
    def json(self): return {'body': self._rows}


class _FakeSession:
    """Records each batched quote URL and echoes a row per requested ticker."""
    def __init__(self): self.urls = []
    def get(self, url, **kw):
        self.urls.append(url)
        tickers = url.rsplit('/', 1)[-1].split(',')
        return _FakeResp([{'symbol': t, 'marketCap': 1e9} for t in tickers])


_bf = YHFinanceFetcher()
_bf.session = _FakeSession()
_symbols = [f'T{i}' for i in range(120)]
_batched = _bf.quotes_batch(_symbols, chunk=50)
check('quotes_batch splits 120 symbols into 3 requests', len(_bf.session.urls), 3)
check('quotes_batch returns every symbol', len(_batched), 120)
check('quotes_batch keys by symbol', _batched['T7']['marketCap'], 1e9)

# A short list must still be one request, not one per symbol.
_bf2 = YHFinanceFetcher(); _bf2.session = _FakeSession()
_bf2.quotes_batch(['AAPL', 'MSFT', 'BHP.AX'], chunk=50)
check('quotes_batch sends one request for a short list', len(_bf2.session.urls), 1)

# quote_summary must reuse a supplied quote rather than re-fetching it.
_qs = YHFinanceFetcher()
_qs_calls = []
_qs._fetch_quote = lambda sym: _qs_calls.append(sym)  # noqa: E731
# marketCap below the floor, so the prefilter rejects and no modules are fetched.
check('quote_summary with supplied quote skips the quote request',
      _qs.quote_summary('AAPL', quote_data={'epsTrailingTwelveMonths': 1.0,
                                            'marketCap': 1.0}), None)
check('quote_summary did not call _fetch_quote', _qs_calls, [])

# analyst_extras parsing, using the shapes the API really returns. ASX leaves
# unavailable values as [] rather than null, and its quarterly earnings-trend
# periods are empty — '+1y' is the entry that carries data for both markets.
_extras_bodies = {
    'calendar-events': {
        'earnings': {'earningsDate': [{'raw': 1786947120, 'fmt': '2026-08-17'}]},
        'exDividendDate': {'raw': 1772668800, 'fmt': '2026-03-05'},
        'dividendDate': [],
    },
    'recommendation-trend': {
        'trend': [{'period': '0m', 'strongBuy': 2, 'buy': 2, 'hold': 12,
                   'sell': 0, 'strongSell': 1},
                  {'period': '-1m', 'strongBuy': 9, 'buy': 9, 'hold': 0,
                   'sell': 0, 'strongSell': 0}],
    },
    'earnings-trend': {
        'trend': [
            {'period': '0q', 'growth': [], 'earningsEstimate': {'avg': []},
             'revenueEstimate': {'avg': [], 'growth': []}},
            {'period': '+1y', 'earningsEstimate': {'avg': {'raw': 2.54526}},
             'revenueEstimate': {'avg': {'raw': 5.68e10}, 'growth': {'raw': 0.0095}}},
        ],
    },
}
_ae = YHFinanceFetcher()
_ae._fetch_module = lambda sym, mod: _extras_bodies.get(mod)  # noqa: E731
_ex = _ae.analyst_extras('BHP.AX')
check('analyst_extras next earnings date', _ex['next_earnings_date'], '2026-08-17')
check('analyst_extras ex-dividend date', _ex['ex_dividend_date'], '2026-03-05')
# (2*1 + 2*2 + 12*3 + 0*4 + 1*5) / 17 = 47/17 = 2.7647 -> 2.76
check('analyst_extras rating uses the latest period', _ex['analyst_rating'], 2.76)
check('analyst_extras counts the analysts', _ex['analyst_count'], 17)
check('analyst_extras reads +1y EPS, not the empty quarter',
      _ex['eps_estimate_next_year'], 2.54526)
check('analyst_extras reads +1y revenue growth',
      _ex['revenue_growth_estimate'], 0.0095)

# Empty modules must yield Nones, not raise.
_ae_empty = YHFinanceFetcher()
_ae_empty._fetch_module = lambda sym, mod: None  # noqa: E731
check('analyst_extras tolerates missing modules',
      set(_ae_empty.analyst_extras('X').values()), {None})


# ────────────────────────────────────────────────────────────────────────────
# Empty-index guard — a failed upstream fetch must not overwrite good R2 data
# ────────────────────────────────────────────────────────────────────────────
from main import index_stock_count, publish_order

# ── Publish order matches what the app tells users ──────────────────────────
# Real values from the 2026-08-16 run's S&P 500 Financials sector, where the
# bug was visible: AMP had the sector's best blended score and was published
# last, because ranking used 'score' while selection used 'blended_score'.
_fin_sector = [
    {'ticker': 'TROW', 'score': 67.4, 'quality_score': 66.2, 'blended_score': 66.9},
    {'ticker': 'RJF',  'score': 64.0, 'quality_score': 55.4, 'blended_score': 60.6},
    {'ticker': 'AMP',  'score': 62.2, 'quality_score': 75.4, 'blended_score': 67.5},
]
check('published order is by blended score, not value score',
      [s['ticker'] for s in publish_order(_fin_sector)], ['AMP', 'TROW', 'RJF'])

# A stock that never got a blended score still ranks rather than raising.
_mixed = [
    {'ticker': 'A', 'score': 90.0},
    {'ticker': 'B', 'score': 10.0, 'blended_score': 95.0},
]
check('falls back to score when blended is absent',
      [s['ticker'] for s in publish_order(_mixed)], ['B', 'A'])

# Ordering must not mutate the caller's list — the US path reassigns, the ASX
# path passes a list it still holds a reference to.
_orig = [{'ticker': 'X', 'score': 1.0, 'blended_score': 1.0},
         {'ticker': 'Y', 'score': 2.0, 'blended_score': 9.0}]
publish_order(_orig)
check('publish_order does not mutate its input',
      [s['ticker'] for s in _orig], ['X', 'Y'])

# djia / nasdaq / russell2000 shape: a sector that yields nothing is skipped
# entirely, so a total failure leaves no sector entries at all.
check('index_stock_count: no sectors at all is zero', index_stock_count([]), 0)

# sp500 / xao shape: a sector that yields nothing is still appended, with an
# empty stocks list. This is the shape that made the bug invisible — the payload
# looks structurally complete and only the stock lists are empty.
_all_empty = [
    {'sector_id': 'sp500_tech', 'name': 'Information Technology', 'stocks': []},
    {'sector_id': 'sp500_health', 'name': 'Health Care', 'stocks': []},
    {'sector_id': 'sp500_finance', 'name': 'Financials', 'stocks': []},
]
check('index_stock_count: sectors present but all empty is zero',
      index_stock_count(_all_empty), 0)

# A single surviving stock is enough to publish — partial data beats none.
_one = [
    {'sector_id': 'xao_tech', 'name': 'Information Technology', 'stocks': []},
    {'sector_id': 'xao_finance', 'name': 'Financials', 'stocks': [{'ticker': 'CBA'}]},
]
check('index_stock_count: one stock across empty sectors publishes',
      index_stock_count(_one), 1)

_full = [
    {'sector_id': 'xao_tech', 'stocks': [{'ticker': 'XRO'}, {'ticker': 'WTC'}]},
    {'sector_id': 'xao_finance', 'stocks': [{'ticker': 'CBA'}, {'ticker': 'NAB'},
                                            {'ticker': 'WBC'}]},
]
check('index_stock_count: counts across sectors', index_stock_count(_full), 5)


# ────────────────────────────────────────────────────────────────────────────
# Results
# ────────────────────────────────────────────────────────────────────────────
print()
print(f'Results: {PASS} passed, {FAIL} failed')
sys.exit(1 if FAIL else 0)

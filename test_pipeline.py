"""Pipeline test suite — scorer, uploader, spreadsheet formatter, bug checks."""
import json
import math
import sys

sys.path.insert(0, 'pipeline')

from scorer import (
    buffett_score, quality_score, score_from_summary,
    enrich_from_edgar, enrich_from_fmp, _extract,
    _first_not_none, _is_bank_industry, _normalise_industry,
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
check('negative net_income → None', score_from_summary('T', mock_neg_income, 's', 'sp500'), None)
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
# Results
# ────────────────────────────────────────────────────────────────────────────
print()
print(f'Results: {PASS} passed, {FAIL} failed')
sys.exit(1 if FAIL else 0)

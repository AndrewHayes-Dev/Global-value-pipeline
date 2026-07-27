# pipeline/scorer.py
# Buffett scoring model — identical logic to the original value-investor-pipeline.

import math
from typing import Optional


def _extract(data, keys: list) -> Optional[float]:
    cur = data
    for key in keys:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    if cur is None:
        return None
    if isinstance(cur, (int, float)):
        return float(cur)
    if isinstance(cur, dict) and cur.get('raw') is not None:
        try:
            return float(cur['raw'])
        except (TypeError, ValueError):
            return None
    try:
        return float(str(cur))
    except (TypeError, ValueError):
        return None


def _first_not_none(*values) -> Optional[float]:
    """
    Like `a or b`, but 0.0 is a real value rather than a miss. A debt-free
    company reports totalDebt of 0, which `or` silently discarded.
    """
    for v in values:
        if v is not None:
            return v
    return None


def _extract_string(summary: dict, key: str) -> Optional[str]:
    for module in ('quoteType', 'summaryProfile', 'price', 'assetProfile'):
        mod = summary.get(module)
        if isinstance(mod, dict):
            val = mod.get(key)
            if isinstance(val, str) and val:
                return val
    val = summary.get(key)
    if isinstance(val, str) and val:
        return val
    return None


# Industry names arrive in two dialects: GICS sub-industries (S&P 500, scraped
# from Wikipedia) and Yahoo's own industry labels (every other index, via
# summaryProfile). Matching a fixed set of GICS strings therefore missed every
# bank outside the S&P 500 — Yahoo calls them 'Banks - Regional' and
# 'Credit Services', not 'Regional Banks' and 'Consumer Finance'. Normalise
# before comparing so both dialects resolve to the same answer.
#
# Deliberately NOT included: Yahoo's 'Credit Services'. GICS separates lenders
# ('Consumer Finance', e.g. AXP) from payment networks ('Transaction & Payment
# Processing Services', e.g. V, MA, PYPL), but Yahoo files both under 'Credit
# Services'. Payment networks have genuine gross margins and free cash flow, so
# suppressing those metrics for them would be a worse error than the one being
# fixed. Consequence: a lender reaching us via the Yahoo dialect is scored as a
# non-bank. That is a limitation of Yahoo's taxonomy, not something a string
# match can resolve.
_BANK_INDUSTRIES = {
    # GICS dialect
    'diversified banks', 'regional banks', 'commercial banks',
    'thrifts and mortgage finance', 'consumer finance',
    # Yahoo dialect
    'banks diversified', 'banks regional', 'mortgage finance',
}


def _normalise_industry(industry: str) -> str:
    """Lowercase, unify dash/ampersand separators, collapse whitespace."""
    if not industry:
        return ''
    s = industry.lower()
    for ch in ('—', '–', '-', '/', ','):
        s = s.replace(ch, ' ')
    s = s.replace('&', ' and ')
    return ' '.join(s.split())


def _is_bank_industry(industry: str) -> bool:
    """
    True for deposit-taking and lending businesses, where free cash flow,
    debt/equity and gross margin are not meaningful measures.
    """
    norm = _normalise_industry(industry)
    if not norm:
        return False
    if norm in _BANK_INDUSTRIES:
        return True
    # Catches 'banks diversified', 'regional banks', 'commercial banking', etc.
    return 'bank' in norm.split() or norm.startswith('bank')


def quality_score(*, fcf_margin, capex_intensity, gross_margin,
                  shareholder_yield, roe_avg, ev_ebit,
                  op_margin=None, gm_trend=None) -> float:
    score = 0.0

    if fcf_margin is not None:
        if fcf_margin >= 0.20:   score += 20
        elif fcf_margin >= 0.15: score += 15
        elif fcf_margin >= 0.10: score += 10
        elif fcf_margin >= 0.05: score += 5

    if capex_intensity is not None and capex_intensity >= 0:
        if capex_intensity <= 0.02:   score += 20
        elif capex_intensity <= 0.05: score += 15
        elif capex_intensity <= 0.10: score += 8
        elif capex_intensity <= 0.20: score += 3

    if gross_margin is not None and gross_margin > 0:
        if gross_margin >= 0.50:   score += 20
        elif gross_margin >= 0.35: score += 15
        elif gross_margin >= 0.20: score += 8
        elif gross_margin >= 0.10: score += 3

    if shareholder_yield is not None and shareholder_yield > 0:
        if shareholder_yield >= 0.08:   score += 15
        elif shareholder_yield >= 0.05: score += 10
        elif shareholder_yield >= 0.03: score += 6
        elif shareholder_yield >= 0.01: score += 3

    if roe_avg is not None and roe_avg > 0:
        if roe_avg >= 0.25:   score += 15
        elif roe_avg >= 0.20: score += 12
        elif roe_avg >= 0.15: score += 8
        elif roe_avg >= 0.10: score += 4

    if ev_ebit is not None and ev_ebit > 0:
        if ev_ebit <= 8:    score += 15
        elif ev_ebit <= 12: score += 10
        elif ev_ebit <= 18: score += 6
        elif ev_ebit <= 25: score += 2

    if op_margin is not None and op_margin > 0:
        if op_margin >= 0.25:   score += 20
        elif op_margin >= 0.20: score += 15
        elif op_margin >= 0.15: score += 10
        elif op_margin >= 0.10: score += 5
        elif op_margin >= 0.05: score += 2

    if gm_trend is not None:
        if gm_trend >= 0.05:    score += 5
        elif gm_trend >= -0.02: score += 3
        elif gm_trend >= -0.10: score += 1

    _MAX_QUALITY = 130.0
    return round(min(100.0, max(0.0, score / _MAX_QUALITY * 100.0)), 1)

def buffett_score(*, pe, pb, margin_of_safety, fcf_yield, debt_equity,
                  owner_earnings, roic, div_yield,
                  roe=None, peg_ratio=None, interest_coverage=None,
                  gross_margin=None, current_ratio=None, eps_growth=None,
                  roe_3yr_avg=None, earnings_consistency=None,
                  book_value_growth=None, net_net_ratio=None,
                  revenue_growth=None, ps_ratio=None, industry: str = '') -> float:
    score = 0.0
    is_bank = _is_bank_industry(industry)

    if pe is not None and pe > 0:
        if pe <= 10:   score += 20
        elif pe <= 15: score += 15
        elif pe <= 20: score += 8
        elif pe <= 25: score += 3

    if pb is not None and pb > 0:
        if pb <= 1.0:   score += 15
        elif pb <= 1.5: score += 12
        elif pb <= 3.0: score += 6

    if margin_of_safety is not None:
        if margin_of_safety >= 40:   score += 20
        elif margin_of_safety >= 25: score += 15
        elif margin_of_safety >= 15: score += 8
        elif margin_of_safety >= 5:  score += 3

    if not is_bank:
        if fcf_yield is not None:
            if fcf_yield >= 0.08:   score += 15
            elif fcf_yield >= 0.05: score += 10
            elif fcf_yield >= 0.02: score += 5
            elif fcf_yield > 0:     score += 2

    if not is_bank:
        # Negative debt/equity means negative shareholder equity — a distressed
        # balance sheet, not a pristine one. Without the >= 0 guard it scored
        # the full 10 points for being "below" every threshold.
        if debt_equity is not None and debt_equity >= 0:
            if debt_equity <= 0.25:  score += 10
            elif debt_equity <= 0.5: score += 7
            elif debt_equity <= 1.0: score += 3

    if owner_earnings is not None and owner_earnings > 0:
        score += 10

    if roic is not None:
        if roic >= 0.15:   score += 5
        elif roic >= 0.10: score += 3
        elif roic >= 0.08: score += 1

    if div_yield is not None and div_yield > 0:
        if div_yield >= 0.04:   score += 5
        elif div_yield >= 0.02: score += 3
        elif div_yield >= 0.01: score += 1

    if roe is not None and roe > 0:
        if roe >= 0.20:   score += 6
        elif roe >= 0.15: score += 4
        elif roe >= 0.10: score += 2

    if peg_ratio is not None and peg_ratio > 0:
        if peg_ratio < 1.0:   score += 8
        elif peg_ratio < 1.5: score += 5
        elif peg_ratio < 2.0: score += 2

    if interest_coverage is not None and interest_coverage > 0:
        if interest_coverage >= 10: score += 5
        elif interest_coverage >= 5: score += 3
        elif interest_coverage >= 2: score += 1

    if not is_bank and gross_margin is not None:
        if gross_margin >= 0.40:   score += 5
        elif gross_margin >= 0.20: score += 3
        elif gross_margin >= 0.10: score += 1

    if current_ratio is not None:
        if current_ratio >= 2.0:   score += 5
        elif current_ratio >= 1.5: score += 3
        elif current_ratio >= 1.0: score += 1

    if eps_growth is not None:
        if eps_growth >= 0.20:   score += 6
        elif eps_growth >= 0.10: score += 4
        elif eps_growth >= 0.05: score += 2

    if roe_3yr_avg is not None and roe_3yr_avg > 0:
        if roe_3yr_avg >= 0.15: score += 5
        elif roe_3yr_avg >= 0.10: score += 3

    if earnings_consistency is not None:
        if earnings_consistency >= 4:   score += 6
        elif earnings_consistency >= 3: score += 4
        elif earnings_consistency >= 2: score += 2

    if book_value_growth is not None:
        if book_value_growth >= 0.10:   score += 5
        elif book_value_growth >= 0.05: score += 3
        elif book_value_growth > 0:     score += 1

    if net_net_ratio is not None:
        if net_net_ratio >= 1.5:  score += 8
        elif net_net_ratio >= 1.0: score += 6
        elif net_net_ratio >= 0.7: score += 3

    if revenue_growth is not None:
        if revenue_growth >= 0.15:   score += 5
        elif revenue_growth >= 0.07: score += 3
        elif revenue_growth > 0:     score += 1

    if ps_ratio is not None and ps_ratio > 0:
        if ps_ratio <= 1.0:   score += 8
        elif ps_ratio <= 2.0: score += 5
        elif ps_ratio <= 3.0: score += 2

    _MAX_RAW_SCORE = 142.0 if is_bank else 172.0
    return round(min(100.0, max(0.0, score / _MAX_RAW_SCORE * 100.0)), 1)


def score_from_summary(ticker: str, summary: dict, sector_id: str,
                       index_id: str, industry: str = '') -> Optional[dict]:
    if not summary:
        return None

    price_mod = summary.get('price') or {}
    fin       = summary.get('financialData') or {}
    key_stats = summary.get('defaultKeyStatistics') or {}
    detail    = summary.get('summaryDetail') or {}
    profile   = summary.get('summaryProfile') or {}

    income_list = (summary.get('incomeStatementHistory') or {}).get('incomeStatementHistory') or []
    cash_list   = (summary.get('cashflowStatementHistory') or {}).get('cashflowStatements') or []
    bal_list    = (summary.get('balanceSheetHistory') or {}).get('balanceSheetStatements') or []
    bal_list_q  = (summary.get('balanceSheetHistoryQuarterly') or {}).get('balanceSheetStatements') or []

    income  = income_list[0] if income_list else {}
    cash    = cash_list[0]   if cash_list   else {}
    # Use annual balance sheet for single-period calcs; fall back to most recent quarter
    balance = (bal_list[0] if bal_list else
               (bal_list_q[0] if bal_list_q else {}))

    price = _extract(fin, ['currentPrice'])

    shares_out  = _extract(key_stats, ['sharesOutstanding'])
    market_cap  = (_extract(price_mod, ['marketCap']) or
                   (shares_out * price if shares_out and price and price > 0 else None))
    # Previously fell back to `price * 1e9`, inventing a share count and with it
    # a market cap that bore no relation to the company. Every ratio built on it
    # (fcf_yield, buyback_yield) was fiction. Better to leave those None.
    mc_for_yield = market_cap

    pe = _extract(detail, ['trailingPE']) or _extract(key_stats, ['forwardPE'])
    if pe is None:
        p       = _extract(fin, ['currentPrice'])
        eps_tmp = _extract(key_stats, ['trailingEps'])
        if p and eps_tmp and eps_tmp > 0:
            pe = p / eps_tmp

    pb = _extract(key_stats, ['priceToBook']) or _extract(detail, ['priceToBook'])

    div_raw = (_extract(detail, ['dividendYield']) or
               _extract(detail, ['trailingAnnualDividendYield']))
    div_yield = None
    if div_raw is not None:
        div_yield = div_raw / 100.0 if div_raw > 1.0 else div_raw

    div_rate_raw = _extract(detail, ['dividendRate'])
    dividend_rate = div_rate_raw if div_rate_raw and div_rate_raw > 0 else None

    revenue    = _extract(income, ['totalRevenue'])    or _extract(fin, ['totalRevenue'])
    net_income = (_extract(income, ['netIncome']) or
                  _extract(income, ['netIncomeApplicableToCommonShares']))
    if net_income is not None and net_income < 0:
        return None

    total_debt = _first_not_none(_extract(balance, ['totalDebt']),
                                 _extract(balance, ['longTermDebt']),
                                 _extract(fin, ['totalDebt']))
    equity = (_extract(balance, ['totalStockholderEquity']) or
              _extract(balance, ['stockholdersEquity']))
    if equity is None:
        bv = _extract(key_stats, ['bookValue'])
        if bv is not None and shares_out is not None:
            equity = bv * shares_out

    operating_cf = (_extract(cash, ['totalCashFromOperatingActivities']) or
                    _extract(fin, ['operatingCashflow']))
    capex = _extract(cash, ['capitalExpenditures'])
    da    = (_extract(cash, ['depreciation']) or
             _extract(cash, ['depreciationAndAmortization']))

    free_cash_flow = (_extract(fin, ['freeCashflow']) or
                      (operating_cf + capex if operating_cf is not None and capex is not None
                       else operating_cf))

    owner_earnings = (net_income + da + capex
                      if net_income is not None and da is not None and capex is not None
                      else net_income)

    roic = _first_not_none(_extract(fin, ['returnOnCapital']),
                           _extract(fin, ['returnOnEquity']))
    if roic is None and net_income is not None and total_debt is not None and equity is not None:
        ic = total_debt + max(equity, 0)
        if ic > 0:
            roic = net_income / ic

    roe       = _extract(fin, ['returnOnEquity'])
    peg_ratio = _extract(key_stats, ['pegRatio'])
    op_margin = _extract(fin, ['operatingMargins'])

    ebit = (_extract(income, ['ebit']) or
            _extract(income, ['operatingIncome']))
    if ebit is None and op_margin is not None and revenue is not None and revenue > 0:
        ebit = op_margin * revenue
    elif ebit is not None and op_margin is None and revenue and revenue > 0:
        op_margin = ebit / revenue
    interest_expense  = _extract(income, ['interestExpense'])
    interest_coverage = None
    if ebit is not None and interest_expense is not None and interest_expense != 0:
        interest_coverage = ebit / abs(interest_expense)

    gross_profit = _extract(income, ['grossProfit'])
    gross_margin = (gross_profit / revenue
                    if gross_profit is not None and revenue and revenue > 0 else None)

    gm_trend = None
    if len(income_list) >= 2:
        gm_series = []
        for inc in income_list:
            gp = _extract(inc, ['grossProfit'])
            rv = _extract(inc, ['totalRevenue'])
            if gp is not None and rv and rv > 0:
                gm_series.append(gp / rv)
        if len(gm_series) >= 2 and gm_series[-1] != 0:
            gm_trend = (gm_series[0] - gm_series[-1]) / abs(gm_series[-1])

    current_assets      = _extract(balance, ['totalCurrentAssets'])
    current_liabilities = _extract(balance, ['totalCurrentLiabilities'])
    current_ratio = (current_assets / current_liabilities
                     if current_assets is not None and current_liabilities and current_liabilities > 0
                     else None)
    if current_ratio is None:
        current_ratio = _extract(fin, ['currentRatio'])

    eps_growth = _extract(fin, ['earningsGrowth'])
    if eps_growth is None and len(income_list) >= 2:
        ni0 = _extract(income_list[0], ['netIncome'])
        ni1 = _extract(income_list[1], ['netIncome'])
        if ni0 is not None and ni1 is not None and ni1 != 0:
            eps_growth = (ni0 - ni1) / abs(ni1)

    if peg_ratio is None and pe is not None and pe > 0 and eps_growth is not None and eps_growth > 0:
        peg_ratio = pe / (eps_growth * 100)

    roe_3yr_avg = None
    if income_list and bal_list:
        roes = []
        length = min(min(len(income_list), len(bal_list)), 4)
        for i in range(length):
            ni_i = _extract(income_list[i], ['netIncome'])
            eq_i = (_extract(bal_list[i], ['totalStockholderEquity']) or
                    _extract(bal_list[i], ['stockholdersEquity']))
            if ni_i is not None and eq_i is not None and eq_i > 0:
                roes.append(ni_i / eq_i)
        if roes:
            roe_3yr_avg = sum(roes) / len(roes)
    if roe_3yr_avg is None:
        roe_3yr_avg = roe

    earnings_consistency = None
    if income_list:
        length = min(len(income_list), 4)
        profitable = sum(1 for i in range(length)
                         if (_extract(income_list[i], ['netIncome']) or 0) > 0)
        earnings_consistency = profitable

    book_value_growth = None
    if len(bal_list) >= 2:
        eq0 = (_extract(bal_list[0], ['totalStockholderEquity']) or
               _extract(bal_list[0], ['stockholdersEquity']))
        eq1 = (_extract(bal_list[1], ['totalStockholderEquity']) or
               _extract(bal_list[1], ['stockholdersEquity']))
        if eq0 is not None and eq1 is not None and eq1 != 0:
            book_value_growth = (eq0 - eq1) / abs(eq1)

    total_liabilities = (_extract(balance, ['totalLiab']) or
                         _extract(balance, ['totalLiabilities']))
    net_net_ratio = None
    if (current_assets is not None and total_liabilities is not None
            and market_cap is not None and market_cap > 0):
        net_net_ratio = (current_assets - total_liabilities) / market_cap

    revenue_growth = None
    if len(income_list) >= 2:
        r0 = _extract(income_list[0], ['totalRevenue'])
        r1 = _extract(income_list[1], ['totalRevenue'])
        if r0 is not None and r1 is not None and r1 != 0:
            revenue_growth = (r0 - r1) / abs(r1)

    eps = _extract(key_stats, ['trailingEps'])

    de_raw = _extract(fin, ['debtToEquity'])
    debt_equity = None
    if de_raw is not None:
        debt_equity = de_raw / 100.0
    elif total_debt is not None and equity is not None and equity > 0:
        debt_equity = total_debt / equity

    # Graham's growth-adjusted intrinsic value: V = EPS x (8.5 + 2g), g in
    # percentage points, clamped to [0, 15] — Graham's own guidance was to
    # keep growth assumptions conservative. The unadjusted Graham Number
    # (sqrt(22.5 x EPS x BVPS)) has no growth term and is proportional to
    # book value per share, so it systematically undervalues asset-light,
    # high-growth companies regardless of earnings quality.
    intrinsic_value = None
    if eps and eps > 0:
        g_pct = (eps_growth * 100) if eps_growth is not None else 0.0
        g_pct = max(0.0, min(g_pct, 15.0))
        intrinsic_value = eps * (8.5 + 2 * g_pct)

    margin_of_safety = None
    if intrinsic_value is not None and intrinsic_value > 0 and price and price > 0:
        margin_of_safety = ((intrinsic_value - price) / intrinsic_value) * 100

    fcf_yield = None
    if free_cash_flow is not None and mc_for_yield and mc_for_yield > 0:
        fcf_yield = free_cash_flow / mc_for_yield

    ps_ratio = None
    if market_cap is not None and revenue and revenue > 0:
        ps_ratio = market_cap / revenue

    # ── Quality metrics ───────────────────────────────────────────────────────
    fcf_margin = None
    if free_cash_flow is not None and revenue and revenue > 0:
        fcf_margin = free_cash_flow / revenue

    capex_intensity = None
    if capex is not None and revenue and revenue > 0:
        capex_intensity = abs(capex) / revenue

    # repurchaseOfStock is a cash-flow line: negative when the company buys its
    # own shares back, positive when it issues new ones. abs() treated dilution
    # as if it were a buyback and paid quality points for it. Negate instead, so
    # issuance produces a negative yield and is correctly penalised.
    repurchase = _extract(cash, ['repurchaseOfStock'])
    buyback_yield = None
    if repurchase is not None and mc_for_yield and mc_for_yield > 0:
        buyback_yield = -repurchase / mc_for_yield
    shareholder_yield = None
    if div_yield is not None or buyback_yield is not None:
        shareholder_yield = (div_yield or 0.0) + (buyback_yield or 0.0)

    cash_and_equiv = _extract(fin, ['totalCash'])
    ev = None
    if market_cap is not None and total_debt is not None:
        ev = market_cap + total_debt - (cash_and_equiv or 0)
    ev_ebit = None
    if ev is not None and ebit is not None and ebit > 0 and ev > 0:
        ev_ebit = ev / ebit

    # Pull industry from Yahoo summaryProfile if not supplied by caller
    if not industry:
        industry = profile.get('industry') or ''

    company_name = (_extract_string(summary, 'shortName') or
                    _extract_string(summary, 'longName') or ticker)

    score = buffett_score(
        pe=pe, pb=pb, margin_of_safety=margin_of_safety,
        fcf_yield=fcf_yield, debt_equity=debt_equity,
        owner_earnings=owner_earnings, roic=roic, div_yield=div_yield,
        roe=roe, peg_ratio=peg_ratio, interest_coverage=interest_coverage,
        gross_margin=gross_margin, current_ratio=current_ratio,
        eps_growth=eps_growth, roe_3yr_avg=roe_3yr_avg,
        earnings_consistency=earnings_consistency,
        book_value_growth=book_value_growth, net_net_ratio=net_net_ratio,
        revenue_growth=revenue_growth, ps_ratio=ps_ratio, industry=industry,
    )

    q_score = quality_score(
        fcf_margin=fcf_margin, capex_intensity=capex_intensity,
        gross_margin=gross_margin, shareholder_yield=shareholder_yield,
        roe_avg=roe_3yr_avg, ev_ebit=ev_ebit,
        op_margin=op_margin, gm_trend=gm_trend,
    )

    blended_score = round(0.6 * score + 0.4 * q_score, 1)

    return {
        'ticker':                ticker,
        'company_name':          company_name,
        'sector_id':             sector_id,
        'index_id':              index_id,
        'industry':              industry,
        'score':                 score,
        'current_price':         price,
        'market_cap':            market_cap,
        'pe_ratio':              pe,
        'pb_ratio':              pb,
        'dividend_yield':        div_yield,
        'dividend_rate':         dividend_rate,
        'free_cash_flow':        free_cash_flow,
        'debt_equity':           debt_equity,
        'revenue':               revenue,
        'net_income':            net_income,
        'intrinsic_value':       intrinsic_value,
        'margin_of_safety':      margin_of_safety,
        'owner_earnings':        owner_earnings,
        'roic':                  roic,
        'roe':                   roe,
        'peg_ratio':             peg_ratio,
        'interest_coverage':     interest_coverage,
        'gross_margin':          gross_margin,
        'current_ratio':         current_ratio,
        'eps_growth':            eps_growth,
        'roe_3yr_avg':           roe_3yr_avg,
        'earnings_consistency':  earnings_consistency,
        'book_value_growth':     book_value_growth,
        'net_net_ratio':         net_net_ratio,
        'revenue_growth':        revenue_growth,
        'quality_score':         q_score,
        'blended_score':         blended_score,
        'fcf_margin':            fcf_margin,
        'capex_intensity':       capex_intensity,
        'shareholder_yield':     shareholder_yield,
        'ev_ebit':               ev_ebit,
        'ps_ratio':              ps_ratio,
        'operating_margin':      op_margin,
        'gross_margin_trend':    gm_trend,
    }


def enrich_from_edgar(stock: dict, edgar_data: Optional[dict]) -> dict:
    """
    Fill interest_coverage, book_value_growth, net_net_ratio from SEC EDGAR XBRL data.
    edgar_data comes from EdgarFetcher.get_stock_data().
    """
    if not edgar_data:
        return stock

    interest_coverage = stock.get('interest_coverage')
    book_value_growth = stock.get('book_value_growth')
    net_net_ratio     = stock.get('net_net_ratio')

    ie  = edgar_data.get('interest_expense', [])
    oi  = edgar_data.get('operating_income', [])
    ebt = edgar_data.get('ebt', [])
    eq  = edgar_data.get('equity', [])
    ca  = edgar_data.get('current_assets', [])
    tl  = edgar_data.get('total_liabilities', [])
    ta  = edgar_data.get('total_assets', [])

    # interest_coverage = EBIT / interest_expense
    # EBIT = OperatingIncomeLoss; fallback: EBT + InterestExpense (for companies
    # like IBM that don't file OperatingIncomeLoss as a standalone XBRL concept)
    if interest_coverage is None and ie:
        ebit = oi[0] if oi else ((ebt[0] + ie[0]) if ebt else None)
        if ebit is not None and ie[0] != 0:
            interest_coverage = ebit / abs(ie[0])

    # book_value_growth = year-over-year equity change
    if book_value_growth is None and len(eq) >= 2 and eq[1] != 0:
        book_value_growth = (eq[0] - eq[1]) / abs(eq[1])

    # net_net_ratio = (current_assets - total_liabilities) / market_cap
    # When Liabilities isn't filed directly, derive it as total_assets - equity
    if net_net_ratio is None and ca:
        mc = stock.get('market_cap')
        if mc and mc > 0:
            if tl:
                net_net_ratio = (ca[0] - tl[0]) / mc
            elif ta and eq:
                net_net_ratio = (ca[0] - (ta[0] - eq[0])) / mc

    if (interest_coverage == stock.get('interest_coverage') and
            book_value_growth == stock.get('book_value_growth') and
            net_net_ratio == stock.get('net_net_ratio')):
        return stock

    fcf = stock.get('free_cash_flow')
    mc  = stock.get('market_cap')
    fcf_yield = fcf / mc if fcf is not None and mc and mc > 0 else None

    new_score = buffett_score(
        pe=stock.get('pe_ratio'), pb=stock.get('pb_ratio'),
        margin_of_safety=stock.get('margin_of_safety'),
        fcf_yield=fcf_yield, debt_equity=stock.get('debt_equity'),
        owner_earnings=stock.get('owner_earnings'), roic=stock.get('roic'),
        div_yield=stock.get('dividend_yield'),
        roe=stock.get('roe'), peg_ratio=stock.get('peg_ratio'),
        interest_coverage=interest_coverage,
        gross_margin=stock.get('gross_margin'), current_ratio=stock.get('current_ratio'),
        eps_growth=stock.get('eps_growth'), roe_3yr_avg=stock.get('roe_3yr_avg'),
        earnings_consistency=stock.get('earnings_consistency'),
        book_value_growth=book_value_growth,
        net_net_ratio=net_net_ratio,
        revenue_growth=stock.get('revenue_growth'),
        ps_ratio=stock.get('ps_ratio'),
        industry=stock.get('industry', ''),
    )
    new_blended = round(0.6 * new_score + 0.4 * stock.get('quality_score', 0.0), 1)

    return {**stock, 'score': new_score, 'blended_score': new_blended,
            'interest_coverage': interest_coverage,
            'book_value_growth': book_value_growth,
            'net_net_ratio': net_net_ratio}


def enrich_from_fmp(stock: dict, ratios_list: list, key_metrics: list = None) -> dict:
    """
    ratios_list: list of FMP annual ratio dicts, newest first (from FmpFetcher.ratios())
    key_metrics: list of FMP annual key-metric dicts (from FmpFetcher.key_metrics())
    """
    if not ratios_list:
        return stock

    ratios = ratios_list[0]

    def fmp_double(key, src=None):
        v = (src or ratios).get(key)
        if v is None:
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    pe = stock['pe_ratio'] or fmp_double('priceEarningsRatio')
    pb = stock['pb_ratio'] or fmp_double('priceToBookRatio')

    div_yield = stock['dividend_yield']
    if div_yield is None:
        raw = fmp_double('dividendYield')
        if raw is not None:
            div_yield = raw / 100.0 if raw > 1.0 else raw

    # 0.0 is a meaningful value for both (debt-free, or no return on capital),
    # so `or` would wrongly discard it in favour of the FMP figure.
    de   = _first_not_none(stock['debt_equity'], fmp_double('debtToEquityRatio'))
    roic = _first_not_none(stock['roic'],
                           fmp_double('returnOnCapitalEmployed'),
                           fmp_double('returnOnEquity'))

    fcf = stock['free_cash_flow']
    if fcf is None and stock.get('market_cap') and stock.get('current_price') and stock['current_price'] > 0:
        fcf_ps = fmp_double('freeCashFlowPerShare')
        if fcf_ps is not None:
            shares = stock['market_cap'] / stock['current_price']
            fcf = fcf_ps * shares

    fcf_yield = None
    if fcf is not None and stock.get('market_cap') and stock['market_cap'] > 0:
        fcf_yield = fcf / stock['market_cap']

    # Fill ratios Yahoo Finance no longer provides via balance sheet data
    interest_coverage = stock.get('interest_coverage')
    book_value_growth = stock.get('book_value_growth')
    net_net_ratio     = stock.get('net_net_ratio')

    # interest_coverage: FMP interestCoverageRatio; 0 means no net interest expense (skip)
    if interest_coverage is None:
        ic_raw = fmp_double('interestCoverageRatio')
        if ic_raw is not None and ic_raw > 0:
            interest_coverage = ic_raw

    # book_value_growth: year-over-year change in bookValuePerShare (needs 2 periods)
    if book_value_growth is None and len(ratios_list) >= 2:
        bvps0 = fmp_double('bookValuePerShare', ratios_list[0])
        bvps1 = fmp_double('bookValuePerShare', ratios_list[1])
        if bvps0 is not None and bvps1 is not None and bvps1 != 0:
            book_value_growth = (bvps0 - bvps1) / abs(bvps1)

    # net_net_ratio: (current_assets - total_liabilities) / market_cap
    # FMP key-metrics provides netCurrentAssetValue (total $) and marketCap
    if net_net_ratio is None and key_metrics:
        km0 = key_metrics[0]
        ncav = fmp_double('netCurrentAssetValue', km0)
        mc   = fmp_double('marketCap', km0)
        if ncav is not None and mc and mc > 0:
            net_net_ratio = ncav / mc

    new_score = buffett_score(
        pe=pe, pb=pb, margin_of_safety=stock['margin_of_safety'],
        fcf_yield=fcf_yield, debt_equity=de,
        owner_earnings=stock['owner_earnings'], roic=roic, div_yield=div_yield,
        roe=stock.get('roe'), peg_ratio=stock.get('peg_ratio'),
        interest_coverage=interest_coverage,
        gross_margin=stock.get('gross_margin'), current_ratio=stock.get('current_ratio'),
        eps_growth=stock.get('eps_growth'), roe_3yr_avg=stock.get('roe_3yr_avg'),
        earnings_consistency=stock.get('earnings_consistency'),
        book_value_growth=book_value_growth,
        net_net_ratio=net_net_ratio,
        revenue_growth=stock.get('revenue_growth'),
        ps_ratio=stock.get('ps_ratio'),
        industry=stock.get('industry', ''),
    )
    new_blended = round(0.6 * new_score + 0.4 * stock.get('quality_score', 0.0), 1)

    return {**stock, 'pe_ratio': pe, 'pb_ratio': pb, 'dividend_yield': div_yield, 'dividend_rate': stock.get('dividend_rate'),
            'debt_equity': de, 'roic': roic, 'free_cash_flow': fcf, 'score': new_score,
            'blended_score': new_blended,
            'interest_coverage': interest_coverage,
            'book_value_growth': book_value_growth,
            'net_net_ratio': net_net_ratio}

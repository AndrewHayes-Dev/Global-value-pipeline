# pipeline/sector_stocks.py
# US sector definitions — S&P 500, DJIA, NASDAQ-100 stocks come from Wikipedia dynamically.
# Russell 2000 constituents come from iShares IWM CSV (top 150 by market value, monthly).
# XAO stocks come from AndrewHayes-Dev/XAO-data companion repo.

# ── Index definitions ─────────────────────────────────────────────────────────

INDEX_DEFINITIONS = [
    {'id': 'sp500',       'name': 'S&P 500',                     'yahoo': '^GSPC', 'fmp': 'GSPC', 'country': 'US'},
    {'id': 'djia',        'name': 'Dow Jones Industrial Average', 'yahoo': '^DJI',  'fmp': 'DJI',  'country': 'US'},
    {'id': 'nasdaq',      'name': 'NASDAQ-100',                   'yahoo': '^NDX',  'fmp': 'NDX',  'country': 'US'},
    {'id': 'russell2000', 'name': 'Russell 2000',                 'yahoo': '^RUT',  'fmp': 'RUT',  'country': 'US'},
    {'id': 'xao',         'name': 'ASX All Ordinaries',           'yahoo': '^AORD', 'fmp': '',     'country': 'AU'},
]

# ── GICS sector name → screener suffix ───────────────────────────────────────
# Used to build sector_id keys like "sp500_tech", "xao_finance", etc.

GICS_TO_SUFFIX = {
    'Financials':             'finance',
    'Materials':              'materials',
    'Health Care':            'health',
    'Real Estate':            'realestate',
    'Consumer Discretionary': 'consumer_disc',
    'Consumer Staples':       'consumer_staples',
    'Industrials':            'industrials',
    'Energy':                 'energy',
    'Information Technology': 'tech',
    'Technology':             'tech',   # Wikipedia NASDAQ-100 uses this alias
    'Utilities':              'utilities',
    'Communication Services': 'comms',
    'Communication':          'comms',  # alternate Wikipedia label
    'Telecommunications':     'comms',  # alternate Wikipedia label
}

# All 11 GICS sectors in display order
GICS_SECTORS_ORDERED = [
    ('Information Technology', 'tech'),
    ('Health Care',            'health'),
    ('Financials',             'finance'),
    ('Consumer Discretionary', 'consumer_disc'),
    ('Communication Services', 'comms'),
    ('Industrials',            'industrials'),
    ('Consumer Staples',       'consumer_staples'),
    ('Energy',                 'energy'),
    ('Utilities',              'utilities'),
    ('Real Estate',            'realestate'),
    ('Materials',              'materials'),
]

# ── FMP free plan symbols eligible for /stable/ratios ────────────────────────

FMP_RATIOS_ALLOWED = {
    'AAPL','TSLA','AMZN','MSFT','NVDA','GOOGL','META','NFLX','JPM','V',
    'BAC','PYPL','DIS','T','PFE','COST','INTC','KO','TGT','NKE',
    'SPY','BA','BABA','XOM','WMT','GE','CSCO','VZ','JNJ','CVX',
    'PLTR','SQ','SHOP','SBUX','SOFI','HOOD','RBLX','SNAP','AMD','UBER',
    'FDX','ABBV','ETSY','MRNA','LMT','GM','F','LCID','CCL','DAL',
    'UAL','AAL','TSM','SONY','ET','MRO','COIN','RIVN','RIOT',
    'VWO','SPYG','NOK','ROKU','ATVI','BIDU','DOCU','ZM','PINS',
    'TLRY','WBA','MGM','NIO','C','GS','WFC','ADBE','PEP','UNH',
    'CARR','HCA','BILI','SIRI',
}

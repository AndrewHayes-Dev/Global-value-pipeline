# pipeline/sector_stocks.py
# US sector definitions — S&P 500, DJIA, NASDAQ-100 stocks come from Wikipedia dynamically.
# Russell 2000 remains curated (no free dynamic source available).
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

# ── Russell 2000 curated lists (no reliable free dynamic source) ──────────────

RUSSELL2000_SECTOR_STOCKS = {
    'russell2000_finance': [
        'IBCP','CBTX','HTBI','STBA','FRST','CARE','PFIS','CIZN','ESSA','BSVN',
        'BANR','CVBF','FIBK','GBCI','HIFS','NBTB','OCFC','PFSI','RNST','TOWN',
        'AFCG','ACRE','AROW','ATAX','BFST','BHLB','BMRC','BRKL','BYFC','CADE',
    ],
    'russell2000_health': [
        'ACAD','ACLS','ACHC','ADUS','AFMD','AGEN','AHCO','NEOG','OMCL','SUPN',
        'HALO','INVA','LMNX','MDGL','PCRX','PRGO','SAGE','RCKT','ANIP','PAHC',
        'ABCL','ACET','ACST','ADMA','ADPT','ADRO','AGIO','AKBA','AKRO','ALLO',
    ],
    'russell2000_industrials': [
        'ABSI','ACHR','ACCO','ACMR','ACTG','HAYZ','HTLD','HURC','KFRC','MNTK',
        'AEIS','BWXT','DXPE','NVEE','PRIM','CECO','HLIO','USLM','GMS','ARCB',
        'AGYS','AHPI','AIRG','AIXI','ALGT','ALUS','AMRC','AMSF','AMTX','APOG',
    ],
    'russell2000_tech': [
        'EGHT','BAND','CLFD','CODA','COHU','CRDO','DGII','DIOD','EDAP','FORM',
        'HLIT','ICAD','MTSI','NABL','PDFS','POWI','RMBS','UCTT','VIAV','IIIV',
        'ACMR','AEHR','AGYS','AIOT','ALRM','ALTR','ALYA','AMID','AMKR','AMMO',
    ],
    'russell2000_consumer_disc': [
        'BOOT','BURL','BJ','CHUY','DINE','EAT','JACK','LZB','CENT','GIII',
        'JILL','LOVE','PLAY','PLCE','REYN','SCVL','TILE','VSCO','PRTS','LESL',
        'ACMR','AEIS','AGIO','AGYS','AHCO','AIXI','ALGT','ALUS','AMRC','AMTX',
    ],
    'russell2000_energy': [
        'AMPY','CIVI','FLNG','GPRE','HESM','MGY','MNRL','NOG','BATL','CRK',
        'EGY','KOS','PFIE','ROCC','SJT','VET','VTLE','SM','REX','TPVG',
    ],
    'russell2000_realestate': [
        'ALEX','BRT','CLPR','CTRE','CUBE','GMRE','IIPR','INDUS','IRT',
        'GOOD','NXRT','NTST','PLYM','SAFE','LAND','PINE','NLCP','MODV','REXR',
    ],
    'russell2000_materials': [
        'ASIX','BCPC','CLF','CSTM','HAYN','HCCI','HUN','IOSP','KALU',
        'MTRX','TROX','RYAM','POWL','MERC','GORO','USAP','ZEUS','KWR',
    ],
    'russell2000_consumer_staples': [
        'ANDE','CALM','CVGW','DXYN','FLGT','FRPT','HAIN','HRMY','JJSF','LANC',
        'LWAY','MGP','MGPI','NATR','NOMD','PZZA','RBCAA','RLGY','SAFM','SENEA',
    ],
    'russell2000_utilities': [
        'ARTNA','AWR','CLNE','CNSL','CONN','CWCO','GWRS','MSEX','NWEC','PESI',
        'SPWR','SRUN','SWX','TPVG','UONE','UONEK','UTL','YORW',
    ],
    'russell2000_comms': [
        'ATNI','CNSL','CODA','FSRV','GFAI','HBCP','IRDM','LBRD','LBRDA','LBRDK',
        'LSXMA','LSXMB','LSXMK','NXST','OOMA','SHEN','SIRI','STRP','TDS','TMUS',
    ],
}

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

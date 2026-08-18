"""
주식 열람: 검색, 시세, 재무 요약, 차트 데이터.

검색 범위:
  - 미국: S&P 500 전체 + NASDAQ 100 + 다우 30 + 인기 종목 (로컬 DB + Wikipedia 자동 갱신)
  - 국내: 코스피 200 핵심 + 코스닥 150 핵심 (~130종목)
  - 임의 티커: DB에 없어도 yfinance 직접 조회
"""
import yfinance as yf
import pandas as pd
from functools import lru_cache
from pathlib import Path
import json, time, threading

# ─────────────────────────────────────────────────────────────────────────────
# 로컬 시드 DB (섹터 정보 포함 — Wikipedia에는 섹터 없음)
# ─────────────────────────────────────────────────────────────────────────────
_SEED: list[tuple] = [
    # (ticker, name, etf, sector_kr)
    ("AAPL",  "Apple Inc.",              "XLK",  "테크"),
    ("MSFT",  "Microsoft Corp.",         "XLK",  "테크"),
    ("NVDA",  "NVIDIA Corp.",            "XLK",  "테크"),
    ("GOOGL", "Alphabet Inc. A",         "XLK",  "테크"),
    ("GOOG",  "Alphabet Inc. C",         "XLK",  "테크"),
    ("META",  "Meta Platforms",          "XLK",  "테크"),
    ("AMZN",  "Amazon.com Inc.",         "XLY",  "소비재"),
    ("TSLA",  "Tesla Inc.",              "XLY",  "소비재"),
    ("AMD",   "Advanced Micro Devices",  "XLK",  "테크"),
    ("AVGO",  "Broadcom Inc.",           "XLK",  "테크"),
    ("ORCL",  "Oracle Corp.",            "XLK",  "테크"),
    ("CRM",   "Salesforce Inc.",         "XLK",  "테크"),
    ("ADBE",  "Adobe Inc.",              "XLK",  "테크"),
    ("NFLX",  "Netflix Inc.",            "XLK",  "테크"),
    ("INTC",  "Intel Corp.",             "XLK",  "테크"),
    ("QCOM",  "Qualcomm Inc.",           "XLK",  "테크"),
    ("TXN",   "Texas Instruments",       "XLK",  "테크"),
    ("MU",    "Micron Technology",       "XLK",  "테크"),
    ("AMAT",  "Applied Materials",       "XLK",  "테크"),
    ("LRCX",  "Lam Research",           "XLK",  "테크"),
    ("KLAC",  "KLA Corp.",              "XLK",  "테크"),
    ("ASML",  "ASML Holding",           "XLK",  "테크"),
    ("TSM",   "Taiwan Semiconductor",   "XLK",  "테크"),
    ("SHOP",  "Shopify Inc.",            "XLK",  "테크"),
    ("SNOW",  "Snowflake Inc.",          "XLK",  "테크"),
    ("PLTR",  "Palantir Technologies",   "XLK",  "테크"),
    ("SMCI",  "Super Micro Computer",    "XLK",  "테크"),
    ("ARM",   "Arm Holdings",            "XLK",  "테크"),
    ("CRWD",  "CrowdStrike Holdings",    "XLK",  "테크"),
    ("PANW",  "Palo Alto Networks",      "XLK",  "테크"),
    ("FTNT",  "Fortinet Inc.",           "XLK",  "테크"),
    ("ZS",    "Zscaler Inc.",            "XLK",  "테크"),
    ("DDOG",  "Datadog Inc.",            "XLK",  "테크"),
    ("MDB",   "MongoDB Inc.",            "XLK",  "테크"),
    ("NOW",   "ServiceNow Inc.",         "XLK",  "테크"),
    ("WDAY",  "Workday Inc.",            "XLK",  "테크"),
    ("TEAM",  "Atlassian Corp.",         "XLK",  "테크"),
    ("VEEV",  "Veeva Systems",           "XLK",  "테크"),
    ("TWLO",  "Twilio Inc.",             "XLK",  "테크"),
    ("NET",   "Cloudflare Inc.",         "XLK",  "테크"),
    ("OKTA",  "Okta Inc.",               "XLK",  "테크"),
    ("RBLX",  "Roblox Corp.",            "XLK",  "테크"),
    ("U",     "Unity Software",          "XLK",  "테크"),
    ("AI",    "C3.ai Inc.",              "XLK",  "테크"),
    ("PATH",  "UiPath Inc.",             "XLK",  "테크"),
    ("SMAR",  "Smartsheet Inc.",         "XLK",  "테크"),
    ("DOCN",  "DigitalOcean",            "XLK",  "테크"),
    ("HPE",   "HP Enterprise",           "XLK",  "테크"),
    ("HPQ",   "HP Inc.",                 "XLK",  "테크"),
    ("DELL",  "Dell Technologies",       "XLK",  "테크"),
    ("ACN",   "Accenture plc",           "XLK",  "테크"),
    ("IBM",   "IBM Corp.",               "XLK",  "테크"),
    ("CSCO",  "Cisco Systems",           "XLK",  "테크"),
    ("ANET",  "Arista Networks",         "XLK",  "테크"),
    ("AKAM",  "Akamai Technologies",     "XLK",  "테크"),
    # 금융
    ("JPM",   "JPMorgan Chase",          "XLF",  "금융"),
    ("GS",    "Goldman Sachs",           "XLF",  "금융"),
    ("MS",    "Morgan Stanley",          "XLF",  "금융"),
    ("BAC",   "Bank of America",         "XLF",  "금융"),
    ("WFC",   "Wells Fargo",             "XLF",  "금융"),
    ("C",     "Citigroup Inc.",          "XLF",  "금융"),
    ("USB",   "U.S. Bancorp",            "XLF",  "금융"),
    ("PNC",   "PNC Financial",           "XLF",  "금융"),
    ("TFC",   "Truist Financial",        "XLF",  "금융"),
    ("COF",   "Capital One Financial",   "XLF",  "금융"),
    ("AXP",   "American Express",        "XLF",  "금융"),
    ("V",     "Visa Inc.",               "XLF",  "금융"),
    ("MA",    "Mastercard Inc.",         "XLF",  "금융"),
    ("PYPL",  "PayPal Holdings",         "XLF",  "금융"),
    ("SQ",    "Block Inc.",              "XLF",  "금융"),
    ("BLK",   "BlackRock Inc.",          "XLF",  "금융"),
    ("SCHW",  "Charles Schwab",          "XLF",  "금융"),
    ("COIN",  "Coinbase Global",         "XLF",  "금융"),
    ("HOOD",  "Robinhood Markets",       "XLF",  "금융"),
    ("SOFI",  "SoFi Technologies",       "XLF",  "금융"),
    ("MSTR",  "MicroStrategy",           "XLF",  "금융"),
    ("ICE",   "Intercontinental Exchange","XLF", "금융"),
    ("CME",   "CME Group",               "XLF",  "금융"),
    ("NDAQ",  "Nasdaq Inc.",             "XLF",  "금융"),
    ("SPGI",  "S&P Global Inc.",         "XLF",  "금융"),
    # 헬스케어
    ("LLY",   "Eli Lilly",              "XLV",  "헬스케어"),
    ("UNH",   "UnitedHealth Group",      "XLV",  "헬스케어"),
    ("JNJ",   "Johnson & Johnson",       "XLV",  "헬스케어"),
    ("PFE",   "Pfizer Inc.",             "XLV",  "헬스케어"),
    ("ABBV",  "AbbVie Inc.",             "XLV",  "헬스케어"),
    ("MRK",   "Merck & Co.",             "XLV",  "헬스케어"),
    ("TMO",   "Thermo Fisher",           "XLV",  "헬스케어"),
    ("DHR",   "Danaher Corp.",           "XLV",  "헬스케어"),
    ("ISRG",  "Intuitive Surgical",      "XLV",  "헬스케어"),
    ("BSX",   "Boston Scientific",       "XLV",  "헬스케어"),
    ("MDT",   "Medtronic plc",           "XLV",  "헬스케어"),
    ("ABT",   "Abbott Laboratories",     "XLV",  "헬스케어"),
    ("AMGN",  "Amgen Inc.",              "XLV",  "헬스케어"),
    ("GILD",  "Gilead Sciences",         "XLV",  "헬스케어"),
    ("BIIB",  "Biogen Inc.",             "XLV",  "헬스케어"),
    ("REGN",  "Regeneron Pharma",        "XLV",  "헬스케어"),
    ("VRTX",  "Vertex Pharmaceuticals",  "XLV",  "헬스케어"),
    ("CVS",   "CVS Health",             "XLV",  "헬스케어"),
    ("CI",    "Cigna Group",             "XLV",  "헬스케어"),
    ("HCA",   "HCA Healthcare",          "XLV",  "헬스케어"),
    ("MRNA",  "Moderna Inc.",            "XLV",  "헬스케어"),
    ("BNTX",  "BioNTech SE",            "XLV",  "헬스케어"),
    # 소비재
    ("TSLA",  "Tesla Inc.",              "XLY",  "소비재"),
    ("HD",    "Home Depot",             "XLY",  "소비재"),
    ("LOW",   "Lowe's Companies",        "XLY",  "소비재"),
    ("NKE",   "Nike Inc.",               "XLY",  "소비재"),
    ("SBUX",  "Starbucks Corp.",         "XLY",  "소비재"),
    ("MCD",   "McDonald's Corp.",        "XLY",  "소비재"),
    ("CMG",   "Chipotle Mexican Grill",  "XLY",  "소비재"),
    ("YUM",   "Yum! Brands",            "XLY",  "소비재"),
    ("ABNB",  "Airbnb Inc.",             "XLY",  "소비재"),
    ("BKNG",  "Booking Holdings",        "XLY",  "소비재"),
    ("EXPE",  "Expedia Group",           "XLY",  "소비재"),
    ("UBER",  "Uber Technologies",       "XLI",  "산업재"),
    ("LYFT",  "Lyft Inc.",               "XLI",  "산업재"),
    ("DASH",  "DoorDash Inc.",           "XLY",  "소비재"),
    ("ETSY",  "Etsy Inc.",               "XLY",  "소비재"),
    ("EBAY",  "eBay Inc.",               "XLY",  "소비재"),
    ("TGT",   "Target Corp.",            "XLP",  "필수소비"),
    ("DG",    "Dollar General",          "XLP",  "필수소비"),
    # 필수소비
    ("PG",    "Procter & Gamble",        "XLP",  "필수소비"),
    ("KO",    "Coca-Cola Co.",           "XLP",  "필수소비"),
    ("PEP",   "PepsiCo Inc.",            "XLP",  "필수소비"),
    ("WMT",   "Walmart Inc.",            "XLP",  "필수소비"),
    ("COST",  "Costco Wholesale",        "XLP",  "필수소비"),
    ("PM",    "Philip Morris",           "XLP",  "필수소비"),
    ("MO",    "Altria Group",            "XLP",  "필수소비"),
    ("MDLZ",  "Mondelez International",  "XLP",  "필수소비"),
    ("GIS",   "General Mills",           "XLP",  "필수소비"),
    ("KHC",   "Kraft Heinz Co.",         "XLP",  "필수소비"),
    ("CL",    "Colgate-Palmolive",       "XLP",  "필수소비"),
    # 에너지
    ("XOM",   "Exxon Mobil",            "XLE",  "에너지"),
    ("CVX",   "Chevron Corp.",           "XLE",  "에너지"),
    ("COP",   "ConocoPhillips",          "XLE",  "에너지"),
    ("SLB",   "Schlumberger",            "XLE",  "에너지"),
    ("EOG",   "EOG Resources",           "XLE",  "에너지"),
    ("PXD",   "Pioneer Natural",         "XLE",  "에너지"),
    ("MPC",   "Marathon Petroleum",      "XLE",  "에너지"),
    ("VLO",   "Valero Energy",           "XLE",  "에너지"),
    ("OXY",   "Occidental Petroleum",    "XLE",  "에너지"),
    ("HAL",   "Halliburton Co.",         "XLE",  "에너지"),
    # 산업재
    ("CAT",   "Caterpillar Inc.",        "XLI",  "산업재"),
    ("DE",    "Deere & Company",         "XLI",  "산업재"),
    ("BA",    "Boeing Co.",              "XLI",  "산업재"),
    ("GE",    "GE Aerospace",            "XLI",  "산업재"),
    ("HON",   "Honeywell",               "XLI",  "산업재"),
    ("RTX",   "RTX Corp.",               "XLI",  "산업재"),
    ("LMT",   "Lockheed Martin",         "XLI",  "산업재"),
    ("NOC",   "Northrop Grumman",        "XLI",  "산업재"),
    ("GD",    "General Dynamics",        "XLI",  "산업재"),
    ("UPS",   "United Parcel Service",   "XLI",  "산업재"),
    ("FDX",   "FedEx Corp.",             "XLI",  "산업재"),
    ("CSX",   "CSX Corp.",               "XLI",  "산업재"),
    ("UNP",   "Union Pacific",           "XLI",  "산업재"),
    ("NSC",   "Norfolk Southern",        "XLI",  "산업재"),
    ("WM",    "Waste Management",        "XLI",  "산업재"),
    ("MMM",   "3M Company",              "XLI",  "산업재"),
    ("EMR",   "Emerson Electric",        "XLI",  "산업재"),
    ("ETN",   "Eaton Corp.",             "XLI",  "산업재"),
    ("PH",    "Parker Hannifin",         "XLI",  "산업재"),
    ("ROK",   "Rockwell Automation",     "XLI",  "산업재"),
    # 유틸리티
    ("NEE",   "NextEra Energy",          "XLU",  "유틸리티"),
    ("DUK",   "Duke Energy",             "XLU",  "유틸리티"),
    ("SO",    "Southern Company",        "XLU",  "유틸리티"),
    ("D",     "Dominion Energy",         "XLU",  "유틸리티"),
    ("AEP",   "American Electric Power", "XLU",  "유틸리티"),
    ("EXC",   "Exelon Corp.",            "XLU",  "유틸리티"),
    # 리츠
    ("AMT",   "American Tower",         "XLRE", "리츠"),
    ("PLD",   "Prologis Inc.",           "XLRE", "리츠"),
    ("EQIX",  "Equinix Inc.",            "XLRE", "리츠"),
    ("CCI",   "Crown Castle Inc.",       "XLRE", "리츠"),
    ("SPG",   "Simon Property Group",    "XLRE", "리츠"),
    ("O",     "Realty Income",           "XLRE", "리츠"),
    # 소재
    ("LIN",   "Linde plc",              "XLB",  "소재"),
    ("FCX",   "Freeport-McMoRan",        "XLB",  "소재"),
    ("NEM",   "Newmont Corp.",           "XLB",  "소재"),
    ("ALB",   "Albemarle Corp.",         "XLB",  "소재"),
    ("CF",    "CF Industries",           "XLB",  "소재"),
    ("MOS",   "Mosaic Company",          "XLB",  "소재"),
    # 통신
    ("TMUS",  "T-Mobile US",             "XLK",  "통신"),
    ("VZ",    "Verizon Communications",  "XLK",  "통신"),
    ("T",     "AT&T Inc.",               "XLK",  "통신"),
    ("CMCSA", "Comcast Corp.",           "XLK",  "통신"),
    ("CHTR",  "Charter Communications", "XLK",  "통신"),
    ("PARA",  "Paramount Global",        "XLK",  "통신"),
    ("DIS",   "Walt Disney Co.",         "XLK",  "통신"),
    ("WBD",   "Warner Bros. Discovery",  "XLK",  "통신"),
    # 암호화폐 관련 ETF
    ("IBIT",  "iShares Bitcoin ETF",     "XLF",  "ETF"),
    ("FBTC",  "Fidelity Bitcoin ETF",    "XLF",  "ETF"),
    ("GBTC",  "Grayscale Bitcoin Trust", "XLF",  "ETF"),
    ("BITO",  "ProShares Bitcoin ETF",   "XLF",  "ETF"),
    ("MARA",  "Marathon Digital",        "XLF",  "테크"),
    ("RIOT",  "Riot Platforms",          "XLF",  "테크"),
    ("CLSK",  "CleanSpark Inc.",         "XLF",  "테크"),
    # 인기 ETF (시장 지수)
    ("SPY",   "SPDR S&P 500 ETF",       "XLK",  "ETF"),
    ("QQQ",   "Invesco QQQ Trust",       "XLK",  "ETF"),
    ("IWM",   "iShares Russell 2000",    "XLK",  "ETF"),
    ("VTI",   "Vanguard Total Stock",    "XLK",  "ETF"),
    ("VOO",   "Vanguard S&P 500",        "XLK",  "ETF"),
    ("GLD",   "SPDR Gold Shares",        "XLB",  "ETF"),
    ("SLV",   "iShares Silver Trust",    "XLB",  "ETF"),
    ("TLT",   "iShares 20Y Treasury",    "XLF",  "ETF"),
    ("HYG",   "iShares HY Bond",         "XLF",  "ETF"),
    ("XLK",   "Technology Select SPDR",  "XLK",  "ETF"),
    ("XLF",   "Financial Select SPDR",   "XLF",  "ETF"),
    ("XLE",   "Energy Select SPDR",      "XLE",  "ETF"),
    ("XLV",   "Health Care Select SPDR", "XLV",  "ETF"),
    ("ARKK",  "ARK Innovation ETF",      "XLK",  "ETF"),
    ("SOXX",  "iShares Semiconductor",   "XLK",  "ETF"),
    ("SMH",   "VanEck Semiconductor",    "XLK",  "ETF"),
]

# 코스피 200 핵심 + 코스닥 150 핵심 (~130종목)
KR_STOCKS: list[tuple] = [
    # 코스피 시총 상위
    ("005930.KS", "삼성전자",           "KR", "국내"),
    ("000660.KS", "SK하이닉스",         "KR", "국내"),
    ("373220.KS", "LG에너지솔루션",     "KR", "국내"),
    ("207940.KS", "삼성바이오로직스",   "KR", "국내"),
    ("005380.KS", "현대차",             "KR", "국내"),
    ("000270.KS", "기아",               "KR", "국내"),
    ("005490.KS", "POSCO홀딩스",        "KR", "국내"),
    ("035420.KS", "NAVER",             "KR", "국내"),
    ("035720.KS", "카카오",             "KR", "국내"),
    ("051910.KS", "LG화학",             "KR", "국내"),
    ("006400.KS", "삼성SDI",            "KR", "국내"),
    ("068270.KS", "셀트리온",           "KR", "국내"),
    ("105560.KS", "KB금융",             "KR", "국내"),
    ("055550.KS", "신한지주",           "KR", "국내"),
    ("012330.KS", "현대모비스",         "KR", "국내"),
    ("003670.KS", "포스코퓨처엠",       "KR", "국내"),
    ("066570.KS", "LG전자",             "KR", "국내"),
    ("323410.KS", "카카오뱅크",         "KR", "국내"),
    ("259960.KS", "크래프톤",           "KR", "국내"),
    ("042700.KS", "한미반도체",         "KR", "국내"),
    ("003550.KS", "LG",                "KR", "국내"),
    ("034730.KS", "SK",                "KR", "국내"),
    ("017670.KS", "SK텔레콤",           "KR", "국내"),
    ("030200.KS", "KT",                "KR", "국내"),
    ("032830.KS", "삼성생명",           "KR", "국내"),
    ("086790.KS", "하나금융지주",       "KR", "국내"),
    ("316140.KS", "우리금융지주",       "KR", "국내"),
    ("024110.KS", "기업은행",           "KR", "국내"),
    ("000810.KS", "삼성화재",           "KR", "국내"),
    ("033780.KS", "KT&G",              "KR", "국내"),
    ("096770.KS", "SK이노베이션",       "KR", "국내"),
    ("010130.KS", "고려아연",           "KR", "국내"),
    ("009540.KS", "HD한국조선해양",     "KR", "국내"),
    ("010140.KS", "삼성중공업",         "KR", "국내"),
    ("042660.KS", "한화오션",           "KR", "국내"),
    ("012450.KS", "한화에어로스페이스", "KR", "국내"),
    ("047050.KS", "포스코인터내셔널",   "KR", "국내"),
    ("000830.KS", "삼성물산",           "KR", "국내"),
    ("028260.KS", "삼성물산우",         "KR", "국내"),
    ("011070.KS", "LG이노텍",           "KR", "국내"),
    ("003490.KS", "대한항공",           "KR", "국내"),
    ("020150.KS", "일진머티리얼즈",     "KR", "국내"),
    ("015760.KS", "한국전력",           "KR", "국내"),
    ("000120.KS", "CJ대한통운",         "KR", "국내"),
    ("097950.KS", "CJ제일제당",         "KR", "국내"),
    ("001040.KS", "CJ",                "KR", "국내"),
    ("018260.KS", "삼성에스디에스",     "KR", "국내"),
    ("010950.KS", "S-Oil",             "KR", "국내"),
    ("051900.KS", "LG생활건강",         "KR", "국내"),
    ("090430.KS", "아모레퍼시픽",       "KR", "국내"),
    ("005180.KS", "빙그레",             "KR", "국내"),
    ("000080.KS", "하이트진로",         "KR", "국내"),
    ("021240.KS", "코웨이",             "KR", "국내"),
    ("004370.KS", "농심",               "KR", "국내"),
    ("282330.KS", "BGF리테일",          "KR", "국내"),
    ("006360.KS", "GS건설",             "KR", "국내"),
    ("000720.KS", "현대건설",           "KR", "국내"),
    ("047040.KS", "대우건설",           "KR", "국내"),
    ("071050.KS", "한국금융지주",       "KR", "국내"),
    ("138040.KS", "메리츠금융지주",     "KR", "국내"),
    ("088350.KS", "한화생명",           "KR", "국내"),
    ("078930.KS", "GS",                "KR", "국내"),
    ("004020.KS", "현대제철",           "KR", "국내"),
    ("005940.KS", "NH투자증권",         "KR", "국내"),
    ("016360.KS", "삼성증권",           "KR", "국내"),
    ("006800.KS", "미래에셋증권",       "KR", "국내"),
    ("039490.KS", "키움증권",           "KR", "국내"),
    ("267250.KS", "HD현대",             "KR", "국내"),
    ("329180.KS", "HD현대중공업",       "KR", "국내"),
    ("011210.KS", "현대위아",           "KR", "국내"),
    ("241560.KS", "두산밥캣",           "KR", "국내"),
    ("034020.KS", "두산에너빌리티",     "KR", "국내"),
    ("035250.KS", "강원랜드",           "KR", "국내"),
    ("004170.KS", "신세계",             "KR", "국내"),
    ("069960.KS", "현대백화점",         "KR", "국내"),
    ("023530.KS", "롯데쇼핑",           "KR", "국내"),
    ("007070.KS", "GS리테일",           "KR", "국내"),
    ("000990.KS", "DB하이텍",           "KR", "국내"),
    ("008770.KS", "호텔신라",           "KR", "국내"),
    ("180640.KS", "한진칼",             "KR", "국내"),
    ("007860.KS", "서연",               "KR", "국내"),
    # 코스닥 시총 상위
    ("086520.KQ", "에코프로",           "KR", "국내"),
    ("247540.KQ", "에코프로비엠",       "KR", "국내"),
    ("196170.KQ", "알테오젠",           "KR", "국내"),
    ("091990.KQ", "셀트리온헬스케어",   "KR", "국내"),
    ("068760.KQ", "셀트리온제약",       "KR", "국내"),
    ("041510.KQ", "에스엠",             "KR", "국내"),
    ("035900.KQ", "JYP Ent.",          "KR", "국내"),
    ("122870.KQ", "와이지엔터테인먼트", "KR", "국내"),
    ("263750.KQ", "펄어비스",           "KR", "국내"),
    ("036570.KQ", "엔씨소프트",         "KR", "국내"),
    ("251270.KQ", "넷마블",             "KR", "국내"),
    ("112040.KQ", "위메이드",           "KR", "국내"),
    ("293490.KQ", "카카오게임즈",       "KR", "국내"),
    ("352820.KQ", "하이브",             "KR", "국내"),
    ("357780.KQ", "솔브레인",           "KR", "국내"),
    ("039030.KQ", "이오테크닉스",       "KR", "국내"),
    ("096530.KQ", "씨젠",               "KR", "국내"),
    ("145020.KQ", "휴젤",               "KR", "국내"),
    ("214150.KQ", "클래시스",           "KR", "국내"),
    ("078600.KQ", "대주전자재료",       "KR", "국내"),
    ("240810.KQ", "원익IPS",            "KR", "국내"),
    ("403870.KQ", "HPSP",              "KR", "국내"),
    ("900340.KQ", "성광벤드",           "KR", "국내"),
    ("131970.KQ", "테크윙",             "KR", "국내"),
    ("290510.KQ", "에스케이씨솔믹스",   "KR", "국내"),
    ("054040.KQ", "한국컴퓨터",         "KR", "국내"),
    ("064760.KQ", "티씨케이",           "KR", "국내"),
    ("237880.KQ", "클리오",             "KR", "국내"),
    ("950130.KQ", "엑세스바이오",       "KR", "국내"),
    ("066970.KQ", "엘앤에프",           "KR", "국내"),
    ("058470.KQ", "리노공업",           "KR", "국내"),
    ("317030.KQ", "에코프로에이치엔",   "KR", "국내"),
    ("009150.KQ", "삼성전기",           "KR", "국내"),
    ("028300.KQ", "HLB",               "KR", "국내"),
    ("009420.KQ", "한올바이오파마",     "KR", "국내"),
    ("207940.KQ", "삼성바이오에피스",   "KR", "국내"),
]

SECTORS_KR = {
    "XLK":"💻 테크","XLF":"🏦 금융","XLE":"⛽ 에너지","XLV":"💊 헬스케어",
    "XLY":"🛒 소비재","XLI":"🏭 산업재","XLP":"🧴 필수소비",
    "XLB":"🏗️ 소재","XLU":"💡 유틸리티","XLRE":"🏠 리츠",
    "KR":"🇰🇷 국내",
}

# ─────────────────────────────────────────────────────────────────────────────
# 전체 DB: 시드 + 국내 + Wikipedia S&P500 보강
# ─────────────────────────────────────────────────────────────────────────────
_DB_LOCK = threading.Lock()
_DB: list[tuple] | None = None          # (ticker, name, etf, sector_kr)
_DB_INDEX: dict[str, tuple] | None = None  # ticker → row

_WIKI_CACHE = Path(__file__).parent / "stock_db_cache.json"
_WIKI_TTL   = 86400 * 7  # 7일


def _load_wiki_cache() -> dict:
    if _WIKI_CACHE.exists():
        try:
            d = json.loads(_WIKI_CACHE.read_text())
            if time.time() - d.get("_ts", 0) < _WIKI_TTL:
                return d
        except Exception:
            pass
    return {}


def _save_wiki_cache(data: dict):
    data["_ts"] = time.time()
    try:
        _WIKI_CACHE.write_text(json.dumps(data))
    except Exception:
        pass


def _fetch_sp500_wiki() -> list[tuple]:
    """Wikipedia에서 S&P 500 전체 목록 + 섹터 정보 가져오기."""
    try:
        import urllib.request
        from io import StringIO
        req = urllib.request.Request(
            "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
            headers={"User-Agent": "Mozilla/5.0"}
        )
        html = urllib.request.urlopen(req, timeout=20).read().decode("utf-8", "ignore")
        tables = pd.read_html(StringIO(html))
        for tbl in tables:
            cols = [str(c).strip() for c in tbl.columns]
            if "Symbol" in cols and "Security" in cols:
                rows = []
                sector_col = next((c for c in cols if "GICS" in c and "Sector" in c and "Sub" not in c), None)
                for _, row in tbl.iterrows():
                    sym = str(row["Symbol"]).strip().replace(".", "-")
                    name = str(row["Security"]).strip()
                    sec_en = str(row[sector_col]).strip() if sector_col else ""
                    if not sym or sym == "nan" or len(sym) > 6: continue
                    rows.append((sym, name, "XLK", sec_en))  # etf placeholder
                if len(rows) >= 400:
                    return rows
    except Exception:
        pass
    return []


def _get_db() -> tuple[list[tuple], dict[str, tuple]]:
    global _DB, _DB_INDEX
    if _DB is not None:
        return _DB, _DB_INDEX

    with _DB_LOCK:
        if _DB is not None:
            return _DB, _DB_INDEX

        # 1) 시드 + 국내
        combined: dict[str, tuple] = {}
        for row in _SEED + KR_STOCKS:
            combined[row[0].upper()] = row

        # 2) Wikipedia S&P500 캐시 확인
        cache = _load_wiki_cache()
        sp500_rows = cache.get("sp500", [])
        if not sp500_rows:
            sp500_rows = _fetch_sp500_wiki()
            if sp500_rows:
                cache["sp500"] = sp500_rows
                _save_wiki_cache(cache)

        # 영문 섹터 → 한국어 매핑
        _en_to_kr = {
            "Information Technology": "테크", "Financials": "금융",
            "Health Care": "헬스케어", "Consumer Discretionary": "소비재",
            "Consumer Staples": "필수소비", "Energy": "에너지",
            "Industrials": "산업재", "Materials": "소재",
            "Utilities": "유틸리티", "Real Estate": "리츠",
            "Communication Services": "통신",
        }
        for row in sp500_rows:
            sym = row[0].upper()
            if sym not in combined:
                sec_kr = _en_to_kr.get(row[3], row[3]) if len(row) > 3 else ""
                combined[sym] = (sym, row[1], "XLK", sec_kr)

        _DB = list(combined.values())
        _DB_INDEX = {r[0].upper(): r for r in _DB}
        return _DB, _DB_INDEX


# 백그라운드에서 미리 로드 (앱 시작 시 지연 없도록)
threading.Thread(target=_get_db, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# 검색 API
# ─────────────────────────────────────────────────────────────────────────────
def search(query: str, sector: str = "전체") -> list[dict]:
    """티커 또는 회사명으로 검색 (미국 + 국내).

    - 로컬 DB (~700종목) 먼저 검색
    - 매칭 없으면 yfinance 직접 조회 (임의 티커 지원)
    """
    q = query.strip()
    if not q:
        return []
    q_up = q.upper()

    db, _ = _get_db()
    results = []
    for ticker, name, etf, sec_kr in db:
        if sector != "전체" and sec_kr != sector:
            continue
        if q_up in ticker.upper() or q.lower() in name.lower():
            results.append({"ticker": ticker, "name": name, "etf": etf, "sector": sec_kr})

    # 정렬: 티커 시작 매칭 우선
    results.sort(key=lambda r: (
        0 if r["ticker"].upper().startswith(q_up) else 1,
        len(r["ticker"])
    ))

    # DB에 없는 티커 → yfinance 직접 시도
    if not results and len(q) <= 8 and q_up.replace("-", "").replace(".", "").isalnum():
        try:
            info = yf.Ticker(q_up).info or {}
            name = info.get("shortName") or info.get("longName") or q_up
            if name and name != q_up:
                results.append({
                    "ticker": q_up, "name": name,
                    "etf": "XLK", "sector": info.get("sector", "기타")
                })
        except Exception:
            pass

    return results[:80]  # 최대 80개


def search_kr(query: str) -> list[dict]:
    """국내 종목만 검색."""
    q = query.strip().lower()
    if not q:
        return [{"ticker": t, "name": n, "etf": e, "sector": s}
                for t, n, e, s in KR_STOCKS][:60]
    return [
        {"ticker": t, "name": n, "etf": e, "sector": s}
        for t, n, e, s in KR_STOCKS
        if q in t.lower() or q in n.lower()
    ][:60]


# ─────────────────────────────────────────────────────────────────────────────
# 시세 / 차트 API
# ─────────────────────────────────────────────────────────────────────────────
def get_quote(ticker: str) -> dict:
    try:
        t    = yf.Ticker(ticker)
        info = t.info or {}
        fi   = t.fast_info
        price   = float(getattr(fi, "last_price", 0) or info.get("currentPrice", 0))
        prev    = float(info.get("previousClose", price) or price)
        chg     = price - prev
        chg_pct = chg / prev if prev else 0
        return {
            "ticker":      ticker,
            "name":        info.get("shortName", ticker),
            "price":       price,
            "change":      chg,
            "change_pct":  chg_pct,
            "volume":      int(getattr(fi, "three_month_average_volume", 0) or 0),
            "market_cap":  info.get("marketCap", 0),
            "pe":          info.get("trailingPE"),
            "forward_pe":  info.get("forwardPE"),
            "eps":         info.get("trailingEps"),
            "revenue_growth": info.get("revenueGrowth"),
            "target_price": info.get("targetMeanPrice"),
            "sector":      info.get("sector", ""),
            "52w_high":    info.get("fiftyTwoWeekHigh"),
            "52w_low":     info.get("fiftyTwoWeekLow"),
            "description": info.get("longBusinessSummary", "")[:300],
        }
    except Exception:
        return {"ticker": ticker, "name": ticker, "price": 0, "change": 0,
                "change_pct": 0, "volume": 0, "market_cap": 0}


def get_ohlcv(ticker: str, period: str = "6mo") -> pd.DataFrame:
    try:
        df = yf.download(ticker, period=period, interval="1d",
                         auto_adjust=True, progress=False)
        return df if df is not None and len(df) > 5 else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


def get_bulk_quotes(tickers: list[str]) -> list[dict]:
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(get_quote, tickers))
    return results


# ─────────────────────────────────────────────────────────────────────────────
# 빠른 배치 스냅샷 (yf.download 단일 요청)
# ─────────────────────────────────────────────────────────────────────────────

def get_batch_snapshot(tickers: list[str], period: str = "6d") -> dict[str, dict]:
    """
    yf.download 단일 요청으로 전체 OHLCV 조회.
    개별 info API 대비 ~10배 빠름.
    반환: {ticker: {price, prev, change_pct, volume, sparkline(list)}}
    """
    if not tickers:
        return {}
    tickers_str = " ".join(tickers)
    try:
        raw = yf.download(
            tickers_str, period=period, interval="1d",
            auto_adjust=True, progress=False,
        )
        if raw is None or raw.empty:
            return {}
    except Exception:
        return {}

    result: dict[str, dict] = {}
    try:
        close_raw  = raw["Close"]
        volume_raw = raw["Volume"]

        def _col(df, tk: str) -> pd.Series:
            # DataFrame(multi-ticker) 또는 Series(single-ticker) 모두 처리
            if isinstance(df, pd.Series):
                return df
            if tk in df.columns:
                return df[tk]
            return pd.Series(dtype=float)

        # 시가총액: fast_info 배치 (별도 스레드)
        mcap_map: dict[str, float] = {}
        def _fetch_mcap(tk: str):
            try:
                mc = yf.Ticker(tk).fast_info.market_cap
                if mc:
                    mcap_map[tk] = float(mc)
            except Exception:
                pass
        import concurrent.futures as _cf
        with _cf.ThreadPoolExecutor(max_workers=min(len(tickers), 8)) as ex:
            list(ex.map(_fetch_mcap, tickers))

        for tk in tickers:
            try:
                c = _col(close_raw,  tk).dropna()
                v = _col(volume_raw, tk).dropna()
                if len(c) < 2:
                    continue
                price = float(c.iloc[-1])
                prev  = float(c.iloc[-2])
                chg   = (price - prev) / prev if prev else 0
                vol   = int(v.iloc[-1]) if not v.empty else 0
                spark = [float(x) for x in c.tolist()]
                result[tk] = {"price": price, "prev": prev, "change_pct": chg,
                              "volume": vol, "sparkline": spark,
                              "market_cap": mcap_map.get(tk, _APPROX_MCAP.get(tk, 0) * 1e8)}
            except Exception:
                continue
    except Exception:
        pass
    return result


def sparkline_svg(values: list[float], width: int = 64, height: int = 24) -> str:
    """가격 시계열을 인라인 SVG 스파크라인으로 변환."""
    if len(values) < 2:
        return ""
    mn, mx = min(values), max(values)
    rng = mx - mn or max(abs(mx), 1e-9)
    n = len(values)
    pts = " ".join(
        f"{i / (n-1) * width:.1f},{height - (v - mn) / rng * height:.1f}"
        for i, v in enumerate(values)
    )
    up = values[-1] >= values[0]
    color = "#F04452" if up else "#2F80ED"
    fill_color = "rgba(240,68,82,0.08)" if up else "rgba(47,128,237,0.08)"
    # 닫힌 폴리곤 (fill용)
    closed = f"{pts} {width},{height} 0,{height}"
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<polygon points="{closed}" fill="{fill_color}"/>'
        f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.6" stroke-linecap="round"/>'
        f'</svg>'
    )


# 미국 대표 유니버스 (시총 순 - 정렬 기준으로 활용)
US_TOP = [
    "AAPL","MSFT","NVDA","GOOGL","AMZN","META","TSLA","AVGO","LLY","JPM",
    "V","WMT","UNH","MA","ORCL","XOM","COST","NFLX","HD","BAC","PLTR","AMD",
    "CSCO","ABBV","CRM","PEP","MRK","ACN","MS","TMO","NOW","ADBE","GS","TXN",
    "QCOM","GE","ISRG","PM","IBM","RTX","NEE","DHR","AMGN","INTC","PYPL",
    "SQ","COIN","MSTR","SMCI","ARM","CRWD","PANW","DDOG","NET","SNOW","MDB",
    "UBER","LYFT","ABNB","BKNG","NKE","SBUX","MCD","CMG","SPY","QQQ","SOXX",
    "IBIT","GBTC","MARA","RIOT","HOOD","SOFI","PLTR","SHOP","RBLX","ARKG",
]

KR_TOP = [
    "005930.KS","000660.KS","373220.KS","207940.KS","005380.KS","000270.KS",
    "005490.KS","035420.KS","035720.KS","051910.KS","006400.KS","068270.KS",
    "105560.KS","055550.KS","086790.KS","316140.KS","012330.KS","003670.KS",
    "066570.KS","323410.KS","259960.KS","042700.KS","034730.KS","017670.KS",
    "030200.KS","003550.KS","009540.KS","010140.KS","042660.KS","012450.KS",
    "086520.KQ","247540.KQ","196170.KQ","091990.KQ","352820.KQ","028300.KQ",
    "041510.KQ","035900.KQ","122870.KQ","036570.KQ","066970.KQ","058470.KQ",
]

# 대략적 시가총액 (단위: 억 달러) — 정렬용 캐시
_APPROX_MCAP: dict[str, float] = {
    "AAPL":45000,"MSFT":31000,"NVDA":52000,"GOOGL":43000,"AMZN":26000,
    "META":15000,"TSLA":15000,"AVBO":22000,"LLY":7000,"JPM":8000,
    "V":6000,"WMT":7000,"UNH":5000,"MA":5000,"ORCL":5000,"XOM":5000,
    "COST":4000,"NFLX":4000,"HD":3500,"BAC":3400,"AMD":2500,"CSCO":2500,
    "ABBV":3000,"CRM":3000,"ADBE":2200,"GS":2000,"MS":1800,"TMO":1800,
    "NOW":2000,"QCOM":2000,"IBM":2000,"GE":2000,"PLTR":2700,"AMGN":1500,
    "SMCI":300,"ARM":1500,"CRWD":1200,"PANW":1100,"DDOG":500,"NET":400,
    "SNOW":300,"MDB":200,"UBER":1700,"COIN":700,"MSTR":1000,"SHOP":1200,
    "IBIT":6000,"SPY":6000,"QQQ":3000,
}


# DB 크기 확인용
def db_size() -> int:
    db, _ = _get_db()
    return len(db)


# 하위호환: ALL_STOCKS
ALL_STOCKS = _SEED + KR_STOCKS
SP500_SAMPLE = _SEED

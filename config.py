"""
Trading system configuration.
API 키는 .env 파일에 저장하세요 (.env.example 참고).
"""

import os
from pathlib import Path

# .env 파일 자동 로드 (python-dotenv 없어도 직접 파싱)
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    for _line in _env_path.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# --- Alpaca (paper trading by default) ---
ALPACA_API_KEY  = os.environ.get("ALPACA_API_KEY",  "your_alpaca_api_key")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "your_alpaca_secret_key")
ALPACA_BASE_URL = os.environ.get("ALPACA_BASE_URL",  "https://paper-api.alpaca.markets")

# --- Finnhub (free tier: 60 req/min) ---
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "your_finnhub_api_key")

# --- 토스증권 오픈 API (사전 신청 단계 — 정식 스펙 공개 시 채움) ---
# 토스증권 PC 웹에서 발급한 키. 국내+해외 통합 REST/WebSocket.
TOSS_APP_KEY    = os.environ.get("TOSS_APP_KEY",    "여기에_토스_APP_KEY_입력")
TOSS_APP_SECRET = os.environ.get("TOSS_APP_SECRET", "여기에_토스_APP_SECRET_입력")
TOSS_BASE_URL   = os.environ.get("TOSS_BASE_URL",   "https://openapi.tossinvest.com")  # TODO(toss): 정식 호스트로 교체

# --- 사용할 브로커 선택: "alpaca"(미국, 기본) | "toss"(국내+해외, 출시 후) ---
BROKER = os.environ.get("BROKER", "alpaca").strip().lower()

# --- Universe: stocks to scan ---
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "META", "GOOGL", "AMZN", "TSLA",
    "AMD", "AVGO", "CRM", "ORCL", "ADBE", "NFLX", "SHOP",
    "JPM", "GS", "MS", "BAC",
    "XOM", "CVX", "LLY", "UNH", "JNJ",
    # Sector ETFs for rotation signal
    "XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLB", "XLU", "XLRE",
]

# Stocks-only universe (exclude ETFs from trading, use ETFs only for sector signal)
SECTOR_ETFS = {"XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLB", "XLU", "XLRE"}
TRADEABLE_UNIVERSE = [s for s in WATCHLIST if s not in SECTOR_ETFS]

# --- Signal weights (must sum to 1.0) ---
SIGNAL_WEIGHTS = {
    "institutional": 0.30,   # unusual volume / options flow
    "sentiment":     0.25,   # news + social sentiment
    "sector":        0.25,   # sector rotation momentum
    "fundamental":   0.20,   # earnings surprise + revenue growth
}

# --- Portfolio rules ---
CAPITAL_TOTAL = 10_000          # total capital in USD (your "bullets")
MAX_POSITIONS = 5               # max concurrent holdings
MAX_POSITION_PCT = 0.25         # max 25% of capital in one stock
MIN_SCORE_TO_BUY = 60           # score threshold (0-100) to open position
SELL_SCORE_THRESHOLD = 35       # exit when score drops below this

# Holding period (days) based on signal score
HOLD_DAYS_STRONG = 60           # score >= 75 → hold up to 60 days
HOLD_DAYS_MEDIUM = 30           # score 60-74 → hold up to 30 days

# Stop-loss and take-profit
STOP_LOSS_PCT = 0.07            # exit if -7% from entry
TAKE_PROFIT_PCT = 0.20          # partial exit at +20%

# --- Scoring lookback windows ---
VOLUME_LOOKBACK_DAYS = 20       # days for average volume baseline
SENTIMENT_LOOKBACK_DAYS = 7     # days of news to aggregate
SECTOR_LOOKBACK_DAYS = 30       # days for sector momentum
FUNDAMENTAL_LOOKBACK_QUARTERS = 4

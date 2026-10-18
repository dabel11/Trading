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

# 플레이스홀더 표식(다국어) — 미설정 키 감지용
_PLACEHOLDER_MARKS = ("your_", "여기에", "xxx", "changeme", "replace")


def _key_set(k: str) -> bool:
    """키가 실제로 설정됐는지(플레이스홀더/빈값/너무 짧음 제외)."""
    k = str(k or "").strip()
    return bool(k) and len(k) >= 16 and not any(m in k.lower() for m in _PLACEHOLDER_MARKS) \
        and "여기에" not in k


def alpaca_ready() -> bool:
    """실거래(Alpaca) 주문을 낼 수 있는 유효 키가 설정돼 있는지."""
    return _key_set(ALPACA_API_KEY) and _key_set(ALPACA_SECRET_KEY)


def alpaca_key_kind() -> str:
    """'paper'(PK…) | 'live'(AK…) | '' — 키 접두사로 판별."""
    k = str(ALPACA_API_KEY or "")
    return "paper" if k.startswith("PK") else ("live" if k.startswith("AK") else "")


def _env_targets() -> list:
    """키를 저장할 .env — 현재 실행 위치 + 짝(개발 폴더 ↔ 앱 번들).
    .app 을 더블클릭하면 번들 안의 별도 .env 를 읽으므로 양쪽을 함께 맞춘다."""
    here = Path(__file__).resolve().parent
    dev = Path.home() / "trade"
    cands = [here, dev, dev / "AI 트레이딩.app" / "Contents" / "Resources" / "trade"]
    out = []
    for d in cands:
        f = d / ".env"
        if (d / "config.py").exists() and f not in out:
            out.append(f)
    return out


def _set_env_lines(path: Path, kv: dict):
    import tempfile
    lines = path.read_text().splitlines() if path.exists() else []
    seen, out = set(), []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line and not line.lstrip().startswith("#") else None
        if key in kv:
            if key not in seen:
                out.append(f"{key}={kv[key]}")
                seen.add(key)
            continue                     # 중복 정의 제거
        out.append(line)
    for k, v in kv.items():
        if k not in seen:
            out.append(f"{k}={v}")
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".env.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def save_alpaca_keys(key_id: str, secret: str) -> str:
    """Alpaca 키를 .env(실행 위치+짝)에 저장하고 실행 중인 프로세스에도 즉시 반영.
    BASE_URL 은 키 종류로 자동 결정(PK→페이퍼, AK→실계좌). 반환: base_url."""
    global ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL
    key_id, secret = key_id.strip(), secret.strip()
    base = ("https://api.alpaca.markets" if key_id.startswith("AK")
            else "https://paper-api.alpaca.markets")
    kv = {"ALPACA_API_KEY": key_id, "ALPACA_SECRET_KEY": secret, "ALPACA_BASE_URL": base}
    for f in _env_targets():
        try:
            _set_env_lines(f, kv)
        except Exception:
            pass
    os.environ.update(kv)
    ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL = key_id, secret, base
    try:                                   # broker 는 import 시점 값을 들고 있으므로 갱신
        import sys as _sys
        b = _sys.modules.get("broker")
        if b is not None:
            b.ALPACA_API_KEY, b.ALPACA_SECRET_KEY, b.ALPACA_BASE_URL = key_id, secret, base
    except Exception:
        pass
    return base

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

"""
시장 시간 판별 유틸 (미국 정규장 기준).

휴장일은 단순화: 주말만 제외 (공휴일은 yfinance가 빈 데이터 반환하므로 자연 처리).
"""

from datetime import datetime, time as dtime
try:
    import pytz
    _ET = pytz.timezone("America/New_York")
except Exception:
    pytz = None
    _ET = None

# 미국 정규장 09:30 ~ 16:00 ET
OPEN_T  = dtime(9, 30)
CLOSE_T = dtime(16, 0)


def now_et() -> datetime:
    if _ET:
        return datetime.now(_ET)
    return datetime.utcnow()   # 폴백 (정확도 낮음)


def is_market_open(dt: datetime | None = None) -> bool:
    """미국 정규장 개장 여부 (평일 09:30~16:00 ET)."""
    dt = dt or now_et()
    if dt.weekday() >= 5:          # 토(5)·일(6)
        return False
    return OPEN_T <= dt.time() <= CLOSE_T


def seconds_until_open(dt: datetime | None = None) -> float:
    """다음 개장까지 남은 초. 이미 개장 중이면 0."""
    dt = dt or now_et()
    if is_market_open(dt):
        return 0.0
    from datetime import timedelta
    target = dt.replace(hour=OPEN_T.hour, minute=OPEN_T.minute,
                        second=0, microsecond=0)
    if dt.time() > OPEN_T:           # 오늘 개장 이미 지남 → 내일
        target += timedelta(days=1)
    while target.weekday() >= 5:     # 주말 건너뜀
        target += timedelta(days=1)
    return max(0.0, (target - dt).total_seconds())


def market_status_label() -> tuple[str, bool]:
    """('개장 중' / '마감', is_open) 반환."""
    op = is_market_open()
    return ("개장 중" if op else "마감", op)

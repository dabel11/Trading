"""
시장 시간 판별 유틸 (미국 정규장 기준).

NYSE 규칙 기반 캘린더: 주말 + 정규 휴장일(관측일 규칙 포함) + 13:00 조기폐장.
예측 불가한 임시 휴장(국가 애도일 등)은 반영되지 않는다 — 실거래에선 브로커
시계(Alpaca get_clock)로 한 번 더 확인하는 것이 안전하다.
"""

from datetime import date, datetime, time as dtime, timedelta
try:
    import pytz
    _ET = pytz.timezone("America/New_York")
except Exception:
    pytz = None
    _ET = None

# 미국 정규장 09:30 ~ 16:00 ET (조기폐장일은 13:00)
OPEN_T  = dtime(9, 30)
CLOSE_T = dtime(16, 0)
EARLY_CLOSE_T = dtime(13, 0)


def now_et() -> datetime:
    if _ET:
        return datetime.now(_ET)
    return datetime.utcnow()   # 폴백 (정확도 낮음)


# ── NYSE 캘린더 ────────────────────────────────────────────────────────────

def _nth_weekday(y: int, m: int, weekday: int, n: int) -> date:
    """y년 m월의 n번째 weekday(월=0). n=-1 이면 마지막."""
    if n > 0:
        d = date(y, m, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = date(y, m + 1, 1) - timedelta(days=1) if m < 12 else date(y, 12, 31)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def _easter(y: int) -> date:
    """그레고리력 부활절 (Anonymous Gregorian algorithm)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(y, month, day)


def _observed(d: date) -> date:
    """토요일 휴일 → 금요일, 일요일 휴일 → 월요일."""
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


_HOLIDAY_CACHE: dict = {}


def nyse_holidays(y: int) -> set:
    if y in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[y]
    h = set()
    # 신정: 일요일이면 월요일 대체, 토요일이면 대체 없음(전년 12/31 개장)
    nyd = date(y, 1, 1)
    if nyd.weekday() == 6:
        h.add(nyd + timedelta(days=1))
    elif nyd.weekday() != 5:
        h.add(nyd)
    h.add(_nth_weekday(y, 1, 0, 3))              # MLK Day (1월 셋째 월)
    h.add(_nth_weekday(y, 2, 0, 3))              # Presidents' Day (2월 셋째 월)
    h.add(_easter(y) - timedelta(days=2))        # Good Friday
    h.add(_nth_weekday(y, 5, 0, -1))             # Memorial Day (5월 마지막 월)
    if y >= 2022:
        h.add(_observed(date(y, 6, 19)))         # Juneteenth
    h.add(_observed(date(y, 7, 4)))              # Independence Day
    h.add(_nth_weekday(y, 9, 0, 1))              # Labor Day (9월 첫째 월)
    h.add(_nth_weekday(y, 11, 3, 4))             # Thanksgiving (11월 넷째 목)
    h.add(_observed(date(y, 12, 25)))            # Christmas
    _HOLIDAY_CACHE[y] = h
    return h


def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in nyse_holidays(d.year)


def close_time(d: date) -> dtime:
    """그날 정규장 마감 시각. 7/3·추수감사절 다음날·12/24 는 13:00 조기폐장."""
    jul3 = date(d.year, 7, 3)
    if d == jul3 and d.weekday() < 4:                 # 7/4 가 화~금일 때만
        return EARLY_CLOSE_T
    if d == _nth_weekday(d.year, 11, 3, 4) + timedelta(days=1):
        return EARLY_CLOSE_T
    if d == date(d.year, 12, 24) and d.weekday() < 4:  # 12/25 가 화~금일 때만
        return EARLY_CLOSE_T
    return CLOSE_T


def trading_day(dt: datetime | None = None) -> date:
    """현재 ET 기준 날짜 — 장부·킬스위치·스케줄의 '하루' 경계.

    로컬(KST) 날짜는 자정이 미국 장중(ET 10~11시)이라 하루 경계로 쓰면 안 된다."""
    return (dt or now_et()).date()


def is_market_open(dt: datetime | None = None) -> bool:
    """미국 정규장 개장 여부 (휴장일·조기폐장 반영)."""
    dt = dt or now_et()
    d = dt.date()
    if not is_trading_day(d):
        return False
    return OPEN_T <= dt.time() < close_time(d)


def seconds_until_open(dt: datetime | None = None) -> float:
    """다음 개장까지 남은 초. 이미 개장 중이면 0."""
    dt = dt or now_et()
    if is_market_open(dt):
        return 0.0
    target = dt.replace(hour=OPEN_T.hour, minute=OPEN_T.minute,
                        second=0, microsecond=0)
    if dt.time() >= OPEN_T:          # 오늘 개장 이미 지남 → 다음 날부터
        target += timedelta(days=1)
    while not is_trading_day(target.date()):
        target += timedelta(days=1)
    return max(0.0, (target - dt).total_seconds())


def market_status_label() -> tuple[str, bool]:
    """('개장 중' / '마감', is_open) 반환."""
    op = is_market_open()
    return ("개장 중" if op else "마감", op)

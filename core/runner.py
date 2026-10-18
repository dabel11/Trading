"""자동매매 실행 루프의 공용 본문 — 데몬(autotrader.py)과 앱 인프로세스 루프가 함께 쓴다.

예전엔 루프가 두 벌이라 안전장치가 데몬에만 있었다(앱 루프는 장 마감·휴장일에도
사이클을 돌려 정규장 밖 체결이 났다). 이제 모든 게이트가 여기 한 곳에 있다:
  - 단일 실행 권한(PID) 재확인 — 다른 실행 주체가 있으면 루프 종료
  - 매매 ON/OFF(enabled) — 꺼지면 루프 종료
  - 장 시간 게이트(휴장일·조기폐장 포함, market_hours)
  - 프로세스 내 사이클 직렬화 — 루프와 '지금 1회 실행'이 겹치지 않음
  - 연속 실패 지수 백오프(최대 30분) + 임계 도달 시 1회 경보
  - schedule="daily" — 매일 daily_time(ET) 이후 1회
  - 틱 손절 가드(core.guard) 기동
"""
import os
import threading
from typing import Callable, Optional

from core import control

FAIL_ALERT_AT = 5            # 이 횟수 연속 실패 시 1회 경보
MAX_BACKOFF = 1800           # 실패 백오프 상한(초)
CLOSED_POLL_MAX = 600        # 장 마감 중 재확인 주기 상한(초)

_CYCLE_LOCK = threading.Lock()   # 프로세스 내 사이클 직렬화


def alert(msg: str):
    """텔레그램 경보 (설정 없으면 무시)."""
    try:
        import notifier
        tg = notifier.load_config().get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token"):
            notifier._telegram_send(tg["bot_token"].strip(),
                                    str(tg.get("chat_id", "")).strip(), msg)
    except Exception:
        pass


def _daily_due(cfg: dict, last_daily: str) -> bool:
    """daily 모드: 오늘(ET 거래일) 아직 안 돌렸고 지정 시각(ET) 지났으면 True.

    날짜도 ET 기준이어야 한다 — 로컬(KST) 날짜는 자정이 미국 장중(ET 10~11시)이라
    같은 ET 하루에 '날짜가 바뀌었다'고 오판해 하루 2회 실행됐다."""
    try:
        import market_hours as mh
        et = mh.now_et()
        hh, mm = (cfg.get("daily_time") or "10:00").split(":")
        target = et.replace(hour=int(hh), minute=int(mm),
                            second=0, microsecond=0)
        return et >= target and last_daily != mh.trading_day(et).isoformat()
    except Exception:
        return False


def _state_file():
    # 호출 시점에 계산 — 격리 모드(POC_ISOLATE)가 control 경로를 바꿔도 따라간다
    return control.CONFIG_FILE.parent / "runner_state.json"


def _load_last_daily() -> str:
    from safe_store import safe_read_json
    return str(safe_read_json(_state_file(), default={}).get("last_daily", ""))


def run_cycle_guarded(cfg: dict, log: Callable[[str], None]) -> Optional[dict]:
    """사이클 1회 ('지금 1회 실행' 용) — 장 시간·외부 실행 주체·중복 실행 게이트 통과 시만.

    실행 안 했으면 None (사유는 log 로 남김)."""
    import market_hours as mh
    if not mh.is_market_open():
        log(f"장 마감 — 사이클 실행 안 함 (개장까지 {mh.seconds_until_open()/3600:.1f}h)")
        return None
    other = control.daemon_pid()
    if other is not None and other != os.getpid():
        log(f"다른 프로세스(PID {other})가 자동매매 실행 중 — 건너뜀(이중 거래 방지)")
        return None
    return _run_locked(cfg, log)


def _run_locked(cfg: dict, log: Callable[[str], None]) -> Optional[dict]:
    if not _CYCLE_LOCK.acquire(blocking=False):
        log("이미 사이클 실행 중 — 건너뜀")
        return None
    try:
        from core.cycle import run_cycle
        return run_cycle(cfg, log=log)
    finally:
        _CYCLE_LOCK.release()


class Runner:
    """루프 1회분(step)을 제공한다. 호출부는 반환된 초만큼 쉬고 다시 step 을 부른다.

    log         : 진행 메시지 콜백
    min_interval: 사이클 최소 간격(초)
    alert_fn    : 연속 실패 경보 발송 함수
    """

    def __init__(self, log: Callable[[str], None], min_interval: int = 5,
                 alert_fn: Callable[[str], object] = alert):
        self.log = log
        self.min_interval = min_interval
        self.alert_fn = alert_fn
        self.fail_streak = 0
        self._alerted = False
        self._last_daily = _load_last_daily()   # 재시작해도 같은 날 재실행 금지
        self.ran = False                 # 직전 step 에서 사이클이 실행됐는지
        self.last_result: Optional[dict] = None
        self.last_error: Optional[str] = None

    def start_guard(self):
        """틱 손절 가드 기동 (중복 호출 안전)."""
        try:
            from core import guard
            guard.start(log=self.log)
        except Exception as e:
            self.log(f"틱 가드 비활성: {e}")

    def step(self) -> Optional[float]:
        """루프 1회. 다음 대기 초를 반환, None 이면 루프를 끝내야 한다."""
        import market_hours as mh
        self.ran, self.last_result, self.last_error = False, None, None

        # PID 자가복구 + 중복 감지 — 다른 실행 주체가 등록돼 있으면 내가 중복
        if not control.register_pid():
            self.log(f"다른 실행 주체(PID {control.daemon_pid()}) 활성 — "
                     "이 루프 종료(이중 거래 방지)")
            return None
        cfg = control.load_config()
        interval = max(self.min_interval, int(cfg.get("interval", 300) or 300))
        if not cfg.get("enabled"):
            self.log("설정 enabled=false — 자동매매 중지")
            return None
        if not mh.is_market_open():
            wait = mh.seconds_until_open()
            self.log(f"장 마감 — 개장까지 {wait/3600:.1f}h 대기")
            return max(self.min_interval, min(interval * 4, CLOSED_POLL_MAX, wait + 1))

        daily = cfg.get("schedule") == "daily"
        if daily and not _daily_due(cfg, self._last_daily):
            return 60
        try:
            self.last_result = _run_locked(cfg, self.log)
            self.ran = self.last_result is not None
        except Exception as e:
            self.fail_streak += 1
            self.last_error = str(e)
            self.log(f"사이클 오류({self.fail_streak}연속): {e}")
            # 같은 오류가 매 사이클 반복되며 알림만 쌓이는 것 방지:
            # 지수 백오프 + 임계 도달 시 1회만 경보.
            if self.fail_streak >= FAIL_ALERT_AT and not self._alerted:
                self.alert_fn(f"⚠️ 자동매매 사이클 {self.fail_streak}연속 실패: {e}")
                self._alerted = True
            return min(interval * (2 ** min(self.fail_streak, 6)), MAX_BACKOFF)
        self.fail_streak, self._alerted = 0, False
        if daily:
            if self.ran:
                self._mark_daily_done()
                self.log("일일 1회 실행 완료 — 내일 같은 시각까지 대기")
            return 60
        return interval

    def _mark_daily_done(self):
        import market_hours as mh
        from safe_store import atomic_write_json
        self._last_daily = mh.trading_day().isoformat()
        try:
            atomic_write_json(_state_file(), {"last_daily": self._last_daily})
        except Exception as e:
            self.log(f"daily 실행 기록 저장 실패: {e}")

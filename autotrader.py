"""헤드리스 24시간 자동매매 데몬 — 유일한 자동매매 실행 주체.

앱(관리 주체)이 사이드바 '자동 매매' 토글과 자동 트레이딩 페이지로 이 데몬을
켜고 끈다. 데몬은 매 사이클 autotrader_config.json 을 다시 읽으므로 앱에서
바꾼 설정은 다음 사이클부터 반영된다. 실제 결정·실행은 core.cycle.run_cycle
(공용 사이클) 한 곳에서 일어난다.

[실행]
  python autotrader.py            # 설정대로 실행 (없으면 모의·복합 기본값 생성)
  종료: Ctrl+C 또는 앱의 '중지' 버튼

[스케줄 모드] (구 scheduler.py 흡수)
  schedule="interval" : interval 초마다 사이클 (기본)
  schedule="daily"    : 매일 daily_time(ET) 이후 첫 기회에 1회만
"""
import atexit
import time
from datetime import datetime, date
from pathlib import Path

from core import control

DIR = Path(__file__).resolve().parent
LOG_FILE = DIR / "autotrader.log"


def _log(msg: str):
    line = f"[{datetime.now().strftime('%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if LOG_FILE.stat().st_size > 800_000:
            tail = LOG_FILE.read_text(encoding="utf-8").splitlines()[-2000:]
            LOG_FILE.write_text("\n".join(tail) + "\n", encoding="utf-8")
    except Exception:
        pass


def _notify_down(reason: str):
    """데드맨 스위치 — 데몬이 멈출 때 텔레그램으로 즉시 알린다."""
    try:
        import notifier
        tg = notifier.load_config().get("telegram", {})
        if tg.get("enabled") and tg.get("bot_token"):
            notifier._telegram_send(
                tg["bot_token"].strip(), str(tg.get("chat_id", "")).strip(),
                f"⚠️ 자동매매 데몬 중지됨 — {reason}\n"
                f"재시작: 앱 → 자동 트레이딩 → '백그라운드 시작'")
    except Exception:
        pass


def _daily_due(cfg: dict, last_daily: str) -> bool:
    """daily 모드: 오늘 아직 안 돌렸고 지정 시각(ET) 지났으면 True."""
    try:
        import pytz
        et = datetime.now(pytz.timezone("America/New_York"))
        hh, mm = (cfg.get("daily_time") or "10:00").split(":")
        target = et.replace(hour=int(hh), minute=int(mm),
                            second=0, microsecond=0)
        return et >= target and last_daily != date.today().isoformat()
    except Exception:
        return False


def main():
    import market_hours as mh
    import signal

    if not control.register_pid():
        _log(f"이미 실행 중인 데몬(PID {control.daemon_pid()}) 감지 — 중복 기동 취소")
        return
    atexit.register(control.cleanup_pid)

    def _on_term(_sig, _frm):
        _log("종료 신호(SIGTERM) — 데몬 중지")
        _notify_down("종료 신호 수신")
        raise SystemExit(0)
    try:
        signal.signal(signal.SIGTERM, _on_term)
    except Exception:
        pass

    _log("=" * 50)
    _log("헤드리스 자동매매 데몬 시작 — 화면 꺼져도 미국장 시간 내내 동작")
    _log("종료: Ctrl+C")

    # 틱 구동 리스크 가드 — ws 체결가 수신 즉시 손절/트레일링 (주기와 무관)
    try:
        from core import guard
        guard.start(log=_log)
    except Exception as e:
        _log(f"틱 가드 비활성: {e}")
    last_daily = ""          # daily 모드: 마지막 실행 날짜
    fail_streak = 0          # 연속 사이클 실패 — 백오프/경보용
    _FAIL_ALERT_AT = 5       # 이 횟수 연속 실패 시 1회 경보
    alerted_fail = False
    try:
        while True:
            # PID 자가복구 — 외부 삭제돼도 앱 감지/중지가 계속 동작.
            # 살아있는 다른 데몬이 등록돼 있으면 내가 중복 → 즉시 종료.
            if not control.register_pid():
                _log(f"다른 데몬(PID {control.daemon_pid()}) 활성 — "
                     "이 인스턴스 종료(이중 거래 방지)")
                return
            cfg = control.load_config()
            # 최소 5초 — 실시간 피드 폴링(5초)과 같은 한계. 더 짧으면 같은
            # 가격을 재평가할 뿐이다 (채점 번들은 core.scoring 이 3분 캐시)
            interval = max(5, int(cfg.get("interval", 300)))
            # 자동매매 OFF면 백그라운드에 남지 않고 데몬을 완전히 종료한다.
            # (SIGTERM 전달 실패 같은 예외 상황까지 커버 — "끄면 안 돌아간다"를 보장)
            # 재개는 앱이 enabled=true 기록 후 데몬을 다시 띄운다(start_daemon).
            if not cfg.get("enabled"):
                _log("설정 enabled=false — 자동매매 중지, 데몬 종료")
                return
            try:
                if not mh.is_market_open():
                    _log(f"장 마감 — 개장까지 {mh.seconds_until_open()/3600:.1f}h 대기")
                    time.sleep(min(interval * 4, 600))
                    continue
                if cfg.get("schedule") == "daily":
                    if _daily_due(cfg, last_daily):
                        from core.cycle import run_cycle
                        run_cycle(cfg, log=_log)
                        last_daily = date.today().isoformat()
                        _log("일일 1회 실행 완료 — 내일 같은 시각까지 대기")
                    time.sleep(60)
                    continue
                from core.cycle import run_cycle
                run_cycle(cfg, log=_log)
                fail_streak = 0          # 성공 → 백오프/경보 해제
                alerted_fail = False
            except KeyboardInterrupt:
                _log("종료 요청 — 데몬 중지")
                _notify_down("수동 종료(Ctrl+C)")
                break
            except Exception as e:
                fail_streak += 1
                _log(f"사이클 오류({fail_streak}연속): {e}")
                # 같은 오류가 매 사이클 반복되며 알림만 쌓이는 것 방지:
                # 지수 백오프(최대 30분) + 임계 도달 시 1회만 경보.
                if fail_streak >= _FAIL_ALERT_AT and not alerted_fail:
                    _notify_down(f"사이클 {fail_streak}연속 실패: {e}")
                    alerted_fail = True
                backoff = min(interval * (2 ** min(fail_streak, 6)), 1800)
                time.sleep(backoff)
                continue
            time.sleep(interval)
    except SystemExit:
        raise
    except BaseException as e:           # 예기치 못한 치명 오류도 알리고 종료
        _log(f"치명 오류로 중지: {e}")
        _notify_down(f"치명 오류: {e}")
        raise


if __name__ == "__main__":
    main()

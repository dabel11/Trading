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
from datetime import datetime
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
    from core.runner import alert
    alert(f"⚠️ 자동매매 데몬 중지됨 — {reason}\n"
          f"재시작: 앱 → 자동 트레이딩 → '백그라운드 시작'")


def main():
    import signal
    from core.runner import Runner

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

    # 루프 본문(PID·enabled·장 시간·daily·실패 백오프/경보)은 core.runner 공용 —
    # 앱 인프로세스 루프와 같은 안전장치를 쓴다.
    runner = Runner(log=_log, min_interval=5)
    runner.start_guard()          # 틱 손절 가드 — 주기와 무관하게 체결가 즉시 평가
    try:
        while True:
            delay = runner.step()
            if delay is None:
                return
            time.sleep(delay)
    except KeyboardInterrupt:
        _log("종료 요청 — 데몬 중지")
        _notify_down("수동 종료(Ctrl+C)")
    except SystemExit:
        raise
    except BaseException as e:           # 예기치 못한 치명 오류도 알리고 종료
        _log(f"치명 오류로 중지: {e}")
        _notify_down(f"치명 오류: {e}")
        raise


if __name__ == "__main__":
    main()

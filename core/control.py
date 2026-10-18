"""앱 ↔ 데몬 제어 계약 — 설정 파일 · PID · 시작/중지의 단일 창구.

앱(관리 주체)은 이 모듈로만 데몬을 다룬다. 데몬도 같은 모듈로 자기 PID를
등록한다. 제어 채널: autotrader_config.json(매 사이클 재독) + autotrader.pid.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = DIR / "autotrader_config.json"
PID_FILE = DIR / "autotrader.pid"

DEFAULT_CONFIG = {
    "enabled": True, "paper": True, "strategy": "ai_influencer",
    "strategy_name": "AI 인플루언서", "horizon": "단기", "interval": 300,
    "dynamic": True, "stop_loss": 0.07, "take_profit": 0.15, "trail": 0.07,
    "min_score": 60, "sell_score": 35, "hold_strong": 60, "hold_medium": 30,
    "daily_loss_limit": 0.05, "discover": {"enabled": False},
    # schedule: "interval"=주기 실행 | "daily"=매일 1회(개장 후 daily_time ET)
    "schedule": "interval", "daily_time": "10:00",
}


# ── 설정 ──────────────────────────────────────────────────────────────────

_LAST_GOOD: dict = {}


def load_config() -> dict:
    """설정 읽기. 파일이 '없을 때만' 안전 기본값(모의)을 생성한다.

    읽기 실패(다른 스레드가 쓰는 중 잘린 파일 등)는 짧게 재시도하고, 그래도 실패하면
    마지막 정상값(없으면 기본값)을 반환하되 파일은 절대 덮어쓰지 않는다 — 예전엔
    파싱 실패 시 파일 전체를 기본값으로 덮어써 사용자 설정(주기·전략·모드)이 날아갔다.
    """
    global _LAST_GOOD
    if not CONFIG_FILE.exists():
        cfg = dict(DEFAULT_CONFIG)
        try:
            save_config(cfg)
        except Exception:
            pass
        return cfg
    for _ in range(6):
        try:
            cfg = json.loads(CONFIG_FILE.read_text())
            if isinstance(cfg, dict) and cfg:
                _LAST_GOOD = dict(cfg)
                return cfg
        except Exception:
            pass
        time.sleep(0.03)
    if _LAST_GOOD:
        return dict(_LAST_GOOD)
    # 정상값을 한 번도 못 읽은 채 파일이 손상돼 있다 — 사용자의 실제 의도(ON/OFF·
    # 모의/실거래)를 알 수 없으므로 매매는 끈 상태로 본다(fail-closed).
    print(f"[control] 설정 파일 손상 — 매매 비활성으로 간주: {CONFIG_FILE}", flush=True)
    return {**DEFAULT_CONFIG, "enabled": False}


def save_config(cfg: dict):
    """원자적 쓰기(임시파일→rename) — 동시 읽기가 잘린 파일을 보지 않는다."""
    global _LAST_GOOD
    from safe_store import atomic_write_json
    atomic_write_json(CONFIG_FILE, cfg)
    _LAST_GOOD = dict(cfg)


def set_enabled(on: bool):
    """매매 ON/OFF (마스터 스위치) — 데몬은 다음 사이클부터 반영."""
    cfg = load_config()
    cfg["enabled"] = bool(on)
    save_config(cfg)


# ── PID ───────────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):
        try:
            o = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"],
                               capture_output=True, text=True)
            return str(pid) in (o.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def daemon_pid():
    """살아있는 데몬 PID (없으면 None)."""
    try:
        pid = int(PID_FILE.read_text().strip())
    except Exception:
        return None
    return pid if _pid_alive(pid) else None


def daemon_alive() -> bool:
    return daemon_pid() is not None


def register_pid() -> bool:
    """데몬이 자기 PID 등록. 살아있는 다른 데몬이 있으면 False(중복 기동)."""
    other = daemon_pid()
    if other is not None and other != os.getpid():
        return False
    try:
        PID_FILE.write_text(str(os.getpid()))
    except Exception:
        pass
    return True


def cleanup_pid():
    """내 PID일 때만 파일 삭제 — 타 인스턴스의 PID 파일 보호."""
    try:
        if int(PID_FILE.read_text().strip()) == os.getpid():
            PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── 시작/중지 (앱에서 호출) ───────────────────────────────────────────────

def start_daemon() -> bool:
    """백그라운드 데몬 기동. 이미 살아있으면 False."""
    if daemon_alive():
        return False
    kw = {"creationflags": 0x00000008} if sys.platform.startswith("win") else {}
    subprocess.Popen([sys.executable, str(DIR / "autotrader.py")],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     cwd=str(DIR), **kw)
    return True


def stop_daemon():
    """매매 비활성(enabled=false) 기록 후 데몬 프로세스 종료."""
    set_enabled(False)
    pid = daemon_pid()
    if pid:
        try:
            if sys.platform.startswith("win"):
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
            else:
                import signal
                os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass

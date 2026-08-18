"""앱 ↔ 데몬 제어 계약 — 설정 파일 · PID · 시작/중지의 단일 창구.

앱(관리 주체)은 이 모듈로만 데몬을 다룬다. 데몬도 같은 모듈로 자기 PID를
등록한다. 제어 채널: autotrader_config.json(매 사이클 재독) + autotrader.pid.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

DIR = Path(__file__).resolve().parent.parent
CONFIG_FILE = DIR / "autotrader_config.json"
PID_FILE = DIR / "autotrader.pid"

DEFAULT_CONFIG = {
    "enabled": True, "paper": True, "strategy": "composite",
    "strategy_name": "복합 (기본)", "horizon": "단기", "interval": 300,
    "dynamic": True, "stop_loss": 0.07, "take_profit": 0.15, "trail": 0.07,
    "min_score": 60, "sell_score": 35, "hold_strong": 60, "hold_medium": 30,
    "daily_loss_limit": 0.05, "discover": {"enabled": False},
    # schedule: "interval"=주기 실행 | "daily"=매일 1회(개장 후 daily_time ET)
    "schedule": "interval", "daily_time": "10:00",
}


# ── 설정 ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    """설정 읽기. 없으면 안전 기본값(모의·복합) 생성 — 단독 실행 가능."""
    try:
        return json.loads(CONFIG_FILE.read_text())
    except Exception:
        try:
            CONFIG_FILE.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False))
        except Exception:
            pass
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False))


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

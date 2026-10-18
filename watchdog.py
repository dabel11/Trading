"""외부 감시 — 앱이 통째로 죽으면 앱 안의 감시(monitor 스레드)도 같이 죽는다.

launchd 가 5분마다 이 스크립트를 실행해, 장중인데 자동매매 프로세스가 꺼져 있거나 사이클이
멈췄으면 텔레그램으로 알린다. 상태 파일을 앱과 공유하므로 같은 사건을 두 번 알리지 않는다.
맥이 잠들어 있는 동안엔 launchd 도 돌지 않는다 — 깨어난 뒤 첫 실행에서 공백을 알린다.

    python3 watchdog.py              # 1회 점검 (launchd 가 부르는 형태)
    python3 watchdog.py --install    # launchd 에이전트 설치(로그인 시 자동, 5분 주기)
    python3 watchdog.py --uninstall  # 제거
    python3 watchdog.py --status     # 설치 여부·최근 점검 결과
"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

LABEL = "com.aitrading.watchdog"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LOG = Path.home() / "Library" / "Logs" / "AITrading-watchdog.log"
VENV_PY = Path.home() / "Library" / "Application Support" / "AITrading" / "venv" / "bin" / "python"


def check_once() -> str:
    from core import control
    import monitor
    cfg = control.load_config()
    alive = control.daemon_alive()
    msg = monitor.check_and_alert(cfg, process_alive=alive, source="외부 감시")
    st = monitor.load_state().get("incident")
    return f"alive={alive} incident={(st or {}).get('kind')} sent={'yes' if msg else 'no'}"


def _plist() -> dict:
    py = str(VENV_PY if VENV_PY.exists() else Path(sys.executable))
    return {
        "Label": LABEL,
        "ProgramArguments": [py, str(ROOT / "watchdog.py")],
        "WorkingDirectory": str(ROOT),
        "StartInterval": 300,
        "RunAtLoad": True,
        "StandardOutPath": str(LOG),
        "StandardErrorPath": str(LOG),
    }


def install():
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    PLIST.write_bytes(plistlib.dumps(_plist()))
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(f"launchctl bootstrap 실패: {r.stderr.strip()}")
    print(f"설치됨: {PLIST}\n5분마다 점검 · 로그 {LOG}")


def uninstall():
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
    PLIST.unlink(missing_ok=True)
    print("제거됨")


def status():
    r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                       capture_output=True, text=True)
    print("launchd:", "설치·로드됨" if r.returncode == 0 else "미설치")
    if LOG.exists():
        tail = LOG.read_text(errors="replace").strip().splitlines()[-3:]
        print("최근 점검:", *tail, sep="\n  ")


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg == "--install":
        install()
    elif arg == "--uninstall":
        uninstall()
    elif arg == "--status":
        status()
    else:
        import time
        try:
            print(time.strftime("%m-%d %H:%M:%S"), check_once(), flush=True)
        except Exception as e:
            print(time.strftime("%m-%d %H:%M:%S"), f"점검 실패: {e}", flush=True)
            sys.exit(1)

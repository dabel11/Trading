"""별도 파이썬 프로세스에서 함수 실행 — 라이브 매매 프로세스의 전역 상태 격리.

백테스트는 실행 내내 portfolio 모듈 전역(_CLOCK·CAPITAL_TOTAL·MAX_POSITIONS 등)을
바꾼다. 같은 프로세스에서 자동매매 사이클이 돌면 그 값을 그대로 읽어 장부가
오염된다(진입일이 과거 날짜로 찍혀 다음 사이클에 max_hold 강제 매도, 실거래
사이징이 백테스트 자본 기준이 되는 등). 그래서 앱은 백테스트를 여기서 새
프로세스로 띄운다 — 자식은 자기 전역만 바꾸고 반환값만 pickle 로 돌려준다.

사용: isolated.call("backtester:run", strategy="momentum", start=..., ...)
"""
import importlib
import pickle
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def call(target: str, *args, timeout: float | None = 1800, **kwargs):
    """target='모듈:함수' 를 새 프로세스에서 실행하고 반환값을 돌려준다.

    자식에서 난 예외는 RuntimeError 로 다시 던진다. 인자·반환값은 pickle 가능해야 한다."""
    with tempfile.TemporaryDirectory(prefix="ait-iso-") as td:
        inp, outp = Path(td) / "in.pkl", Path(td) / "out.pkl"
        inp.write_bytes(pickle.dumps((target, args, kwargs)))
        proc = subprocess.run(
            [sys.executable, "-m", "core.isolated", str(inp), str(outp)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            timeout=timeout)
        if not outp.exists():
            tail = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-3:]
            raise RuntimeError(f"격리 실행 실패(exit {proc.returncode}): {' / '.join(tail)}")
        ok, val = pickle.loads(outp.read_bytes())
        if not ok:
            raise RuntimeError(val)
        return val


def _main(inp: str, outp: str):
    target, args, kwargs = pickle.loads(Path(inp).read_bytes())
    mod, fn = target.split(":", 1)
    try:
        res = (True, getattr(importlib.import_module(mod), fn)(*args, **kwargs))
    except Exception as e:
        res = (False, f"{type(e).__name__}: {e}")
    Path(outp).write_bytes(pickle.dumps(res))


if __name__ == "__main__":
    _main(*sys.argv[1:3])

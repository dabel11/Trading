"""
안전한 JSON 파일 저장/로드.

목적:
  - 원자적 쓰기: temp 파일에 쓰고 os.replace()로 교체
    → 쓰는 도중 크래시해도 기존 파일이 깨지지 않음
  - 파일 락(fcntl): 여러 프로세스(앱 + 스케줄러)가
    동시에 쓰는 경쟁 상태 방지
  - 손상 백업: 읽기 실패 시 .corrupt 백업 후 기본값 반환
"""

import os
import json
import tempfile
import threading
from pathlib import Path

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:          # Windows
    _HAS_FCNTL = False


# ── 거래 장부 전용 프로세스 간 락 ────────────────────────────────────────────
# atomic_write_json 의 fcntl 락은 개별 쓰기 1회만 보호한다. 하지만 매매는
# "현금/포지션을 읽고 → 계산하고 → 쓰는" read-modify-write 시퀀스라, 그 사이
# 다른 프로세스(데몬 ↔ 앱)가 끼어들면 갱신이 통째로 유실된다(lost update).
# trade_lock() 은 이 시퀀스 전체를 프로세스 간에 직렬화한다.
#
#   - 프로세스 내부: threading.RLock 으로 모든 스레드를 직렬화(+재진입 허용)
#   - 프로세스 사이: 깊이 0→1 로 진입할 때만 fcntl flock 을 잡고, 1→0 에서 푼다
# 둘을 합쳐 "재진입 가능한 프로세스 간 락"을 만든다. execute_orders 가
# trade_lock 안에서 record_buy → adjust 를 중첩 호출해도 데드락이 없다.
_TRADE_LOCK_FILE = Path(__file__).resolve().parent / ".trade.lock"
_proc_lock = threading.RLock()


class _TradeLock:
    """재진입 가능한 프로세스 간 거래 락 (단일 인스턴스로 공유)."""

    def __init__(self):
        self._depth = 0          # 같은 스레드의 중첩 깊이 (RLock 보유 중에만 변경)
        self._fd = None          # flock 을 잡고 있는 파일 디스크립터

    def __enter__(self):
        _proc_lock.acquire()     # 스레드 직렬화 + 동일 스레드 재진입
        self._depth += 1
        if self._depth == 1 and _HAS_FCNTL:
            try:
                self._fd = os.open(str(_TRADE_LOCK_FILE),
                                   os.O_CREAT | os.O_RDWR, 0o644)
                fcntl.flock(self._fd, fcntl.LOCK_EX)
            except OSError:
                if self._fd is not None:
                    try:
                        os.close(self._fd)
                    except OSError:
                        pass
                    self._fd = None
        return self

    def __exit__(self, *exc):
        self._depth -= 1
        if self._depth == 0 and self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        _proc_lock.release()
        return False


_trade_lock = _TradeLock()


def trade_lock():
    """매매 read-modify-write 를 프로세스 간에 직렬화하는 컨텍스트 매니저.

    사용: `with trade_lock(): <장부 읽기→수정→쓰기>`
    재진입 가능하므로 중첩 호출(execute_orders → record_buy → adjust)에 안전하다.
    """
    return _trade_lock


def atomic_write_json(path, data, indent: int = 2):
    """원자적 + 락 보호 JSON 쓰기."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 같은 디렉터리에 temp 파일 (os.replace는 동일 파일시스템에서만 원자적)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if _HAS_FCNTL:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                except OSError:
                    pass
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())     # 디스크에 강제 기록
        os.replace(tmp, path)        # 원자적 교체
    except Exception:
        # 실패 시 temp 정리
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def safe_read_json(path, default=None):
    """JSON 읽기. 손상 시 .corrupt 로 백업하고 default 반환."""
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            if _HAS_FCNTL:
                try:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                except OSError:
                    pass
            return json.load(f)
    except (json.JSONDecodeError, ValueError):
        # 손상된 파일 백업 후 기본값
        try:
            backup = path.with_suffix(path.suffix + ".corrupt")
            path.replace(backup)
        except OSError:
            pass
        return default if default is not None else {}
    except Exception:
        return default if default is not None else {}

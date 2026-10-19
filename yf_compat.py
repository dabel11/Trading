"""yfinance 운영 보정 — 앱·데몬 시작 시 install() 1회.

1) 시간대 캐시 누수 차단: yfinance(1.4.x)의 tz 캐시는 peewee SqliteDatabase 를 스레드별
   연결로 열고 닫지 않는다. yf.download(threads=True) 를 60초마다 부르는 앱에선 연결이
   계속 쌓여(tkr-tz.db + -wal 약 200개) macOS 기본 파일 한도 256 에 닿고, 그 뒤로는
   설정·장부·로그 파일을 못 열어 매매 사이클이 전부 실패했다(2026-10-19 장중 발견).
   같은 역할(티커→시간대)을 하는 메모리 사전으로 바꿔 파일을 아예 열지 않게 한다.
2) 파일 핸들 소프트 한도 상향(256 → 8192): 다른 누수가 생겨도 즉시 마비되지 않게.
"""
from __future__ import annotations

import threading

_DONE = {"on": False}


class _MemTzCache:
    """yfinance.cache._TzCacheDummy 와 같은 인터페이스(lookup/store/tz_db) — 프로세스 메모리."""

    def __init__(self):
        self._d: dict = {}
        self._lock = threading.Lock()

    def lookup(self, tkr):
        with self._lock:
            return self._d.get(tkr)

    def store(self, tkr, tz):
        with self._lock:
            if tz is None:
                self._d.pop(tkr, None)
            else:
                self._d[tkr] = tz

    @property
    def tz_db(self):
        return None


def raise_fd_limit(target: int = 8192) -> tuple:
    """RLIMIT_NOFILE 소프트 한도를 target 까지(하드 한도 이내). 반환: (이전, 이후)."""
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        cap = target if hard == resource.RLIM_INFINITY else min(target, hard)
        if soft != resource.RLIM_INFINITY and soft < cap:
            resource.setrlimit(resource.RLIMIT_NOFILE, (cap, hard))
        return soft, resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except Exception:
        return None, None


def patch_tz_cache() -> bool:
    try:
        import yfinance.cache as yc
        if isinstance(getattr(yc._TzCacheManager, "_tz_cache", None), _MemTzCache):
            return True
        yc._TzCacheManager._tz_cache = _MemTzCache()
        try:
            yc._TzDBManager.close_db()          # 이미 열린 기본 연결이 있으면 닫는다
        except Exception:
            pass
        return True
    except Exception:
        return False


def install() -> bool:
    if _DONE["on"]:
        return True
    _DONE["on"] = True
    raise_fd_limit()
    return patch_tz_cache()

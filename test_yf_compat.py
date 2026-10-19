"""
yf_compat — yfinance tz 캐시 파일 누수 차단·파일 한도 상향 (2026-10-19 장중 EMFILE 마비 재발 방지).

실행: python3 -m pytest test_yf_compat.py -q
"""
import yf_compat


def test_mem_tz_cache_roundtrip():
    c = yf_compat._MemTzCache()
    assert c.lookup("AAPL") is None and c.tz_db is None
    c.store("AAPL", "America/New_York")
    assert c.lookup("AAPL") == "America/New_York"
    c.store("AAPL", None)
    assert c.lookup("AAPL") is None


def test_patch_replaces_sqlite_cache_and_is_idempotent():
    import yfinance.cache as yc
    assert yf_compat.patch_tz_cache()
    assert isinstance(yc._TzCacheManager._tz_cache, yf_compat._MemTzCache)
    first = yc._TzCacheManager._tz_cache
    assert yf_compat.patch_tz_cache() and yc._TzCacheManager._tz_cache is first
    assert isinstance(yc.get_tz_cache(), yf_compat._MemTzCache)


def test_raise_fd_limit_never_lowers():
    import resource
    before = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    yf_compat.raise_fd_limit(64)                       # 낮은 목표는 무시
    assert resource.getrlimit(resource.RLIMIT_NOFILE)[0] == before

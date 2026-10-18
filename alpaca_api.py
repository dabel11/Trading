"""Alpaca 연결 — 읽기 전용 점검(계좌·시세·스트림 인증) + 일괄 시세(snapshots).

주문은 하지 않는다(주문은 broker.py). 키는 config 모듈에서 호출 시점에 읽어
설정 화면에서 저장하면 재시작 없이 바로 반영된다.
"""
from __future__ import annotations

import json
import time

import requests

DATA_URL = "https://data.alpaca.markets"
STREAM_URL = "wss://stream.data.alpaca.markets/v2/iex"     # 무료: IEX · 30종목


def _creds():
    import config as c
    return c.ALPACA_API_KEY, c.ALPACA_SECRET_KEY, c.ALPACA_BASE_URL


def _hdr():
    k, s, _ = _creds()
    return {"APCA-API-KEY-ID": k, "APCA-API-SECRET-KEY": s}


def ready() -> bool:
    import config as c
    return c.alpaca_ready()


def _mask(v: str) -> str:
    v = str(v or "")
    return ("•" * max(0, len(v) - 4)) + v[-4:] if v else ""


def account() -> dict:
    """GET /v2/account (읽기 전용)."""
    if not ready():
        return {"ok": False, "error": "키가 설정되지 않았습니다"}
    _, _, base = _creds()
    try:
        r = requests.get(f"{base}/v2/account", headers=_hdr(), timeout=8)
        if r.status_code in (401, 403):
            return {"ok": False, "error": f"인증 실패({r.status_code}) — 키·시크릿 또는 계좌 종류(페이퍼/실계좌) 확인"}
        r.raise_for_status()
        a = r.json()
        return {"ok": True, "paper": "paper" in base, "status": a.get("status"),
                "account": _mask(a.get("account_number")),
                "equity": float(a.get("equity") or 0), "cash": float(a.get("cash") or 0),
                "buying_power": float(a.get("buying_power") or 0),
                "currency": a.get("currency", "USD")}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def snapshots(symbols: list) -> dict:
    """여러 종목 최신 시세 {TK: {price, prev, change, change_pct}} — 1회 호출(IEX)."""
    if not ready() or not symbols:
        return {}
    out = {}
    syms = [s.upper() for s in symbols if s and not s.startswith("^") and "=" not in s
            and "-USD" not in s and ":" not in s]
    for i in range(0, len(syms), 100):
        chunk = syms[i:i + 100]
        try:
            r = requests.get(f"{DATA_URL}/v2/stocks/snapshots",
                             params={"symbols": ",".join(chunk), "feed": "iex"},
                             headers=_hdr(), timeout=8)
            if r.status_code != 200:
                continue
            for tk, sn in (r.json() or {}).items():
                lt = (sn or {}).get("latestTrade") or {}
                db = (sn or {}).get("dailyBar") or {}
                pdb = (sn or {}).get("prevDailyBar") or {}
                p = float(lt.get("p") or db.get("c") or 0)
                prev = float(pdb.get("c") or 0)
                if p > 0:
                    out[tk] = {"price": p, "prev": prev,
                               "open": float(db.get("o") or p), "high": float(db.get("h") or p),
                               "low": float(db.get("l") or p),
                               "change": (p - prev) if prev else 0.0,
                               "change_pct": ((p - prev) / prev) if prev else 0.0}
        except Exception:
            continue
    return out


def stream_auth_test(timeout: float = 6.0) -> dict:
    """IEX 스트림 접속·인증만 확인하고 바로 끊는다(무료는 동시 1연결)."""
    if not ready():
        return {"ok": False, "error": "키가 설정되지 않았습니다"}
    try:
        import websocket
        k, s, _ = _creds()
        ws = websocket.create_connection(STREAM_URL, timeout=timeout)
        try:
            ws.recv()                                    # [{"T":"success","msg":"connected"}]
            ws.send(json.dumps({"action": "auth", "key": k, "secret": s}))
            msg = json.loads(ws.recv())
        finally:
            ws.close()
        m = msg[0] if isinstance(msg, list) and msg else {}
        if m.get("T") == "success" and m.get("msg") == "authenticated":
            return {"ok": True}
        return {"ok": False, "error": f"{m.get('code', '')} {m.get('msg', msg)}".strip()}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}


def check() -> dict:
    """설정 화면 '연결 테스트' — 계좌 · 시세 · 스트림."""
    res = {"account": account()}
    t = time.time()
    snap = snapshots(["AAPL", "MSFT"])
    res["data"] = ({"ok": True, "sample": {k: round(v["price"], 2) for k, v in snap.items()},
                    "ms": int((time.time() - t) * 1000)} if snap else
                   {"ok": False, "error": "시세 조회 실패(키 권한 또는 네트워크)"})
    try:
        import realtime_feed as rtf
        if rtf.alpaca_stream_state().get("authenticated"):
            res["stream"] = {"ok": True, "note": "실시간 스트림 연결 중"}
        else:
            res["stream"] = stream_auth_test()
    except Exception:
        res["stream"] = stream_auth_test()
    return res

"""
조건부 가격 알림 관리.
alerts.json 에 목표 조건을 저장, 앱 루프(15초)마다 체크.

각 알림 형식:
  {
    "id": "AAPL_above_320",
    "ticker": "AAPL",
    "condition": "above" | "below",
    "target": 320.0,
    "note": "목표가 도달",
    "triggered": false,
    "created": "2026-06-04T00:00:00"
  }
"""

from datetime import datetime
from pathlib import Path
from safe_store import atomic_write_json, safe_read_json

ALERTS_FILE = Path(__file__).parent / "price_alerts.json"


def load() -> list[dict]:
    return safe_read_json(ALERTS_FILE, default={"alerts": []}).get("alerts", [])


def save(alerts: list[dict]):
    atomic_write_json(ALERTS_FILE, {"alerts": alerts})


def add(ticker: str, condition: str, target: float, note: str = "") -> dict:
    """알림 추가. condition: 'above' | 'below'"""
    alerts = load()
    aid = f"{ticker}_{condition}_{target}"
    # 중복 제거
    alerts = [a for a in alerts if a["id"] != aid]
    alerts.append({
        "id": aid,
        "ticker": ticker.upper(),
        "condition": condition,
        "target": float(target),
        "note": note,
        "triggered": False,
        "created": datetime.now().isoformat(timespec="seconds"),
    })
    save(alerts)
    return {"ok": True, "id": aid}


def remove(alert_id: str):
    alerts = [a for a in load() if a["id"] != alert_id]
    save(alerts)


def check(prices: dict[str, float]) -> list[dict]:
    """
    현재 가격 dict {ticker: price} 와 비교해서 발동된 알림 목록 반환.
    발동된 알림은 triggered=True 로 마크.
    """
    alerts = load()
    fired = []
    changed = False
    for a in alerts:
        if a.get("triggered"):
            continue
        price = prices.get(a["ticker"])
        if price is None:
            continue
        hit = (a["condition"] == "above" and price >= a["target"]) or \
              (a["condition"] == "below" and price <= a["target"])
        if hit:
            a["triggered"] = True
            a["triggered_at"] = datetime.now().isoformat(timespec="seconds")
            a["triggered_price"] = price
            fired.append(a)
            changed = True
    if changed:
        save(alerts)
    return fired


def reset_triggered(alert_id: str):
    """알림 재활성화 (triggered → False)."""
    alerts = load()
    for a in alerts:
        if a["id"] == alert_id:
            a["triggered"] = False
            a.pop("triggered_at", None)
            a.pop("triggered_price", None)
    save(alerts)

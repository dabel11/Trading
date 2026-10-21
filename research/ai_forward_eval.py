"""
AI 판단 forward 평가 — influence_log 의 판단을 이후 실제 가격과 맞춰 채점한다(SPY 대비 초과수익).

AI 판단(LLM·실시간 의견)은 과거 시점을 재현할 수 없어 백테스트하지 못한다(influence/engine.py
docstring). 그래서 판단을 시점 그대로 기록해 두고, 시간이 지난 뒤 이 스크립트로 채점한다.

채점 규칙(고정 — 결과를 보고 바꾸지 않는다):
  • 진입 = 판단 기록 시각(줄의 ts) 뒤 첫 정규장 종가. 장중 판단이면 그날 종가, 장 마감 뒤·
    휴장일 판단이면 다음 거래일 종가(조기폐장 13:00 반영). 판단 뒤의 가격만 쓴다 — 판단부터
    종가까지의 움직임은 놓치는 보수적 근사.
  • 청산 = 진입 뒤 h 거래일 종가, h ∈ HORIZONS. 아직 h 거래일이 지나지 않은 판단은 '대기'.
  • 초과수익 = 종목 수익률 − 같은 날짜 SPY 수익률 (수정주가 = 배당 포함 근사).
  • 중복 제거: 같은 (경로, 판단기, 종목, 진입일)은 첫 판단 하나만 센다 — 스캐너는 3시간마다
    같은 종목을 다시 판단하므로 그대로 세면 같은 결과를 여러 번 센다.
  • 유의성: 같은 날 진입한 판단끼리는 같은 시장을 겪어 독립이 아니고, h 거래일 창은 진입일끼리
    겹친다. 그래서 진입일별 평균을 한 표본으로 보고(일자 군집) 유효 표본 = 진입일 수 ÷ h 로
    t 값을 낸다. 유효 표본이 MIN_INDEPENDENT 미만이면 판정을 보류한다.

보는 것 (경로 src — scanner=전체 시장, engine=자동매매 대상 — 와 판단기 by 별로):
  ① 의견별(BUY·HOLD·SELL) 평균 초과수익·승률(초과수익 > 0 비율)
  ② BUY − 판단한 종목 전체 — 판단 대상 자체의 기울기를 빼고 본 '고르는 능력'
  ③ 점수 IC — 진입일마다 점수와 초과수익의 순위 상관(스피어만)의 평균
  ④ 점수 구간별(매도 문턱 35 · 매수 문턱 60 기준) 평균 초과수익
  ⑤ 소스별 IC — 증거(analyst·insider·news·social·crowd) stance 와 초과수익의 순위 상관

실행:
  python3 research/ai_forward_eval.py                    # 설정 폴더의 influence_log-*.jsonl
  python3 research/ai_forward_eval.py --logs ~/trade     # 폴더·파일 지정(여러 개 가능) — 워크트리에선 필수
결과: docs/ai-forward-<날짜>.md · research/results/ai-forward-<날짜>.json
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("TRADE_NOTIFY_DISABLED", "1")

import numpy as np            # noqa: E402
import pandas as pd           # noqa: E402

import market_hours as mh     # noqa: E402

HORIZONS = (1, 5, 10, 20)            # 거래일
MIN_INDEPENDENT = 20                 # 유효 표본(진입일 수 ÷ h)이 이보다 적으면 판정 보류
MIN_IC_NAMES = 10                    # 진입일 하루에 이보다 적게 판단했으면 그날 IC 는 건너뜀
T_SIGNAL = 2.0                       # |t| 이 이 이상이면 '신호' — 여러 칸을 보므로 우연도 나온다
SETTLE_SEC = 20 * 60                 # 종가 확정 여유 — 장 마감 뒤 이만큼 지난 세션만 '완료'
PRICE_LEAD_DAYS = 70                 # 가격은 첫 판단보다 이만큼(달력일) 앞에서부터 받는다
ACTIONS = ("BUY", "HOLD", "SELL")
SOURCES = ("analyst", "insider", "news", "social", "crowd")
SCORE_BINS = (0.0, 35.0, 50.0, 60.0, 70.0, 100.01)
SCORE_LABELS = ("≤35 (매도 문턱)", "35–50", "50–60", "60–70 (매수 문턱~)", "≥70")
ET = ZoneInfo("America/New_York")
RESULTS_DIR = ROOT / "research" / "results"


# ── 기록 읽기 ────────────────────────────────────────────────────────────────

def log_files(specs: list) -> list[Path]:
    """폴더(그 안의 influence_log-*.jsonl)·파일 경로들 → 파일 목록(중복 제거, 정렬)."""
    out = []
    for s in specs:
        p = Path(s).expanduser()
        out += sorted(p.glob("influence_log-*.jsonl")) if p.is_dir() else [p]
    return sorted({p.resolve() for p in out if p.exists()})


def load_judgments(paths: list[Path]) -> tuple[pd.DataFrame, dict]:
    """JSONL → 판단 1건 = 1행 (src, by, ts, ticker, action, score, conviction, stance_<소스>)."""
    rows, bad, versions = [], 0, Counter()
    for p in paths:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    ts = float(rec["ts"])
                except Exception:
                    bad += 1
                    continue
                versions[rec.get("v")] += 1
                for v in rec.get("verdicts") or []:
                    try:
                        row = {"src": rec.get("src") or "?", "by": v.get("by") or rec.get("by") or "?",
                               "ts": ts, "ticker": str(v["ticker"]).upper(),
                               "action": v.get("action"), "score": float(v["score"]),
                               "conviction": float(v.get("conviction") or 0.0),
                               "model": rec.get("model")}
                    except Exception:
                        bad += 1
                        continue
                    for e in v.get("evidence") or []:
                        k = e.get("source_key")
                        if k in SOURCES and e.get("stance") is not None:
                            row[f"stance_{k}"] = float(e["stance"])
                    rows.append(row)
    df = pd.DataFrame(rows)
    for k in SOURCES:
        if f"stance_{k}" not in df.columns:
            df[f"stance_{k}"] = np.nan
    info = {"files": [str(p) for p in paths], "bad_lines": bad, "versions": dict(versions),
            "n_raw": len(df)}
    return df, info


# ── 시간 ─────────────────────────────────────────────────────────────────────

def session_close_ts(d: date) -> float:
    """그날 정규장 마감 epoch (조기폐장 13:00 반영)."""
    return datetime.combine(d, mh.close_time(d), tzinfo=ET).timestamp()


def last_complete_session(now_ts: float) -> date:
    """종가가 확정(마감 + SETTLE_SEC)된 가장 최근 거래일."""
    d = datetime.fromtimestamp(now_ts, ET).date()
    while not (mh.is_trading_day(d) and session_close_ts(d) + SETTLE_SEC <= now_ts):
        d -= timedelta(days=1)
    return d


def first_close_after(ts: float) -> date:
    """ts 뒤 첫 정규장 마감일(규칙 캘린더) — 진입일."""
    d = datetime.fromtimestamp(ts, ET).date()
    while not (mh.is_trading_day(d) and session_close_ts(d) > ts):
        d += timedelta(days=1)
    return d


def trading_days_after(d: date, n: int) -> date:
    """d 뒤 n 번째 거래일(규칙 캘린더 — 아직 오지 않은 날짜의 만기 안내용)."""
    while n > 0:
        d += timedelta(days=1)
        if mh.is_trading_day(d):
            n -= 1
    return d


# ── 채점 ─────────────────────────────────────────────────────────────────────

def score_judgments(df: pd.DataFrame, close: pd.DataFrame, horizons=HORIZONS,
                    bench: str = "SPY") -> pd.DataFrame:
    """판단마다 진입일·h 일 수익·초과수익. close = 완료 세션 종가(행=날짜 오름차순, 열=티커).

    진입 = ts 뒤 첫 마감(close.index 중). 열은 entry_i(없으면 -1 = 아직 진입 종가 없음),
    entry_date, ret_<h>·ex_<h>(대기·가격 없음은 NaN), pending_<h>(만기 전 True)."""
    out = df.copy()
    dates = [pd.Timestamp(x).date() for x in close.index]
    closes = np.array([session_close_ts(d) for d in dates])
    pos = np.searchsorted(closes, out["ts"].to_numpy(dtype=float), side="right")
    n = len(dates)
    out["entry_i"] = np.where(pos < n, pos, -1)
    out["entry_date"] = [dates[i] if i >= 0 else None for i in out["entry_i"]]
    px = close.to_numpy(dtype=float)
    col = {t: j for j, t in enumerate(close.columns)}
    b = col[bench]
    tick = np.array([col.get(t, -1) for t in out["ticker"]])
    ei = out["entry_i"].to_numpy()
    for h in horizons:
        xi = ei + h
        ok = (ei >= 0) & (xi < n)
        ret = np.full(len(out), np.nan)
        bret = np.full(len(out), np.nan)
        have = ok & (tick >= 0)
        r0 = px[ei[have], tick[have]]
        r1 = px[xi[have], tick[have]]
        ret[have] = r1 / r0 - 1
        bret[ok] = px[xi[ok], b] / px[ei[ok], b] - 1
        out[f"ret_{h}"] = ret
        out[f"ex_{h}"] = ret - bret
        out[f"pending_{h}"] = ~ok
    return out


def dedupe(scored: pd.DataFrame) -> pd.DataFrame:
    """같은 (src, by, 종목, 진입일) 은 가장 이른 판단 하나. 진입 종가가 아직 없는 판단은
    (src, by, 종목) 마다 하나만 남겨 '대기' 수를 센다."""
    s = scored.sort_values("ts", kind="stable")
    return s.drop_duplicates(["src", "by", "ticker", "entry_i"], keep="first").reset_index(drop=True)


# ── 통계 ─────────────────────────────────────────────────────────────────────

def clustered(values: pd.Series, days: pd.Series, h: int) -> dict:
    """진입일 군집 평균·t (유효 표본 = 진입일 수 ÷ h)."""
    m = pd.Series(values.to_numpy(dtype=float), index=days.to_numpy()).dropna()
    m = m.groupby(level=0).mean()
    nd = len(m)
    eff = nd / max(1, h)
    mean = float(m.mean()) if nd else float("nan")
    sd = float(m.std(ddof=1)) if nd > 1 else float("nan")
    t = mean / (sd / math.sqrt(eff)) if nd > 1 and sd > 0 and eff >= 2 else float("nan")
    return {"mean": mean, "t": t, "n_days": nd, "eff": eff}


def daily_ic(x: pd.Series, y: pd.Series, days: pd.Series) -> pd.Series:
    """진입일마다 x 와 y 의 스피어만 순위 상관 (그날 MIN_IC_NAMES 미만·x 상수면 제외)."""
    d = pd.DataFrame({"x": x.to_numpy(dtype=float), "y": y.to_numpy(dtype=float),
                      "d": days.to_numpy()}).dropna()
    out = {}
    for day, g in d.groupby("d"):
        if len(g) < MIN_IC_NAMES or g["x"].nunique() < 2 or g["y"].nunique() < 2:
            continue
        out[day] = float(g["x"].rank().corr(g["y"].rank()))
    return pd.Series(out, dtype=float)


def ic_stats(ics: pd.Series, h: int) -> dict:
    nd = len(ics)
    eff = nd / max(1, h)
    mean = float(ics.mean()) if nd else float("nan")
    sd = float(ics.std(ddof=1)) if nd > 1 else float("nan")
    t = mean / (sd / math.sqrt(eff)) if nd > 1 and sd > 0 and eff >= 2 else float("nan")
    return {"mean": mean, "t": t, "n_days": nd, "eff": eff}


def verdict_label(stat: dict) -> str:
    if stat["eff"] < MIN_INDEPENDENT or stat["t"] != stat["t"]:
        return f"보류 (유효 표본 {stat['eff']:.0f} < {MIN_INDEPENDENT})"
    if stat["t"] >= T_SIGNAL:
        return "양(+)의 신호"
    if stat["t"] <= -T_SIGNAL:
        return "역효과"
    return "구별 못 함"


def summarize_group(g: pd.DataFrame, horizons=HORIZONS) -> dict:
    """한 (src, by) 묶음 → 기간별 지표."""
    out = {"n_judgments": int(len(g)), "n_tickers": int(g["ticker"].nunique()),
           "actions": {a: int((g["action"] == a).sum()) for a in ACTIONS},
           "no_entry_yet": int((g["entry_i"] < 0).sum()), "horizons": {}}
    for h in horizons:
        ex, ret = f"ex_{h}", f"ret_{h}"
        done = g[~g[f"pending_{h}"] & g[ex].notna()]
        hz = {"n": int(len(done)), "pending": int(g[f"pending_{h}"].sum()),
              "no_price": int((~g[f"pending_{h}"] & g[ex].isna()).sum())}
        if len(done):
            days = done["entry_date"]
            hz["all"] = {**clustered(done[ex], days, h), "n": int(len(done)),
                         "ret": float(done[ret].mean()), "hit": float((done[ex] > 0).mean())}
            hz["by_action"] = {}
            for a in ACTIONS:
                s = done[done["action"] == a]
                if len(s):
                    hz["by_action"][a] = {**clustered(s[ex], s["entry_date"], h), "n": int(len(s)),
                                          "median": float(s[ex].median()),
                                          "hit": float((s[ex] > 0).mean()),
                                          "ret": float(s[ret].mean())}
            # BUY − 그날 판단한 종목 전체 (일자별 차이)
            day_all = done.groupby("entry_date")[ex].mean()
            buy = done[done["action"] == "BUY"]
            if len(buy):
                buy_day = buy.groupby("entry_date")[ex].mean()
                diff = buy_day - day_all.reindex(buy_day.index)
                hz["buy_minus_all"] = clustered(diff, pd.Series(diff.index), h)
                hz["buy_minus_all"]["label"] = verdict_label(hz["buy_minus_all"])
            ic = ic_stats(daily_ic(done["score"], done[ex], days), h)
            ic["label"] = verdict_label(ic)
            hz["score_ic"] = ic
            bins = pd.cut(done["score"], SCORE_BINS, right=False, labels=SCORE_LABELS)
            hz["score_bins"] = {str(lab): {"n": int(len(s)), "mean": float(s[ex].mean()),
                                           "hit": float((s[ex] > 0).mean())}
                                for lab, s in done.groupby(bins, observed=True)}
            hz["source_ic"] = {}
            for k in SOURCES:
                st = ic_stats(daily_ic(done[f"stance_{k}"], done[ex], days), h)
                if st["n_days"]:
                    st["label"] = verdict_label(st)
                    st["coverage"] = float(done[f"stance_{k}"].notna().mean())
                    hz["source_ic"][k] = st
        out["horizons"][str(h)] = hz
    return out


# ── 가격 ─────────────────────────────────────────────────────────────────────

def load_closes(tickers: list, first: date, last: date) -> tuple[pd.DataFrame, list]:
    """first~last(포함) 완료 세션 종가. 날짜 축 = SPY 거래일. 반환: (종가 표, 못 받은 티커)."""
    import backtester as bt
    # download_prices 는 종가 30개 이하인 종목을 버린다 → 판단 기간이 짧아도 앞쪽을 넉넉히 받는다
    start = (first - timedelta(days=PRICE_LEAD_DAYS)).isoformat()
    end_excl = (last + timedelta(days=1)).isoformat()
    px, missing = bt.download_prices(sorted(set(tickers) | {"SPY"}), start, end_excl)
    if "SPY" not in px:
        raise SystemExit("SPY 가격을 받지 못했습니다")
    idx = pd.DatetimeIndex(px["SPY"].index)
    idx = idx[(idx.date >= first) & (idx.date <= last)]
    close = pd.DataFrame({t: px[t]["Close"].reindex(idx) for t in px}, index=idx)
    return close, missing


# ── 보고서 ───────────────────────────────────────────────────────────────────

def _p(x, d=2) -> str:
    return "—" if x is None or x != x else f"{x:+.{d}%}"


def _t(x) -> str:
    return "—" if x is None or x != x else f"{x:+.1f}"


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def write_report(ctx: dict, path: Path):
    L = [f"# AI 판단 forward 평가 — {ctx['date']}", "",
         f"판단 기록 {ctx['first_judgment']} ~ {ctx['last_judgment']} (UTC) · 가격은 "
         f"{ctx['last_session']} 장 마감까지. 생성: `research/ai_forward_eval.py`.", "",
         "## 요약 (자동 생성)", ""]
    for name, g in ctx["groups"].items():
        h1 = g["horizons"][str(HORIZONS[0])]
        L.append(f"- **{name}**: 판단 {g['n_judgments']:,}건(중복 제거 후) · {g['n_tickers']}종목 · "
                 f"BUY {g['actions']['BUY']:,} / HOLD {g['actions']['HOLD']:,} / SELL {g['actions']['SELL']:,}. "
                 f"채점된 판단: " + ", ".join(
                     f"{h}일 {g['horizons'][str(h)]['n']:,}건" for h in HORIZONS) + ".")
        for h in HORIZONS:
            hz = g["horizons"][str(h)]
            if hz.get("buy_minus_all"):
                b, ic = hz["buy_minus_all"], hz["score_ic"]
                L.append(f"  - {h}일: BUY − 전체 {_p(b['mean'])} (t {_t(b['t'])}) → **{b['label']}**; "
                         f"점수 IC {ic['mean']:+.3f} (t {_t(ic['t'])}) → {ic['label']}")
        if not h1["n"]:
            L.append(f"  - 아직 채점할 판단이 없다. 첫 {HORIZONS[0]}일 결과는 "
                     f"{ctx['maturity'][str(HORIZONS[0])]} 장 마감 뒤, {HORIZONS[-1]}일 결과는 "
                     f"{ctx['maturity'][str(HORIZONS[-1])]} 이후.")
    L.append(f"- 판정 기준: 유효 표본(진입일 수 ÷ h) {MIN_INDEPENDENT} 이상에서 |t| ≥ {T_SIGNAL:.0f}. "
             f"그 전엔 '보류'. 판단기: " + ", ".join(f"`{k}` {v:,}건" for k, v in ctx["by_counts"].items())
             + ".")
    if ctx["missing_prices"]:
        L.append(f"- 가격을 받지 못한 종목(상장폐지·인수·티커 변경 추정 — 판단은 '가격 없음'으로 빠짐): "
                 + ", ".join(f"`{t}`" for t in ctx["missing_prices"]) + ".")
    L.append("")

    L += ["## 채점 규칙 (고정)", "",
          "- **진입**: 판단 기록 시각 뒤 첫 정규장 종가(장중 판단 → 그날 종가, 마감 뒤·휴장일 → 다음 거래일 종가, "
          "조기폐장 13:00 반영). 판단 뒤 가격만 쓰므로 미래 참조가 없고, 판단~종가 사이 움직임은 놓친다.",
          "- **청산**: 진입 뒤 h 거래일 종가, h = " + "·".join(map(str, HORIZONS)) + ". 만기 전 판단은 '대기'.",
          "- **초과수익**: 종목 수익률 − 같은 날짜 SPY 수익률(yfinance 수정주가).",
          "- **중복 제거**: 같은 (경로, 판단기, 종목, 진입일)은 가장 이른 판단 하나.",
          "- **유의성**: 진입일별 평균을 한 표본으로(일자 군집), 유효 표본 = 진입일 수 ÷ h 로 t 계산. "
          f"IC 는 하루 {MIN_IC_NAMES}종목 이상 판단한 날만.", ""]

    for name, g in ctx["groups"].items():
        L += [f"## {name}", ""]
        L += ["| 기간 | 채점 | 대기 | 가격 없음 | 전체 평균 초과 | BUY 평균 초과 (t) | BUY 승률 | HOLD 평균 초과 | "
              "SELL 평균 초과 | BUY − 전체 (t) | 점수 IC (t) |",
              "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
        for h in HORIZONS:
            hz = g["horizons"][str(h)]
            ba = hz.get("by_action", {})
            bu, ho, se = ba.get("BUY"), ba.get("HOLD"), ba.get("SELL")
            bm, ic, al = hz.get("buy_minus_all"), hz.get("score_ic"), hz.get("all")
            cells = [f"{h}일", f"{hz['n']:,}", f"{hz['pending']:,}", f"{hz['no_price']:,}",
                     _p(al["mean"]) if al else "—",
                     f"{_p(bu['mean'])} ({_t(bu['t'])})" if bu else "—",
                     f"{bu['hit']:.0%}" if bu else "—",
                     _p(ho["mean"]) if ho else "—", _p(se["mean"]) if se else "—",
                     f"{_p(bm['mean'])} ({_t(bm['t'])})" if bm else "—",
                     f"{ic['mean']:+.3f} ({_t(ic['t'])})" if ic and ic["n_days"] else "—"]
            L.append("| " + " | ".join(cells) + " |")
        L.append("")
        hl = next((h for h in reversed(HORIZONS) if g["horizons"][str(h)].get("score_bins")), None)
        if hl:
            hz = g["horizons"][str(hl)]
            L += [f"**점수 구간별 — {hl}일 초과수익**", "", "| 점수 | 판단 | 평균 초과 | 승률 |", "|---|---:|---:|---:|"]
            for lab in SCORE_LABELS:
                s = hz["score_bins"].get(lab)
                if s:
                    L.append(f"| {lab} | {s['n']:,} | {_p(s['mean'])} | {s['hit']:.0%} |")
            L += ["", f"**소스별 IC — {hl}일** (증거 stance 와 초과수익의 순위 상관)", "",
                  "| 소스 | 증거 있는 판단 비율 | IC | t | 판정 |", "|---|---:|---:|---:|---|"]
            for k, s in hz["source_ic"].items():
                L.append(f"| `{k}` | {s['coverage']:.0%} | {s['mean']:+.3f} | {_t(s['t'])} | {s['label']} |")
            L.append("")

    L += ["## 한계", "",
          "- 판단 시각부터 그날 종가까지의 움직임은 채점에서 빠진다(라이브 매수는 판단 직후 체결).",
          "- 스캐너 판단 대상은 전체 시장 순회 순서에 따라 날마다 다르다 — 'BUY − 전체' 를 같은 날 판단한 종목끼리 "
          "비교하는 이유.",
          "- 유효 표본 ÷ h 는 겹치는 창에 대한 거친 보정이다. 여러 기간·소스를 동시에 보므로 |t| ≥ 2 가 우연히 "
          "나올 수 있다 — 한 칸의 신호보다 기간·경로에 걸친 일관성을 본다.",
          "- 상장폐지·티커 변경 종목은 가격이 없어 '가격 없음' 으로 빠진다.", ""]
    path.write_text("\n".join(L), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--logs", nargs="*", default=None,
                    help="influence_log 폴더·파일 (기본: 설정 파일 폴더 = 앱이 기록하는 곳)")
    ap.add_argument("--no-write", action="store_true", help="문서·JSON 을 쓰지 않고 요약만 출력")
    a = ap.parse_args(argv)
    if a.logs is None:
        from core import control
        a.logs = [str(control.CONFIG_FILE.parent)]
    paths = log_files(a.logs)
    if not paths:
        raise SystemExit(f"influence_log-*.jsonl 이 없습니다: {a.logs}")
    df, info = load_judgments(paths)
    if df.empty:
        raise SystemExit("판단 기록이 비어 있습니다")
    try:
        import yf_compat
        yf_compat.patch_tz_cache()
    except Exception:
        pass

    now = time.time()
    last = last_complete_session(now)
    first = datetime.fromtimestamp(float(df["ts"].min()), ET).date()
    if last < first:
        close = pd.DataFrame(columns=sorted(set(df["ticker"]) | {"SPY"}), dtype=float)
        missing = []
    else:
        close, missing = load_closes(sorted(df["ticker"].unique()), first, last)
    scored = dedupe(score_judgments(df, close))

    groups = {}
    for (src, by), g in scored.groupby(["src", "by"]):
        label = {"scanner": "전체 시장 스캐너", "engine": "자동매매 엔진"}.get(src, src)
        groups[f"{label} (`{src}` · 판단기 `{by}`)"] = summarize_group(g)
    entry0 = first_close_after(float(df["ts"].min()))   # 가장 이른 판단의 진입일
    today = date.today().isoformat()
    ctx = {"date": today, "info": info, "missing_prices": missing,
           "first_judgment": _utc(float(df["ts"].min())), "last_judgment": _utc(float(df["ts"].max())),
           "last_session": last.isoformat(), "by_counts": dict(Counter(scored["by"])),
           "maturity": {str(h): trading_days_after(entry0, h).isoformat() for h in HORIZONS},
           "groups": groups}
    for name, g in groups.items():
        print(f"{name}: 판단 {g['n_judgments']:,} · 채점 " + ", ".join(
            f"{h}일 {g['horizons'][str(h)]['n']:,}" for h in HORIZONS))
    if a.no_write:
        return ctx
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    js = RESULTS_DIR / f"ai-forward-{today}.json"
    doc = ROOT / "docs" / f"ai-forward-{today}.md"
    js.write_text(json.dumps({"params": {"horizons": HORIZONS, "min_independent": MIN_INDEPENDENT,
                                         "min_ic_names": MIN_IC_NAMES, "t_signal": T_SIGNAL},
                              **{k: v for k, v in ctx.items() if k != "date"}},
                             ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    write_report(ctx, doc)
    print(f"→ {doc.relative_to(ROOT)} · {js.relative_to(ROOT)}")
    return ctx


if __name__ == "__main__":
    main()

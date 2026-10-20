"""
AI 판단기·전체시장 스캐너·설정/키 저장·텔레그램 소유자 등록·브로커 엔드포인트 회귀 테스트.

test_core.py 가 다루지 않던(그리고 실제로 사고가 났던) 경로들:
  · 설정 파일 읽기/쓰기 레이스로 사용자 설정이 기본값으로 덮이던 문제
  · 텔레그램 사용자명 chat_id 로 알림이 안 가던 문제(숫자 소유자 부트스트랩)
  · 근거 없는(시간초과) AI 판단이 근거 있는 판단을 덮던 문제
  · 페이퍼 키(PK)로 '실거래' 모드 시 실계좌 엔드포인트로 가서 401 나던 문제
  · AI 판단 시점 기록(forward 검증용 JSONL) — 판단마다 1줄·캐시 적중 미기록·실패 무영향

모든 상태 파일은 tmp_path 로 격리 — 실제 장부·설정·.env 는 건드리지 않는다.
실행: python3 -m pytest test_influence_and_config.py -q
"""
import json
import os
import stat
import time

import pytest


# ── AI 판단기 (규칙 폴백) ───────────────────────────────────────────────────
def _ev(stance, cred=0.9, conf=1.0, key="analyst"):
    import ai_judge as aj
    return aj.Evidence(key, key, "권위", cred, stance, conf, detail="t")


def test_aggregate_empty_is_neutral():
    import ai_judge as aj
    assert aj.aggregate([]) == (0.0, 0.0)
    assert aj.stance_to_score(0.0) == 50.0


def test_stance_to_score_clamps():
    import ai_judge as aj
    assert aj.stance_to_score(5.0) == 100.0
    assert aj.stance_to_score(-5.0) == 0.0


def test_rule_judge_strong_buy_and_no_evidence():
    import ai_judge as aj
    v = aj._judge_rule("AAA", [_ev(1.0), _ev(0.8, key="insider")], 60, 35)
    assert v.action == "BUY" and v.score >= 60 and v.by == "rule"
    assert 0 < v.conviction <= 1
    empty = aj._judge_rule("BBB", [], 60, 35)
    assert empty.action == "HOLD" and empty.score == 50.0
    assert "근거 부족" in empty.rationale


def test_rule_judge_strong_sell():
    import ai_judge as aj
    v = aj._judge_rule("CCC", [_ev(-1.0), _ev(-0.9, key="news")], 60, 35)
    assert v.action == "SELL" and v.score <= 35


def test_extract_json_array_tolerates_fences_and_prose():
    import ai_judge as aj
    assert aj._extract_json_array('```json\n[{"t":"A"}]\n```') == [{"t": "A"}]
    assert aj._extract_json_array('결과: [1, 2] 끝') == [1, 2]
    assert aj._extract_json_array("not json") is None
    assert aj._extract_json_array('{"a": 1}') is None


def test_judge_batch_without_llm_uses_rules():
    import ai_judge as aj
    out = aj.judge_batch([("AAA", [_ev(1.0)], {})], use_llm=False)
    assert len(out) == 1 and out[0].by == "rule" and out[0].ticker == "AAA"


# ── 전체 시장 스캐너 병합 ───────────────────────────────────────────────────
@pytest.fixture
def scanner_full(monkeypatch):
    from influence import scanner
    monkeypatch.setattr(scanner, "FULL", {})
    return scanner


def _verdict(tk, score, evidence):
    import ai_judge as aj
    return aj.Verdict(tk, "HOLD", score, 0.5, "", "rule", evidence)


def test_merge_keeps_evidence_backed_verdict(scanner_full):
    sc = scanner_full
    sc.merge({"AAA": _verdict("AAA", 80, [_ev(1.0)])})
    sc.merge({"AAA": _verdict("AAA", 50, [])})          # 시간초과 → 근거 없음
    assert sc.FULL["AAA"].score == 80


def test_merge_newer_evidence_replaces(scanner_full):
    sc = scanner_full
    sc.merge({"AAA": _verdict("AAA", 80, [_ev(1.0)])})
    sc.merge({"AAA": _verdict("AAA", 30, [_ev(-1.0)])})
    assert sc.FULL["AAA"].score == 30


def test_top_buys_threshold_and_order(scanner_full):
    sc = scanner_full
    sc.merge({t: _verdict(t, s, [_ev(0.5)]) for t, s in
              (("LOW", 40), ("MID", 65), ("HIGH", 90), ("EDGE", 60))})
    assert sc.top_buys(10, 60) == ["HIGH", "MID", "EDGE"]
    assert sc.top_buys(1, 60) == ["HIGH"]


# ── AI 판단 시점 기록 (influence_log-YYYYMM.jsonl) ─────────────────────────
@pytest.fixture
def eng_log(tmp_path, monkeypatch, scanner_full):
    import ai_judge as aj
    from influence import engine, providers
    monkeypatch.setattr(engine, "LAST",
                        {"ts": 0.0, "verdicts": {}, "by": "rule", "regime": "neutral"})
    monkeypatch.setattr(engine, "LOG_DIR", tmp_path)
    monkeypatch.setattr(aj, "_llm_config", lambda: None)   # .env 키와 무관하게 규칙 판단
    calls = []

    def fake_gather(t):
        calls.append(t)
        return [aj.Evidence("analyst", "월가 애널리스트", "권위", 0.92, 0.8, 1.0,
                            "25곳 중 매수 20", {"buy": 20})]
    monkeypatch.setattr(providers, "gather", fake_gather)
    return engine, tmp_path, calls


def _log_rows(d):
    return [json.loads(line) for f in sorted(d.glob("influence_log-*.jsonl"))
            for line in f.read_text().splitlines()]


def test_judgment_appends_one_point_in_time_line(eng_log):
    eng, d, _ = eng_log
    scores, verdicts = eng.score_universe(["AAA", "BBB"], held={"BBB"}, regime="bull",
                                          prices={"AAA": 12.5})
    rows = _log_rows(d)
    assert len(rows) == 1
    r = rows[0]
    assert (r["v"], r["regime"], r["by"], r["provider"], r["model"]) == \
        (1, "bull", "rule", None, None)
    assert (r["buy_th"], r["sell_th"], r["held"]) == (60.0, 35.0, ["BBB"])
    assert r["iso"].endswith("+00:00") and abs(r["ts"] - time.time()) < 60
    assert (d / f"influence_log-{time.strftime('%Y%m', time.gmtime(r['ts']))}.jsonl").exists()
    by_t = {v["ticker"]: v for v in r["verdicts"]}
    assert set(by_t) == {"AAA", "BBB"}
    a = by_t["AAA"]
    assert a["score"] == scores["AAA"] and a["action"] == verdicts["AAA"].action
    assert a["rationale"] == verdicts["AAA"].rationale and a["by"] == "rule"
    assert a["price"] == 12.5 and by_t["BBB"]["price"] is None
    ev = a["evidence"][0]
    assert (ev["source_key"], ev["stance"], ev["confidence"], ev["credibility"], ev["raw"]) \
        == ("analyst", 0.8, 1.0, 0.92, {"buy": 20})


def test_cache_hit_does_not_log(eng_log):
    eng, d, calls = eng_log
    eng.score_universe(["AAA", "BBB"])
    n = len(calls)
    eng.score_universe(["AAA"])              # TTL 내 + 기존 판단의 부분집합 → 캐시 적중
    assert len(calls) == n                   # 재수집 없음 = 새 판단 없음
    assert len(_log_rows(d)) == 1


def test_log_write_failure_never_breaks_scoring(eng_log, monkeypatch):
    eng, d, _ = eng_log
    blocker = d / "not_a_dir"
    blocker.write_text("")
    monkeypatch.setattr(eng, "LOG_DIR", blocker / "sub")   # mkdir 실패 → OSError
    msgs = []
    scores, verdicts = eng.score_universe(["AAA"], log=msgs.append)
    assert scores == {"AAA": verdicts["AAA"].score} and scores["AAA"] > 50
    assert eng.LAST["verdicts"]["AAA"] is verdicts["AAA"]
    assert any("기록 실패" in m for m in msgs)


def test_llm_judgment_logs_model_but_never_key(eng_log, monkeypatch):
    import ai_judge as aj
    eng, d, _ = eng_log
    cfg = {"provider": "anthropic", "key": "sk-ant-SECRET-0123456789",
           "model": "claude-test", "url": "http://x"}
    monkeypatch.setattr(aj, "_llm_config", lambda: cfg)
    monkeypatch.setattr(aj, "_judge_llm", lambda items, c, b, s: [
        aj.Verdict(t, "BUY", 77.0, 0.6, "근거", "llm", list(ev)) for t, ev, _ in items])
    eng.score_universe(["AAA"])
    text = next(d.glob("influence_log-*.jsonl")).read_text()
    r = json.loads(text)
    assert (r["by"], r["provider"], r["model"]) == ("llm", "anthropic", "claude-test")
    assert r["verdicts"][0]["score"] == 77.0
    assert "SECRET" not in text


def test_append_jsonl_concurrent_processes_never_interleave(tmp_path):
    import subprocess
    import sys
    f = tmp_path / "log.jsonl"
    code = ("import sys, safe_store\n"
            "for i in range(30):\n"
            "    safe_store.append_jsonl(sys.argv[1],"
            " {'w': sys.argv[2], 'i': i, 'pad': sys.argv[2] * 40000})\n")
    here = os.path.dirname(os.path.abspath(__file__))
    procs = [subprocess.Popen([sys.executable, "-c", code, str(f), w], cwd=here)
             for w in "abcd"]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    rows = [json.loads(line) for line in f.read_text().splitlines()]
    assert len(rows) == 120                  # 40KB 줄 × 4프로세스 동시 — 섞이면 파싱 실패
    assert all(r["pad"] == r["w"] * 40000 for r in rows)


def test_append_jsonl_isolates_torn_last_line(tmp_path):
    from safe_store import append_jsonl
    f = tmp_path / "log.jsonl"
    f.write_text('{"ok": 1}\n{"torn": ')     # 크래시로 줄 중간에 끊긴 마지막 기록
    append_jsonl(f, {"ok": 2})
    lines = f.read_text().splitlines()
    assert lines[:2] == ['{"ok": 1}', '{"torn": ']
    assert json.loads(lines[2]) == {"ok": 2}


def test_append_jsonl_gives_up_on_held_lock(tmp_path):
    fcntl = pytest.importorskip("fcntl")
    from safe_store import append_jsonl
    f = tmp_path / "log.jsonl"
    with open(f, "a") as holder:
        fcntl.flock(holder, fcntl.LOCK_EX)
        t = time.monotonic()
        with pytest.raises(TimeoutError):
            append_jsonl(f, {"x": 1}, lock_timeout=0.05)
        assert time.monotonic() - t < 1.0    # 사이클을 오래 붙잡지 않음
    assert f.read_text() == ""


# ── 키 판별 / .env 저장 ────────────────────────────────────────────────────
def test_key_set_rejects_placeholders():
    import config
    assert not config._key_set("")
    assert not config._key_set("your_alpaca_api_key")
    assert not config._key_set("여기에_토스_APP_KEY_입력")
    assert not config._key_set("short")
    assert config._key_set("PKABCDEFGHIJKLMNOPQR")


def test_alpaca_key_kind(monkeypatch):
    import config
    monkeypatch.setattr(config, "ALPACA_API_KEY", "PK1234567890ABCDEF")
    assert config.alpaca_key_kind() == "paper"
    monkeypatch.setattr(config, "ALPACA_API_KEY", "AK1234567890ABCDEF")
    assert config.alpaca_key_kind() == "live"
    monkeypatch.setattr(config, "ALPACA_API_KEY", "your_alpaca_api_key")
    assert config.alpaca_key_kind() == ""


def test_set_env_lines_dedups_preserves_and_locks(tmp_path):
    import config
    env = tmp_path / ".env"
    env.write_text("# 주석 유지\nFINNHUB_API_KEY=fh\nALPACA_API_KEY=old1\n"
                   "ALPACA_API_KEY=old2\n# ALPACA_SECRET_KEY=commented\n")
    config._set_env_lines(env, {"ALPACA_API_KEY": "new", "ALPACA_SECRET_KEY": "sec"})
    lines = env.read_text().splitlines()
    assert lines.count("ALPACA_API_KEY=new") == 1
    assert not any(l.startswith("ALPACA_API_KEY=old") for l in lines)
    assert "# 주석 유지" in lines and "FINNHUB_API_KEY=fh" in lines
    assert "# ALPACA_SECRET_KEY=commented" in lines      # 주석 줄은 키로 보지 않음
    assert "ALPACA_SECRET_KEY=sec" in lines
    assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
    assert not list(tmp_path.glob(".env.*.tmp"))           # 임시파일 잔존 없음


def test_set_env_lines_creates_missing_file(tmp_path):
    import config
    env = tmp_path / ".env"
    config._set_env_lines(env, {"K": "v"})
    assert env.read_text() == "K=v\n"


# ── 브로커 엔드포인트 (돈이 오가는 쪽으로는 절대 보정하지 않는다) ─────────────
@pytest.mark.parametrize("key,base,paper,expect_paper", [
    ("PK1234567890ABCDEF", "https://api.alpaca.markets", False, True),   # PK 는 항상 페이퍼
    ("AK1234567890ABCDEF", "https://paper-api.alpaca.markets", False, False),
    ("AK1234567890ABCDEF", "https://api.alpaca.markets", True, True),    # 모의 요청은 그대로
    ("XX1234567890ABCDEF", "https://paper-api.alpaca.markets", False, True),
    ("XX1234567890ABCDEF", "https://api.alpaca.markets", False, False),
    ("PK1234567890ABCDEF", "https://paper-api.alpaca.markets", True, True),
])
def test_broker_paper_endpoint(monkeypatch, key, base, paper, expect_paper):
    broker = pytest.importorskip("broker")
    monkeypatch.setattr(broker, "ALPACA_API_KEY", key)
    monkeypatch.setattr(broker, "ALPACA_BASE_URL", base)
    assert broker._paper_endpoint(paper) is expect_paper


# ── 설정 파일 레이스 (core.control) ─────────────────────────────────────────
@pytest.fixture
def ctl(tmp_path, monkeypatch):
    from core import control
    monkeypatch.setattr(control, "CONFIG_FILE", tmp_path / "autotrader_config.json")
    monkeypatch.setattr(control, "_LAST_GOOD", {})
    return control


def test_load_config_creates_defaults_only_when_missing(ctl):
    cfg = ctl.load_config()
    assert ctl.CONFIG_FILE.exists()
    assert cfg.get("paper", True) is True


def test_truncated_read_returns_last_good_and_never_overwrites(ctl):
    ctl.save_config({"interval": 15, "strategy": "ai_influencer", "enabled": True})
    assert ctl.load_config()["interval"] == 15
    ctl.CONFIG_FILE.write_text('{"interval": 1')            # 쓰는 중 잘린 파일
    got = ctl.load_config()
    assert got["interval"] == 15 and got["strategy"] == "ai_influencer"
    assert ctl.CONFIG_FILE.read_text() == '{"interval": 1'  # 기본값으로 덮지 않음


def test_corrupt_without_last_good_fails_closed(ctl):
    ctl.CONFIG_FILE.write_text("{broken")
    got = ctl.load_config()
    assert got["enabled"] is False
    assert ctl.CONFIG_FILE.read_text() == "{broken"


# ── 텔레그램 소유자 부트스트랩 ─────────────────────────────────────────────
@pytest.fixture
def tg(tmp_path, monkeypatch):
    import notifier
    import telegram_bot
    monkeypatch.setattr(notifier, "NOTIFY_CONFIG_FILE", tmp_path / "notify_config.json")
    monkeypatch.setattr(notifier, "_NOTIFY_LAST_GOOD", {})
    monkeypatch.setattr(telegram_bot, "_allowed_chat", "")
    sent = []
    monkeypatch.setattr(telegram_bot, "_send", lambda tok, cid, text: sent.append((cid, text)))
    monkeypatch.setattr(telegram_bot, "handle", lambda text, cid="": f"handled:{text}")
    return telegram_bot, notifier, sent


def _upd(chat_id, text):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "text": text}}


def test_first_plain_message_registers_owner(tg):
    bot, notifier, sent = tg
    bot._process_update("tok", _upd(12345, "안녕"))
    assert notifier.load_config()["telegram"]["chat_id"] == "12345"
    assert sent and sent[0][0] == "12345"


def test_non_owner_ignored_and_owner_not_hijacked(tg):
    bot, notifier, sent = tg
    bot._process_update("tok", _upd(111, "/start"))
    sent.clear()
    bot._process_update("tok", _upd(999, "/status"))
    assert sent == []
    assert notifier.load_config()["telegram"]["chat_id"] == "111"


def test_owner_command_dispatched_and_bare_slash_safe(tg):
    bot, notifier, sent = tg
    bot._process_update("tok", _upd(111, "/start"))
    sent.clear()
    bot._process_update("tok", _upd(111, "/status"))
    assert sent == [("111", "handled:/status")]
    sent.clear()
    bot._process_update("tok", _upd(111, "/"))            # 예전엔 IndexError
    assert sent and "/help" in sent[0][1]


def test_first_message_command_runs_after_registration(tg):
    bot, notifier, sent = tg
    bot._process_update("tok", _upd(222, "/status"))
    assert sent[-1] == ("222", "handled:/status")


# ── 사이클 알림 라벨 ───────────────────────────────────────────────────────
def test_cycle_mode_label_follows_broker_endpoint():
    from types import SimpleNamespace
    from core import cycle
    assert cycle._mode_label(True) == "모의"
    assert cycle._mode_label(False, SimpleNamespace(paper=True)) == "Alpaca 페이퍼"
    assert cycle._mode_label(False, SimpleNamespace(paper=False)) == "🔴 실거래"
    assert isinstance(cycle._market_open(), bool)

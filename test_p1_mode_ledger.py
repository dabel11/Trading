"""
P1 안전장치 — 실거래 전환 2단계 확인(감사 #21), 장부 손상 fail-closed(감사 #13).

모든 장부·설정·저널 파일은 tmp_path 로 격리한다. 네트워크(Alpaca)는 가짜로 대체.
실행: python3 -m pytest test_p1_mode_ledger.py -q
"""
import json

import pytest


@pytest.fixture
def env(tmp_path, monkeypatch):
    import safe_store, portfolio, paper_account, config, broker, alpaca_api
    from core import control, intents, mode
    monkeypatch.setattr(safe_store, "_TRADE_LOCK_FILE", tmp_path / ".trade.lock")
    monkeypatch.setattr(portfolio, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(paper_account, "PAPER_FILE", tmp_path / "paper_account.json")
    monkeypatch.setattr(control, "CONFIG_FILE", tmp_path / "autotrader_config.json")
    monkeypatch.setattr(control, "PID_FILE", tmp_path / "autotrader.pid")
    monkeypatch.setattr(intents, "JOURNAL_FILE", None)
    monkeypatch.setattr(config, "alpaca_ready", lambda: True)
    monkeypatch.setattr(broker, "ALPACA_API_KEY", "PK1234567890ABCDEF")
    monkeypatch.setattr(alpaca_api, "account", lambda: {
        "ok": True, "account": "****1234", "equity": 100000.0, "buying_power": 200000.0, "status": "ACTIVE"})
    mode._PENDING.clear()
    control.save_config({"paper": True, "enabled": True, "auto_running": False})
    paper_account.reset(10000)
    return tmp_path


def _hold(paper, ticker="AAPL"):
    import portfolio
    portfolio.PortfolioManager(paper=paper).record_buy(ticker, 3, 100.0, 70)


# ── #21 실거래 전환 ──────────────────────────────────────────────────────
def test_target_label_follows_key_kind(env, monkeypatch):
    import broker
    from core import mode
    assert mode.target_label() == ("Alpaca 페이퍼 계좌 — 가상 돈(실주문 경로 검증)", False)
    monkeypatch.setattr(broker, "ALPACA_API_KEY", "AK1234567890ABCDEF")
    assert mode.target_label()[1] is True


def test_live_blocked_while_auto_running_or_holding(env):
    from core import control, mode
    control.save_config({**control.load_config(), "auto_running": True})
    _hold(True)
    r = mode.request_live("app")
    assert not r["ok"] and "code" not in r
    assert any("자동매매가 실행 중" in b for b in r["blockers"])
    assert any("보유 1종목" in b and "AAPL" in b for b in r["blockers"])


def test_live_blocked_without_keys_or_broker(env, monkeypatch):
    import config, alpaca_api
    from core import mode
    monkeypatch.setattr(alpaca_api, "account", lambda: {"ok": False, "error": "인증 실패(401)"})
    assert any("연결 실패" in b for b in mode.check(False)["blockers"])
    monkeypatch.setattr(config, "alpaca_ready", lambda: False)
    assert any("API 키가 없습니다" in b for b in mode.check(False)["blockers"])


def test_two_step_code_wrong_then_right(env):
    from core import control, mode
    r = mode.request_live("app")
    assert r["ok"] and len(r["code"]) == 4 and "주문이 가는 곳" in r["notes"][0]
    bad = mode.confirm_live("0000" if r["code"] != "0000" else "1111", "app")
    assert not bad["switched"] and control.load_config()["paper"] is True
    r = mode.request_live("app")                           # 틀리면 다시 요청해야 한다
    ok = mode.confirm_live(r["code"], "app")
    assert ok["switched"] and control.load_config()["paper"] is False


def test_code_is_single_use_and_expires(env, monkeypatch):
    from core import mode
    r = mode.request_live("app")
    monkeypatch.setattr(mode.time, "time", lambda: 9e12)  # 만료 이후
    assert not mode.confirm_live(r["code"], "app")["switched"]


def test_confirm_rechecks_preconditions(env):
    from core import control, mode
    r = mode.request_live("app")
    control.save_config({**control.load_config(), "auto_running": True})   # 그 사이 자동매매 시작
    res = mode.confirm_live(r["code"], "app")
    assert not res["switched"] and control.load_config()["paper"] is True


def test_codes_are_per_requester(env):
    from core import mode
    r = mode.request_live("app")
    assert not mode.confirm_live(r["code"], "telegram")["switched"]


def test_back_to_paper_blocked_by_live_holdings(env):
    from core import control, mode
    control.save_config({**control.load_config(), "paper": False})
    _hold(False, "MSFT")
    r = mode.switch_to_paper()
    assert not r["ok"] and any("MSFT" in b for b in r["blockers"])
    assert control.load_config()["paper"] is False


def test_telegram_two_step_flow(env, monkeypatch):
    import telegram_bot as tg
    from core import control
    monkeypatch.setattr(tg, "_is_owner", lambda cid: True)
    monkeypatch.setattr(tg, "write_status", lambda s: None)
    first = tg.handle("/실거래", "1")
    assert "확인하려면" in first and control.load_config()["paper"] is True
    code = first.rsplit(" ", 1)[-1].strip()
    done = tg.handle(f"/실거래 {code}", "1")
    assert "실거래 모드로 전환" in done and "Alpaca 페이퍼" in done
    assert control.load_config()["paper"] is False
    assert "모의투자(페이퍼) 모드로 전환" in tg.handle("/모의", "1")


# ── #13 장부 손상 fail-closed ────────────────────────────────────────────
def test_paper_corruption_halts_all_and_alerts_once(env):
    from core import ledger_guard, intents
    import portfolio
    _hold(True)
    (env / "state_paper.json").write_text("{not json")
    portfolio.PortfolioManager(paper=True)                # 앱 화면 등의 읽기가 .corrupt 로 옮긴다
    alerts = []
    r = ledger_guard.check(True, alert=alerts.append)
    assert r["halted"] and r["block_all"] and "state_paper.json" in r["reason"]
    h = intents.halted(intents.load())
    assert h["by"] == "ledger"
    assert len(alerts) == 1
    assert not (env / "state_paper.json.corrupt").exists()            # 시각 붙여 보관
    assert list(env.glob("state_paper.json.corrupt-*"))
    assert ledger_guard.check(True, alert=alerts.append)["halted"] and len(alerts) == 1


def test_resume_after_restore_runs_again(env):
    from core import ledger_guard, intents
    (env / "paper_account.json").write_text("garbage")
    assert ledger_guard.check(True)["halted"]
    (env / "paper_account.json").write_text(json.dumps({"cash": 10000.0, "seed": 10000.0}))  # 백업에서 복구
    intents.clear_halt()                                                 # /resume
    assert ledger_guard.check(True) == {"halted": False, "block_all": False, "reason": ""}


def test_live_corruption_blocks_buys_not_all(env):
    from core import ledger_guard
    (env / "state.json").write_text("[1, 2")
    r = ledger_guard.check(False)
    assert r["halted"] and not r["block_all"]


def test_other_mode_file_does_not_halt(env):
    from core import ledger_guard
    (env / "state.json").write_text("broken")                           # 실거래 장부 손상
    assert not ledger_guard.check(True)["halted"]                         # 모의 매매는 계속


def test_run_cycle_places_no_orders_on_corrupt_paper_ledger(env, monkeypatch):
    from core import cycle
    import core.runner as runner
    monkeypatch.setattr(runner, "alert", lambda m: None)
    (env / "state_paper.json").write_text("{bad")
    res = cycle._run_cycle({"paper": True, "strategy": "composite"}, log=lambda m: None)
    assert res["halted"] and res["bought"] == [] and res["sold"] == []

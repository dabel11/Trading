# 매매 사이클 · 멱등성 · 안전장치 감사 (2026-10-18)

범위: 전략(점수)은 제외. **주문이 실제로 돌고, 한 번만 실행되고, 사고가 나도 손실이 묶이는가**만 본다.
근거: 코드 리딩 + 격리 프로브 [`probe_safety.py`](probe_safety.py) (임시 디렉터리, 네트워크 0, 가짜 Alpaca 클라이언트 사용, 실장부 미접촉) → **13/13 재현**.

```bash
"$HOME/Library/Application Support/AITrading/venv/bin/python" docs/safety-audit/probe_safety.py
```

---

## 0. 결론

1. **지금 실제로 매매하는 주체는 `autotrader.py`가 아니라 앱 프로세스 안의 `_PoCDaemon` 루프**다(`autotrader.pid` = `ui_ng.dashboard` PID). 그런데 데몬에 있던 안전장치(장 시간 게이트, 틱 손절 가드, 연속 실패 백오프·경보, 데드맨 알림)가 이 루프로 **하나도 이식되지 않았다**. 실제 모의 장부의 주문 13건은 **전부 정규장 밖**에서 체결됐고, 토요일 05:35 ET에 체결된 것도 있다.
2. 모의 장부의 정산 항등식(시드 − 매수 + 매도 = 현금)은 **현재는 맞는다**($1,308). 다만 **이중 매도가 나면 가짜 현금이 생기는** 경로가 있어서, 경합이 한 번만 일어나도 모의 수익률(실거래 전환을 판단할 근거)이 부풀려진다.
3. 실거래 경로(`broker.py` + `execute_*`)는 **멱등성이 없다**. 주문 의도를 저장하지 않고, 브로커와 장부를 대조하지 않으며, 부분 체결이 나면 장부가 브로커와 어긋난다. Alpaca 키가 없어서 아직 피해는 0이지만, **실거래 전에 반드시 막아야 할 결함**이다.
4. 시간 기준이 섞여 있다. 하루 경계를 **KST 자정**으로 잡는데, 이 시각은 미국 장중(ET 10–11시)이다. 그래서 킬스위치가 장중에 풀리고, daily 모드는 하루에 두 번 돈다.

---

## 1. 현재 사이클 구조

```
[ui_ng.dashboard 프로세스]  ← 실제 실행 주체 (PID 8471)
  _PoCDaemon._loop  (interval, 최소 30s, 24/7 — 장 시간 체크 없음)
    └ run_cycle(cfg)                         core/cycle.py
        파라미터 주입(portfolio 모듈 전역 변경) → 발굴 → 채점 → 실시간가 오버레이
        → generate_orders (락 밖, 스냅샷)
        → 킬스위치/약세장 필터
        → execute_orders  (trade_lock: RLock + flock)
             매도: [실거래] broker.execute_order → validate_fill → record_sell → [모의] cash+=
             매수: 자금게이트 → … → record_buy → [모의] cash-=
             각 체결 → orders_log.json + 알림
  같은 프로세스: 텔레그램 봇(/buy /sell /confirm /mode), 백테스트·최적화(io_bound 스레드)

[autotrader.py]  ← 현재 미사용. register_pid 가 앱 PID를 보고 기동을 거부함
  장시간 게이트 · guard(틱 손절) · fail_streak 백오프/경보 · SIGTERM 데드맨
```

장부 파일 4개(`state_paper.json`, `paper_account.json`, `trades_paper.json`, `orders_log.json`)는 **각각 따로 원자적으로 쓰인다**. 하지만 파일들 사이의 트랜잭션은 없다.

---

## 2. 발견 사항

심각도는 **현재(모의) / 실거래 전환 시** 두 가지로 매겼다.

| # | 영역 | 결함 | 재현 결과 | 위치 | 심각도 |
|---|---|---|---|---|---|
| 1 | 멱등성 | **이중 매도 → 유령 현금.** 매도 루프가 락 안에서 장부를 다시 읽은 뒤에도 "이미 팔린 종목"인지 확인하지 않는다. `record_sell`은 아무 일도 안 하는데 `cash += qty*price`는 그대로 실행된다. 틱 가드·수동 매도·텔레그램 매도가 사이클과 겹치면 발생한다. 실거래에서는 보유 0주 상태의 매도라서 **공매도로 처리된다**(Alpaca는 포지션 0에서 낸 매도를 숏으로 간주). | 현금 10,840 (정상 9,920), 매도 로그 2건 | [execution.py:113-142](../../core/execution.py) | 중 / **치명** |
| 2 | 멱등성 | **매도 수량을 보유 수량에 맞추지 않음.** 사용자가 6주를 판 뒤 사이클이 10주 전량 매도를 내면 4주만 차감하면서 현금은 10주어치가 들어온다. | 현금 10,600 (정상 10,000) | 위와 동일 | 중 / 높음 |
| 3 | 실거래 체결 | **부분 체결 후 재시도 때 전량을 다시 주문함.** 30주가 체결된 1차 주문을 취소하고, 다시 50주를 주문한다. `res`를 덮어써서 1차 체결분은 장부에서 빠진다. | 브로커 80주 / 장부 50주 / 의도 50주 | [broker.py:219-255](../../broker.py) | – / **치명** |
| 4 | 실거래 체결 | **`canceled` 상태로 부분 체결된 주문이 기록되지 않음.** `validate_fill`이 filled/partially_filled 상태만 인정한다. | 브로커 20주, 장부 0주 | [broker.py:44-49](../../broker.py) | – / 높음 |
| 5 | 실거래 안전 | **급락하면 손절 매도를 포기함.** 지정가 ±0.5%에 미체결이고, 재조회 가격이 2% 넘게 움직였으면 "추격 포기"한다. 매수에는 맞는 로직이지만, **손절은 바로 가격이 급변할 때 나가야 한다**. | `status=new`, "추격 포기" | [broker.py:244-251](../../broker.py) | – / **치명** |
| 6 | 실거래 멱등성 | **수동·텔레그램 실거래가 시장가 미체결이면 예외만 내고 주문을 취소하지 않음.** 주문은 브로커에 남아 있다가 나중에 체결되고, 장부는 그 사실을 모른다. 그러면 다음 사이클이 같은 종목을 또 산다. 메시지의 "다음 사이클에서 재확인"을 처리하는 코드도 **없다**. | 취소 호출 0회 | [execution.py:215-221](../../core/execution.py), [broker.py:194-198](../../broker.py) | – / 높음 |
| 7 | 킬스위치 | **모의 모드 일일 손실 한도가 평가손실을 보지 못함.** 자산을 `현금 + 원가`로 계산하기 때문이다. | 실제 −15%인데 halted=False | [cycle.py:119-120](../../core/cycle.py) | 높음 / – (실거래는 브로커 equity 사용) |
| 8 | 시간 | **daily 모드가 하루 2회 실행됨(서머타임 기간).** `last_daily`를 KST 날짜로 기록하는데 KST 자정이 ET 11:00이라서, 같은 ET 날짜에 다시 "오늘 아직 안 돌았다"고 판단한다. `last_daily`는 메모리에만 있어서 재시작해도 다시 돈다. | 7/15 10:05 ET, 11:05 ET 두 번 실행 | [autotrader.py:54-64](../../autotrader.py) | 낮음(현재 interval 모드) / 중 |
| 9 | 시간 | **킬스위치 하루 경계가 KST 자정.** ET 10–11시에 `halted`가 풀리고 기준 자산이 다시 잡힌다. 장중에 한도에 걸려도 1시간 뒤에 매수가 재개된다. 진입일·보유일·재진입 쿨다운도 모두 로컬 날짜를 쓴다. | 코드 리딩 | [risk_guard.py:43](../../risk_guard.py), [portfolio.py:73](../../portfolio.py) | 높음 / 높음 |
| 10 | 격리 | **백테스트가 라이브 상태를 오염시킴.** 백테스트(특히 '최고 수익 전략 찾기')가 앱 프로세스 안에서 `portfolio._CLOCK`, `CAPITAL_TOTAL`, `MAX_POSITIONS`, `MAX_POSITION_PCT` 같은 **모듈 전역을 바꾼다**. 그동안 자동매매 사이클이 돌면 진입일이 과거 날짜로 찍혀 다음 사이클에 `max_hold`로 즉시 매도되고, 실거래에서는 사이징이 백테스트 자본 기준으로 계산된다. `run_cycle`은 MAX_POSITIONS/PCT/CAPITAL을 다시 넣지 않는다. | entry_date=2023-01-03 → `max_hold (1384d)` | [backtester.py:818-844](../../backtester.py), [cycle.py:40-48](../../core/cycle.py) | 높음 / **치명** |
| 11 | 설정 | **설정 파일이 손상된 순간에 읽으면 기본값으로 덮어씀.** `save_config`는 원자적 쓰기가 아니라서(truncate→write) 그 사이에 읽을 수 있다. 그러면 `load_config`가 실패하고 **DEFAULT_CONFIG를 파일에 쓴다**. 결과는 enabled=True, paper=True, 손절 7%, 전략 ai_influencer다. 사용자가 끈 매매가 다시 켜지고, 실거래 중이면 모의로 바뀌어 **실보유 종목이 관리되지 않는다**. UI 타이머·가드·봇이 이 파일을 자주 읽어서 경합 창이 계속 열려 있다. | 디스크가 기본값으로 바뀜 | [control.py:29-42](../../core/control.py) | 중 / 높음 |
| 12 | 시간 | **휴장일·조기폐장을 모름.** 주말만 거른다. 추수감사절이나 블랙프라이데이 14:30을 개장으로 판단한다. 게다가 실제 실행 루프(`_PoCDaemon`)는 이 함수조차 **호출하지 않는다**. | Thanksgiving open=True | [market_hours.py:26-31](../../market_hours.py), [dashboard.py:911-918](../../ui_ng/dashboard.py) | 중 / 높음 |
| 13 | 장부 | **장부 손상 시 빈 장부로 계속 진행(fail-open).** `safe_read_json`이 `.corrupt`로 백업하고 `{}`를 돌려준다. 그러면 포지션을 잊은 채 새로 매수하고, 기존 보유는 손절 관리를 받지 못한다. `paper_account.json`이 손상되면 현금이 $10,000으로 리셋된다. | positions={} | [safe_store.py:131-138](../../safe_store.py) | 중 / 높음 |

**코드 리딩으로만 확인한 것(프로브 없음)**

| # | 결함 | 위치 |
|---|---|---|
| 14 | 실행 루프에 **장 시간 게이트, 틱 손절 가드, 실패 백오프·경보, 데드맨 알림이 없다**. 오류가 나면 `last_log`만 갱신하고 끝난다. 앱이 죽거나 맥이 잠들면 아무도 모른다. | [dashboard.py:896-918](../../ui_ng/dashboard.py) |
| 15 | **가격 신선도 검사가 없다.** 실시간 피드가 죽으면 채점 번들의 일봉 종가(어제 값일 수 있음)로 조용히 대체한다. 그러면 손절 판단이 묵은 가격으로 이뤄져서 **폭락을 못 본다**. `realtime_feed`에 `ts`가 있는데도 쓰지 않는다. | [cycle.py:83-104](../../core/cycle.py) |
| 16 | `trade_lock`을 **브로커 네트워크 I/O 동안 잡고 있다**. 주문 1건이 최대 약 17초 걸리고(폴링 3+5초 × 2회 + 재조회), 그동안 수동 매도와 손절 경로가 대기한다. | [execution.py:84](../../core/execution.py) |
| 17 | client_order_id가 **분 단위 시각**으로 만들어지고 주문 의도와 연결되지 않는다. 분이 바뀌면 같은 의도의 재전송을 막지 못한다. 재시작 뒤에도 `get_order_by_client_id`로 조회하지 않는다. | [broker.py:121-126](../../broker.py) |
| 18 | **사전 리스크 한도가 없다**: 주문당 최대 금액, 사이클·일 최대 주문 수, 일 회전율, 가격 이상치(기준가 대비 ±X%). 과거 핑퐁 과잉매매가 바로 이 부재에서 생겼다. | execution 깔때기 |
| 19 | 실거래 가용자금이 브로커 buying_power가 아니라 **상수 `CAPITAL_TOTAL − 원가`**다. | [portfolio.py:185](../../portfolio.py) |
| 20 | 모의 체결이 `record_buy`(장부), `adjust`(현금), `log_order`(로그) **세 파일에 따로** 쓰인다. 중간에 크래시가 나거나 예외가 발생하면 서로 어긋난다. | [execution.py:174-179](../../core/execution.py) |
| 21 | 텔레그램 `/실거래`는 **명령 한 번으로 즉시 전환**된다. 2단계 확인이 없고, 자동매매가 도는 중이나 보유 종목이 있어도 허용된다. | [telegram_bot.py:256](../../telegram_bot.py) |

---

## 3. 권장 전략

### A. "사이클이 실제로 돈다"를 보장하기 (liveness)

| 원칙 | 구체안 |
|---|---|
| **실행 주체는 하나, 안전장치도 한 곳** | `_PoCDaemon._loop`와 `autotrader.main`의 루프 본문을 `core/runner.py`(장시간 게이트 → run_cycle → 실패 백오프·경보 → 하트비트)로 합치고, 두 곳 모두 이 함수를 부르게 한다. 지금은 "데몬에만 있는 안전장치"가 생기는 구조다. |
| **시장 시계는 브로커에 묻기** | Alpaca `get_clock()`(is_open, next_open, next_close)과 `get_calendar()`를 쓴다(alpaca-py 0.43에 있음). 키가 없는 모의 모드에서는 `exchange_calendars`(XNYS)를 쓴다. 휴장일과 13:00 조기폐장을 자동으로 처리한다. |
| **거래일 = ET 거래일** | `trading_day()` 단일 함수(ET 기준 세션 날짜)를 만들고 `date.today()`를 모두 대체한다. 대상: risk_guard, daily 모드, entry_date, exit_date, 쿨다운. 한국에서 돌리는 이 앱에서는 필수다. |
| **하트비트 + 외부 감시** | 사이클마다 `heartbeat.json`(ts, cycle_id, 결과)을 기록하고, **맥 밖의** 감시 서비스(healthchecks.io 같은 데드맨 cron, 무료)에 ping을 보낸다. N분 동안 ping이 없으면 텔레그램/메일 경보가 간다. 앱 안의 감시는 앱이 죽으면 같이 죽는다. |
| **잠자기 방지 + 자동 재기동** | `caffeinate -i`(또는 `pmset`)로 잠자기를 막고, launchd `KeepAlive`로 프로세스를 감독한다. `_resume_auto`는 이미 있으니 크래시 루프 백오프만 추가한다. |
| **무진행 감지** | "사이클은 성공했는데 가격 신선도 0%" 같은 **조용한 고장**은 경보로 처리한다. |

### B. 멱등성 — "같은 의도는 한 번만, 결과는 정확히 한 번 반영"

핵심은 장부를 '결과'가 아니라 **이벤트 저널**로 만드는 것이다.

1. **의도 먼저 기록 (write-ahead)**
   `intent_id = f"{trading_day}-{cycle_id}-{ticker}-{side}"`를 만들고, 주문 전에 `intents` 테이블에 `PENDING`으로 저장한다. client_order_id는 이 intent_id에서 **결정적으로** 만든다(시각 기반 X). 재시도는 `-r1`을 붙이되 같은 intent에 묶는다.
2. **주문 상태 머신**
   `PENDING → SUBMITTED → PARTIAL → FILLED | CANCELED | REJECTED | EXPIRED`. 최종 상태에 도달하기 전에는 같은 ticker·side의 새 intent를 만들지 않는다. 이것이 중복 매수, 이중 매도, 부분 체결 재주문(#1, #2, #3)을 구조적으로 막는다.
3. **체결은 이벤트 단위로, 중복 제거 키는 execution_id**
   Alpaca `TradingStream`의 `trade_updates`(fill/partial_fill)를 받는다. `TradeUpdate`에 `execution_id`, `qty`, `price`, `position_qty`가 있다. 각 체결을 `fills(execution_id UNIQUE)`에 INSERT하고 포지션은 거기서 파생한다. 같은 이벤트가 두 번 와도 한 번만 반영된다. 폴링(`_poll_fill`)은 스트림 장애 시 폴백으로만 쓴다.
4. **취소 후에는 최종 상태를 다시 읽기**
   `cancel` 뒤에 `get_order_by_id`로 최종 `filled_qty`를 확인하고 그 값만큼 반영한다(#4). 재시도 수량은 `요청 − 누적 체결`이다(#3).
5. **사이클 시작 = 대조(reconcile)**
   실거래에서는 **브로커가 진실의 원천**이다. 사이클 첫 단계에서:
   - `get_all_positions()`와 로컬 장부를 비교한다. 로컬에는 메타데이터(진입일, 고점, 진입점수)만 둔다.
   - `get_orders(status=open)`을 조회해, 열린 주문이 있는 종목은 이번 사이클 결정에서 제외한다.
   - `PENDING`/`SUBMITTED` intent는 `get_order_by_client_id`로 상태를 확정한다. 크래시 복구도 이 단계에서 이뤄진다(#6, #17).
   - 불일치가 허용치를 넘으면 **매매 중단 + 경보**. 조용히 덮어쓰지 않는다.
6. **모의 모드도 같은 저널 사용**
   모의 체결은 "가짜 브로커가 즉시 FILLED 이벤트를 낸다"로 모델링한다. 그러면 실거래와 모의가 **같은 상태 머신**을 타게 되고, 모의 테스트가 실거래 경로를 검증하게 된다.
7. **저장소: SQLite(WAL) 한 파일**
   `positions`, `cash`, `intents`, `fills`, `orders_log`를 **한 트랜잭션**으로 갱신한다(#20). 손상되거나 무결성 검사에 실패하면 **fail-closed**: 매매를 중단하고 경보를 보낸다(#13). 파일 여러 개 + flock보다 단순하고 강하다.

> 대안(작은 변경): SQLite 이전이 부담스러우면 단기적으로 #1, #2만 패치한다(락 안에서 `pos is None`이면 skip, `qty = min(qty, pos.shares)`, 현금 반영은 `record_sell`이 실제로 판 수량 기준). 다만 실거래 전에는 1~5단계가 필요하다.

### C. 안전장치 — "사고가 나도 손실이 묶인다"

| 장치 | 내용 |
|---|---|
| **사전 리스크 게이트 (단일 깔때기 안)** | `execute_*`가 모든 주문의 길목이니 여기에 `pre_trade_check()` 하나를 둔다. 검사 항목: 장 개장 여부 · 가격 신선도(≤ N초) · 기준가 대비 이상치 · 주문당 최대 금액 · 사이클/일 최대 주문 수 · 일 회전율 · 종목 최대 비중 · **매도 ≤ 브로커 보유 − 열린 매도 주문**(숏 방지) · 같은 종목 반대 방향 최소 간격. 하나라도 걸리면 거부하고 사유를 로그에 남긴다. |
| **비대칭 실행** | 위험을 **늘리는** 주문(매수)은 지정가 + 추격 금지. 위험을 **줄이는** 주문(손절·청산)은 반드시 나가야 하니 미체결이면 재가격 → 시장가로 에스컬레이션한다(#5). |
| **브로커 측 보호 주문** | 매수가 체결되면 Alpaca **stop / trailing_stop 주문**(또는 bracket/OTO)을 서버에 걸어 둔다. 그러면 앱, 맥, 인터넷이 죽어도 손절이 작동한다. 앱 측 손절은 "더 빠른 추가 레이어"가 된다. 무인 운영의 가장 큰 안전 이득이다. |
| **킬스위치 정비** | ① 시가평가(MTM) 자산 기준(#7) ② ET 거래일 경계(#9) ③ 최대 낙폭(peak 대비) 한도 ④ 연속 오류 N회 시 자동 정지 ⑤ **비상 버튼**: `cancel_orders()` + `close_all_positions(cancel_orders=True)`를 앱과 텔레그램(`/panic`, 2단계 확인)에 연결. |
| **설정 안전** | `save_config`에 `atomic_write_json`을 쓰고, `load_config` 실패 시 **last-known-good을 반환하고 절대 파일에 쓰지 않는다**(#11). 스키마와 범위를 검증한다(손절 0~50% 등). 위험 설정(paper→live, 손실한도 해제)은 2단계 확인을 거치고 **자동매매 정지 + 보유 0 상태에서만** 허용한다(#21). |
| **프로세스 격리** | 백테스트와 최적화를 `ProcessPoolExecutor` 같은 **별도 프로세스**에서 돌린다(#10). 근본적으로는 파라미터를 모듈 전역이 아니라 `Params` 객체로 넘기는 게 맞다. 그러면 `run_cycle`과 틱 가드의 전역 주입도 사라진다. |
| **락 범위 축소** | 락은 "결정 재검증 + 상태 전이 기록"에만 잡는다. 브로커 대기는 상태 머신(SUBMITTED)으로 빼서 락 밖에서 기다린다(#16). |

### D. 실거래 진입 단계

지금의 "모의"는 **로컬 시뮬레이터**라서 `broker.py` 경로를 실제 환경에서 한 번도 검증하지 않았다. 중간에 **Alpaca Paper API**(무료, 실제 주문 수명주기·부분체결·거부·큐잉을 그대로 재현)를 넣는다.

| 단계 | 환경 | 통과 조건 |
|---|---|---|
| 0 | 로컬 모의 | 위 P0 수정 후 프로브 0/13. 정산 항등식이 매 사이클 자동 검사됨 |
| 1 | **Alpaca Paper** (`Broker(paper=True)`) | 2주 이상 무인 운영, 대조 불일치 0, 미해결 intent 0, 하트비트 누락 경보 동작 확인 |
| 2 | 실거래 소액 (예: $500, 주문당 $100 상한) | 2주, 브로커 측 stop 동작 확인, 비상 버튼 리허설 |
| 3 | 단계적 증액 | 한도는 설정에서만 올리고, 올릴 때마다 2단계 확인 |

### E. 테스트 전략

- **가짜 브로커 시나리오 테스트**: 부분체결, 체결 지연, 취소 경합, 거부, 큐잉, 네트워크 예외, 중복 이벤트. `probe_safety.py`의 FakeTrading을 `test_core.py`로 옮겨 회귀 테스트로 만든다.
- **크래시 주입 테스트**: 실행기 각 단계(의도 기록 → 제출 → 체결 반영 → 현금) 사이에서 강제로 예외를 내고 재기동한다. 그 뒤 대조를 돌려 장부와 브로커가 일치하는지 확인한다.
- **불변식 검사 (매 사이클 + 테스트)**: `시드 − Σ매수 + Σ매도 = 현금`, `보유 수 ≤ MAX_POSITIONS`, `보유 수량 ≥ 0`, `열린 intent ≤ 1/종목·방향`. 위반하면 매매를 멈춘다.
- **시간 테스트**: KST 자정 전후, 서머타임 전환일, 휴장일, 조기폐장.
- **동시성 테스트**: 사이클 + 틱 가드 + 수동 매도 + 백테스트를 동시에 돌린다.

---

## 4. 우선순위 로드맵

| 우선 | 작업 | 해결 항목 | 규모 |
|---|---|---|---|
| **P0** (모의 데이터 정확성, 즉시) | 매도 루프에 `pos is None` skip + 수량 clamp + 실제 매도 수량으로 현금 반영 | #1 #2 | 소 |
| P0 | 실행 루프 통합(`core/runner.py`): 장 시간 게이트 + 실패 백오프·경보 | #12 #14 | 소~중 |
| P0 | `save_config` 원자화 + `load_config` 실패 시 쓰기 금지 | #11 | 소 |
| P0 | 백테스트를 별도 프로세스로 실행 | #10 | 소 |
| P0 | ET 거래일 단일 함수 + risk_guard·daily 모드 적용, 모의 킬스위치를 MTM으로 | #7 #8 #9 | 소 |
| **P1** (실거래 전 필수) | intent 저널 + 상태 머신 + execution_id 중복 제거 + 사이클 시작 시 대조 | #3 #4 #6 #17 #19 | 대 |
| P1 | 사전 리스크 게이트 + 가격 신선도 + 비대칭 실행 | #5 #15 #18 | 중 |
| P1 | 브로커 측 stop 주문 + 비상 버튼 + 외부 하트비트 | 무인 운영 | 중 |
| P1 | Alpaca Paper 단계 추가, 모드 전환 2단계 확인 | #21, 단계 1 | 소 |
| **P2** (강화) | SQLite 단일 장부(fail-closed), 락 범위 축소, 시장 캘린더 | #13 #16 #20 | 중~대 |

---

## 출처

- Alpaca 포럼 — 보유 0주에서 낸 매도는 공매도로 간주, 보유 초과 매도는 거부: [Simultaneous buy and sell orders](https://forum.alpaca.markets/t/simultaneous-buy-and-sell-orders/11992), [insufficient qty available for order](https://forum.alpaca.markets/t/alpaca-py-apierror-insufficient-qty-available-for-order/17712), [long → short switching](https://forum.alpaca.markets/t/switching-from-long-to-short-position-at-market-close/11395) (커뮤니티 답변이며 공식 문서 아님)
- alpaca-py 0.43.4 (로컬 venv에서 확인): `TradingClient.get_clock / get_calendar / get_order_by_client_id / cancel_orders / close_all_positions`, `TradeUpdate.execution_id / qty / price / position_qty`

---

## 5. 진행 상황 (2026-10-18)

P0 수정 완료(미커밋). 프로브 결과 **14개 중 9개 ok**. 남은 5개는 P1/P2 범위다.

| 수정 | 변경 | 프로브 |
|---|---|---|
| 1. 매도 재검증 | `execute_orders` 매도 루프가 락 안의 최신 장부로 미보유 건너뜀 + 수량 clamp. 현금은 `record_sell`이 실제 차감한 수량 기준. `execute_manual`도 장부를 다시 읽음 | #1 #2 ok |
| 2. 실행 루프 통합 | 새 `core/runner.py`(PID·enabled·장 시간·daily·실패 백오프/경보·틱 가드)를 데몬과 앱 `_PoCDaemon`이 공용으로 씀. '지금 1회 실행'은 `run_cycle_guarded`. `market_hours`에 NYSE 휴장일·13:00 조기폐장 규칙 추가(2024~2027 공표 일정과 일치) | #12 #14 ok |
| 3. 설정 안전 | (다른 세션 커밋 `9a43c03`: 원자적 쓰기 + 손상 시 덮어쓰기 금지) + 정상값 캐시가 없을 때는 `enabled=False`로 fail-closed | #11 ok |
| 4. 백테스트 격리 | 새 `core/isolated.py`(별도 파이썬 프로세스 실행). 앱 백테스트와 '최고 수익 전략 찾기'(`backtester.compare_strategies`)가 이걸 거침 | #10 ok |
| 5. ET 거래일 + MTM | `market_hours.trading_day()`를 risk_guard·portfolio `_today`·주문로그·daily 모드에 적용. daily 실행 기록은 `runner_state.json`에 영속. 킬스위치 자산은 `cycle.mark_to_market_equity`(현금 + 시가) | #7 #8 #9 ok |

남은 항목: #3 #4 #5 #6(실거래 체결·멱등성 → P1 intent 저널·대조), #13(장부 fail-open → P2 SQLite).
주의: 첫 실행 때 `day_state.json`이 KST 날짜에서 ET 날짜로 바뀌면서 킬스위치 기준 자산이 1회 다시 잡힌다.

### P1 진행 (2026-10-19)

프로브 **14개 중 0개 BUG**. 테스트: test_core · test_ev · test_influence_and_config · test_p1_mode_ledger · test_portfolio_churn · test_monitor 전부 통과.

| 수정 | 변경 | 해결 |
|---|---|---|
| 의도 저널 · 대조 · 리스크 게이트 | `core/intents.py`, `broker.execute_order`, `pre_trade_check`, `reconcile_live` (커밋 `20cf3f2`) | #3 #4 #5 #6 #15 #17 #18 #19 |
| 실거래 전환 2단계 확인 · 장부 손상 fail-closed | `core/mode.py`, `core/ledger_guard.py` — 손상이면 저널 halt(by="ledger") (커밋 `ce4a966`) | #21 #13 |
| 회전율 안전장치 | 당일 매수 회전 금지 + 같은 발행사 중복 진입 방지 (커밋 `c98a6be`) | #18 |
| 브로커 측 보호 주문 | 실거래 매수 체결 → 보유 전량에 GTC stop(평단 × (1 − `stop_loss`), 설정 `broker_stop` = stop · trailing · off). coid `ait-<매수 의도>-stop`, 저널 kind="protect". 앱 매도(자동·틱 가드·수동·텔레그램)와 추가 매수는 stop 을 **먼저 취소하고 확인된 뒤에만** 낸다 — 미확정이면 보류(stop 과 이중 매도·wash trade 방지). 취소 직전 stop 이 체결됐으면 그 체결을 장부에 반영하고 앱 매도는 생략. 매도·매수 뒤 남은 보유에 다시 건다. `reconcile_live`: stop 체결 반영, 보유마다 stop 확인·복구(재기동·제출 실패·만료·저널 유실 시 브로커 stop 되살림), 실패는 경보 1회. `pre_trade_check`의 열린 매도에서 자기 stop 은 `protect_qty`로 따로 | 무인 운영 |
| 매매 중단 해제 | `core/emergency.py` `status()`·`resume()` — 텔레그램 `/resume`(사유 표시 → `/confirm`), 앱 설정 '매매 중단 · 비상 정지' 카드. 장부 손상·비상 정지 중단은 대조가 일치해도 자동으로 풀리지 않는다(대조 불일치는 `also`에 덧붙음) | 무인 운영 |
| 비상 버튼 | `emergency.panic()` — 실거래: 자동매매 정지 + halt(by="panic") 먼저 기록 → `cancel_orders()` + `close_all_positions(cancel_orders=True)` → 청산 주문을 저널(kind="panic")에 기록 → 대조로 장부 반영(장 마감이면 개장 체결 후 다음 대조). 모의: 모의 장부 전량 현재가 청산 + 자동매매 정지. 텔레그램 `/panic` → `/confirm`, 앱 버튼(확인 대화상자) | 킬스위치 ⑤ |

남은 항목: TradingStream execution_id 중복 제거, 락 범위 축소(#16), SQLite 단일 장부(P2), Alpaca Paper 단계 무인 운영(단계 1) — 보호 stop 은 실제 Alpaca Paper 에서 한 번 확인 필요(가짜 브로커로만 검증).

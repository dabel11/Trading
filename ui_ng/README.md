# ui_ng — NiceGUI 이전본 (탈-Streamlit)

기존 Streamlit `app.py`의 view 레이어를 NiceGUI로 재구성한 것. **백엔드는 한 줄도
수정하지 않고**(portfolio / paper_account / realtime_feed / core.* / strategy_catalog /
universe) 그대로 호출만 한다.

## 설치 / 실행

```bash
pip install -r ../requirements.txt     # nicegui 포함
# 또는 최소한: pip install nicegui

# 브라우저(개발용)
python -m ui_ng.dashboard              # http://127.0.0.1:8502

# 네이티브 창(pywebview, 배포 형태)
python -m ui_ng.window
```

> 주의: 반드시 **프로젝트 루트**에서 실행해야 백엔드 모듈을 import 한다.

## 구성

| 파일 | 역할 |
|---|---|
| `dashboard.py` | 앱 본체 — 7페이지(대시보드·전략 선택·자동 트레이딩·포트폴리오·주식 열람·직접 주문·설정) + 테마 + 라우팅 + 상태 격리 + PoC 데몬 |
| `window.py` | pywebview 네이티브 창 래퍼(= `native_app.py`의 NiceGUI 버전). 서버를 띄우고 창에 표시 |
| `native.py` | NiceGUI 내장 `native=True` 런처(대안) |
| `_poc_state/` | **격리된 PoC 전용 상태**(config·현금·장부·주문·pid·lock) — 실가동 앱과 분리 |

## 상태 격리 (중요)

쓰기 액션(주문·설정저장·전략변경·자동매매)은 모두 **활성화**돼 있지만, 모든 상태
파일을 `_poc_state/`로 리다이렉트해 **실가동 앱과 완전히 분리**된다. 백엔드 경로
상수를 import 직후 몽키패치하는 방식이라(모듈 싱글톤), 인프로세스 호출 전부가
격리 디렉터리에만 기록한다. 자동매매 데몬도 외부 프로세스(autotrader.py)가 아니라
**인프로세스 run_cycle 루프**라 좀비·중복 데몬이 생기지 않는다.

초기화하려면 `_poc_state/` 디렉터리를 지우면 된다(현금 $10,000, 보유 0으로 재시드).

## Streamlit 대비 개선점

- 전체 페이지 rerun 깜빡임 제거 — `@ui.refreshable` + `ui.timer`로 필요한 블록만 패치
- 네트워크(위키 유니버스) 로드를 `run.io_bound`로 백그라운드 처리 → 이벤트 루프 블로킹 없음
- WebSocket 메시지 한도 상향(1MB→32MB)
- 프로 터미널 다크 테마(Pretendard, 상승 빨강/하락 파랑) 이식, 풀폭 2단 레이아웃

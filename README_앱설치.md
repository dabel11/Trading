# AI 트레이딩 — macOS 앱

## 실행 방법

### 방법 1: 응용 프로그램에서 실행 (이미 설치됨)
- **Launchpad** 또는 **응용 프로그램 폴더** → `AI 트레이딩` 클릭
- 처음 실행 시 패키지 자동 설치 (1~2분), 이후엔 즉시 실행
- 자동으로 기본 브라우저에 대시보드가 열립니다

### 방법 2: DMG로 배포 (다른 Mac에 설치)
1. `AI트레이딩.dmg` 더블클릭
2. `AI 트레이딩` 아이콘을 `Applications` 폴더로 드래그
3. Launchpad에서 실행

## 처음 실행 시 "확인되지 않은 개발자" 경고가 뜨면
자체 빌드라 서명이 없어 macOS가 차단할 수 있습니다:
- **우클릭 → 열기** → 경고창에서 **열기** 클릭 (최초 1회만)
- 또는 터미널: `xattr -cr "/Applications/AI 트레이딩.app"`

## 요구사항
- macOS 10.13+
- Python 3 (python.org 또는 Homebrew)
  - 없으면 https://python.org 에서 설치

## 종료
- 브라우저 탭을 닫아도 서버는 백그라운드에서 유지됩니다
- 완전 종료: 터미널에서 `pkill -f "streamlit run"`

## 로그 위치
- `~/Library/Logs/AITrading.log`

## 구조
```
AI 트레이딩.app/
├── Contents/
│   ├── Info.plist          앱 메타데이터
│   ├── MacOS/AITrading     진입점
│   └── Resources/
│       ├── icon.icns       앱 아이콘
│       └── trade/          전체 소스 + launcher.sh
```

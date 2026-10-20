# 서버 운영 — 맥을 꺼도 매매가 도는 구성

맥 앱이 하던 일(자동매매 루프 · 텔레그램 봇 · 감시/리포트 · 외부 감시)을 리눅스 서버의
Docker 컨테이너 2개로 옮긴다. 코드는 맥 앱과 **같은 것**이다. 서버에선 창 없이
`python -m ui_ng.dashboard` 만 돌린다.

```
 맥 (개발·화면)                         서버 (Ubuntu, 24시간)
 ─────────────                          ─────────────────────────────────
 ~/trade  ── deploy.sh (커밋된 코드) ──▶ /opt/trade  (코드 + 장부 *.json)
          ── cutover.sh (장부·키, 1회) ─▶     │ 마운트
 AI 트레이딩.app ── Tailscale ─────────▶ app      : 매매 루프·텔레그램·감시 (127.0.0.1:8502)
   (.remote_url 이 있으면 서버 화면만 연다)   watchdog : 5분마다 외부 점검 → 텔레그램 경보
 휴대폰 ── 텔레그램 / Tailscale ───────▶
```

## 지켜야 할 것 3가지

1. **매매는 한 곳에서만.** 맥과 서버가 동시에 돌면 장부가 갈라지고 텔레그램 봇이 서로
   충돌한다(Alpaca 연결 후엔 이중 주문). 옮길 땐 반드시 `cutover.sh` 를 쓴다. 이 스크립트가
   한쪽을 끄고 다른 쪽을 켠다.
2. **8502 포트를 인터넷에 열지 않는다.** UI 에 로그인이 없다. 열려 있으면 누구나 주문할 수
   있다. compose 는 서버 자신(127.0.0.1)에만 열고, 접속은 Tailscale 이나 SSH 터널로만 한다.
3. **장부·키는 git 으로 다니지 않는다.** `deploy.sh` 는 커밋된 코드만 보낸다. 장부(`*.json`,
   `*.jsonl`)와 키(`.env`, `notify_config.json`)는 `cutover.sh` 만 옮기고, 옮기기 전에 양쪽에
   `backups/cutover-…/` 사본을 남긴다.

## 1. 서버 고르기 (사용자 몫)

- 사양: **메모리 2GB 이상**(앱만 ~0.5GB, 여유 필요), Ubuntu 24.04 LTS, 디스크 20GB+
- 지역: 서울이면 화면 반응이 빠르다. 매매 자체는 지역과 무관하다(분 단위 사이클).
- 후보(가격은 대략):
  - **AWS Lightsail / Vultr 서울 2GB** — 월 $10~12. 한국 카드 결제·한글 자료가 많아 무난하다.
  - **Hetzner 4GB** — 월 €5 안팎으로 가장 싸다. 유럽·미국 리전만 있고 가입 심사가 있다.
  - Oracle Cloud 무료(ARM) — 공짜지만, CPU 를 적게 쓰는 무료 인스턴스는 회수될 수 있다.
    매매 서버로는 권하지 않는다.
- 만들 때 **SSH 키 로그인**을 선택하고 맥의 공개키를 등록한다. 키가 없으면 맥에서 만든다:
  ```bash
  ssh-keygen -t ed25519 -C trade-server
  ```
  `~/.ssh/id_ed25519.pub` 내용을 서버 생성 화면에 붙여 넣는다.
- 맥 `~/.ssh/config` 에 별칭을 두면 이후 명령이 짧아진다:
  ```
  Host trade
    HostName <서버 IP>
    User ubuntu
  ```

## 2. 설치 (처음 1회)

```bash
# ① 서버 준비: 시간대 KST·방화벽(SSH만)·스왑·Docker (+ Tailscale)
ssh trade 'bash -s -- --tailscale' < deploy/server-setup.sh
# docker 그룹 반영을 위해 한 번 재접속
ssh trade 'sudo tailscale up'           # 표시되는 링크로 본인 Tailscale 계정 승인
ssh trade 'sudo tailscale serve --bg 8502'   # → https://<서버이름>.<테일넷>.ts.net (테일넷 안에서만)

# ② 코드 배포 + 기동. 장부가 비어 있어 매매·봇은 아직 꺼진 상태다.
deploy/deploy.sh trade

# ③ 이전 — 미국 장 마감 후나 주말에. 맥 앱을 끄고 장부·키를 서버로 옮긴 뒤 서버를 켠다.
deploy/cutover.sh to-server trade --url https://<서버이름>.<테일넷>.ts.net
```

맥과 휴대폰에도 Tailscale 앱을 설치하고 같은 계정으로 로그인해야 위 주소가 열린다.
Tailscale 없이 쓰려면 SSH 터널을 쓴다: `ssh -N -L 8502:127.0.0.1:8502 trade` 를 켜 둔 채
`http://127.0.0.1:8502` 를 연다.

## 3. 일상 운영

| 할 일 | 명령 |
|---|---|
| 코드 배포(커밋 후) | `deploy/deploy.sh trade` (정규장 중엔 거부, 꼭 필요하면 `--force`) |
| 상태 | `ssh trade 'cd /opt/trade/deploy && docker compose ps'` |
| 앱 로그 | `ssh trade 'cd /opt/trade/deploy && docker compose logs --tail 100 -f app'` |
| 외부 감시 로그 | `ssh trade 'cd /opt/trade/deploy && docker compose logs --tail 20 watchdog'` |
| 재시작 | `ssh trade 'cd /opt/trade/deploy && docker compose restart app'` |
| 배포된 커밋 | `ssh trade cat /opt/trade/DEPLOYED` |
| 의존성 버전 올리기 | `deploy/requirements.lock` 수정·커밋 → `deploy.sh` (이미지 자동 재빌드) |

- 앱이 죽으면 Docker 가 바로 다시 띄운다(`restart: unless-stopped`). 서버가 재부팅돼도 자동 기동된다.
- 텔레그램 `/report`·`/panic` 등은 그대로 쓴다. 이제 봇은 서버에서 돈다.
- 서버 자체가 꺼지면 watchdog 도 같이 꺼져 경보가 없다. 일일 리포트가 안 오면 서버를 의심한다.

## 4. 되돌리기 (서버 → 맥)

```bash
deploy/cutover.sh to-mac trade
```
서버를 멈추고 서버 장부를 맥으로 가져온다. `.remote_url` 을 지우고 맥 외부 감시(launchd)를
다시 설치한다. 그다음 맥 앱을 열면 하던 그대로 이어서 돈다.

어느 쪽이 매매 중인지는 표식 두 개로 판단한다. 서버에 `/opt/trade/ACTIVE` 가 있으면 서버가
매매 중이고, 맥에 `~/trade/.remote_url` 이 있으면 맥은 원격 모드다. `cutover.sh` 만 이 표식을
바꾼다. 서버가 대기 상태(ACTIVE 없음)면 `deploy.sh` 는 코드만 갱신하고 앱을 켜지 않는다.
켜면 서버의 옛 장부로 맥과 동시에 매매하기 때문이다. 대기 중인 서버는 재부팅돼도 켜지지 않는다.

## 파일

| 파일 | 역할 |
|---|---|
| `Dockerfile` | python:3.14-slim + 고정 의존성. 코드는 넣지 않는다(마운트) |
| `compose.yaml` | app + watchdog, 127.0.0.1 전용 포트, 로그 회전, 자동 재시작 |
| `requirements.lock` | 맥 실가동 venv 와 같은 버전 |
| `server-setup.sh` | 서버 1회 준비 |
| `deploy.sh` | 커밋된 코드 배포 + 재시작 + 기동 확인 |
| `cutover.sh` | 실행 주체 이전(to-server / to-mac) |

#!/usr/bin/env bash
# 실행 주체 이전 — 맥에서 실행. 매매는 언제나 한 곳에서만 돌아야 한다(이중 매매·봇 충돌 방지).
#
#   deploy/cutover.sh to-server <ssh-host> [--url https://…ts.net] [--force] [--yes]
#       맥 앱 종료 → 맥 장부·키를 서버로 복사 → 서버 기동 → 맥은 '원격 모드'로 전환
#       (--url 을 주면 맥 앱 아이콘이 그 서버 화면을 연다)
#   deploy/cutover.sh to-mac <ssh-host> [--force] [--yes]
#       서버 정지 → 서버 장부를 맥으로 복사 → 맥 원격 모드 해제 (되돌리기)
#
# 장부 = 프로젝트 루트의 *.json, *.jsonl, .env (전부 .gitignore 대상, 코드 파일 아님).
# 양쪽 모두 덮어쓰기 전에 backups/cutover-<시각>-<방향>/ 에 사본을 남긴다.
# --force : 미국 정규장 중에도 진행   --yes : 확인 질문 생략
set -euo pipefail

MODE="${1:-}" HOST="${2:-}"
[ -n "$MODE" ] && [ -n "$HOST" ] && shift 2 || { sed -n '2,13p' "$0"; exit 2; }
URL="" FORCE=0 YES=0
while [ $# -gt 0 ]; do
  case "$1" in
    --url) URL="${2:-}"; shift ;;
    --force) FORCE=1 ;;
    --yes) YES=1 ;;
    *) echo "알 수 없는 옵션: $1" >&2; exit 2 ;;
  esac
  shift
done

LOCAL="$(cd "$(dirname "$0")/.." && pwd)"
# 워크트리에서 돌려도 장부는 실가동 폴더(~/trade)의 것이어야 한다.
[ -f "$LOCAL/autotrader_config.json" ] || LOCAL="$HOME/trade"
DIR="${TRADE_REMOTE_DIR:-/opt/trade}"   # 서버 앱 폴더 (server-setup.sh 와 같아야 함)
TS="$(date +%Y%m%d-%H%M%S)"
PY="$HOME/Library/Application Support/AITrading/venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3)"

ET="$(TZ=America/New_York date +%u%H%M)"
DOW="${ET:0:1}" HM="${ET:1}"
if [ "$FORCE" = 0 ] && [ "$DOW" -le 5 ] && [ "$HM" -ge 0930 ] && [ "$HM" -lt 1600 ]; then
  echo "미국 정규장 중입니다(ET $(TZ=America/New_York date +%H:%M)). 장 마감 후 하거나 --force" >&2
  exit 1
fi

ssh "$HOST" "test -f $DIR/DEPLOYED" \
  || { echo "서버에 코드가 없습니다. 먼저 deploy/deploy.sh $HOST" >&2; exit 1; }

confirm() {
  [ "$YES" = 1 ] && return 0
  printf '%s [y/N] ' "$1"; read -r ans; [ "$ans" = "y" ] || [ "$ans" = "Y" ] || { echo "중단"; exit 1; }
}

# 맥 장부 목록 — 루트의 무시된 *.json/*.jsonl + .env (추적 파일은 절대 포함하지 않음)
local_state() {
  ( cd "$LOCAL"
    for f in .env *.json *.jsonl; do
      [ -f "$f" ] || continue
      git ls-files --error-unmatch -- "$f" >/dev/null 2>&1 && continue
      echo "$f"
    done )
}

remote_compose() { ssh "$HOST" "cd $DIR/deploy && docker compose $*"; }

stop_mac_app() {
  echo "== 맥 앱 종료"
  pkill -TERM -f "ui_ng.window" 2>/dev/null || true
  pkill -TERM -f "ui_ng.dashboard" 2>/dev/null || true
  pkill -TERM -f "autotrader.py" 2>/dev/null || true
  for _ in $(seq 1 20); do
    pgrep -f "ui_ng.dashboard|ui_ng.window|autotrader.py" >/dev/null || break
    sleep 0.5
  done
  if pgrep -f "ui_ng.dashboard|ui_ng.window|autotrader.py" >/dev/null; then
    echo "   SIGTERM 무시 → 강제 종료"
    pkill -KILL -f "ui_ng.dashboard|ui_ng.window|autotrader.py" 2>/dev/null || true
    sleep 1
  fi
}

set_local_cfg() {   # 맥 쪽 설정의 자동매매 스위치를 끄거나 되돌림 (원격 모드 동안 맥 감시가 '꺼짐' 경보를 내지 않게)
  "$PY" - "$LOCAL/autotrader_config.json" "$1" <<'EOF'
import json, os, sys, tempfile
path, on = sys.argv[1], sys.argv[2] == "on"
try:
    cfg = json.load(open(path))
except FileNotFoundError:
    sys.exit(0)
cfg["enabled"] = on
if not on:
    cfg["auto_running"] = False
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
with os.fdopen(fd, "w") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.chmod(tmp, 0o600)
os.replace(tmp, path)
EOF
}

case "$MODE" in
to-server)
  # 맥 앱(~/trade/ui_ng/window.py)이 원격 모드를 모르면 .remote_url 을 무시하고 로컬 서버를
  # 띄운다 → 서버와 동시에 텔레그램 봇이 돈다. 이 코드가 ~/trade 에 병합된 뒤에만 진행.
  grep -q "REMOTE_FILE" "$LOCAL/ui_ng/window.py" \
    || { echo "$LOCAL/ui_ng/window.py 에 원격 모드가 없습니다. 서버 배포 브랜치를 먼저 병합하세요." >&2; exit 1; }
  FILES="$(local_state)"
  echo "맥($LOCAL) → 서버($HOST:$DIR) 로 매매를 옮깁니다. 복사할 장부:"
  echo "$FILES" | sed 's/^/   /'
  confirm "맥 앱을 종료하고 진행할까요?"

  echo "== 서버 정지 + 서버 쪽 기존 장부 백업"
  remote_compose stop >/dev/null 2>&1 || true
  ssh "$HOST" "mkdir -p $DIR/backups/cutover-$TS-to-server && cd $DIR && for f in .env *.json *.jsonl; do [ -f \"\$f\" ] && cp -p \"\$f\" backups/cutover-$TS-to-server/; done; true"

  stop_mac_app
  mkdir -p "$LOCAL/backups/cutover-$TS-to-server"
  (cd "$LOCAL" && echo "$FILES" | xargs -I{} cp -p {} "backups/cutover-$TS-to-server/")

  echo "== 장부 복사"
  (cd "$LOCAL" && echo "$FILES" | rsync -a --files-from=- ./ "$HOST:$DIR/")

  echo "== 서버 기동"
  ssh "$HOST" "touch $DIR/ACTIVE"      # 실행 주체 표식 — deploy.sh 가 이게 있을 때만 재시작한다
  remote_compose up -d 2>&1 | tail -3
  ssh "$HOST" 'for i in $(seq 1 30); do curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:${TRADE_PORT:-8502}/ 2>/dev/null && { echo "UI 응답 OK"; exit 0; }; sleep 2; done; echo "UI 응답 없음 — docker compose logs app 확인" >&2; exit 1'

  echo "== 맥을 원격 모드로"
  set_local_cfg off
  "$PY" "$LOCAL/watchdog.py" --uninstall >/dev/null 2>&1 || true
  if [ -n "$URL" ]; then
    printf '%s\n' "$URL" > "$LOCAL/.remote_url"
    echo "   맥 앱 아이콘 → $URL"
  else
    printf '# 서버 화면 주소를 이 줄 아래에 적으면 맥 앱이 그 주소를 연다\n' > "$LOCAL/.remote_url"
    echo "   ~/trade/.remote_url 에 서버 주소를 적으면 맥 앱이 서버 화면을 엽니다 (파일이 있는 한 맥은 매매·봇을 띄우지 않음)"
  fi
  echo "완료 — 이제 매매·텔레그램 봇·감시는 서버에서 돕니다. 맥은 꺼도 됩니다."
  ;;

to-mac)
  echo "서버($HOST) → 맥($LOCAL) 으로 매매를 되돌립니다."
  confirm "서버 앱을 정지하고 진행할까요?"

  echo "== 서버 정지"
  ssh "$HOST" "rm -f $DIR/ACTIVE"
  remote_compose stop 2>&1 | tail -2

  echo "== 맥 쪽 기존 장부 백업"
  mkdir -p "$LOCAL/backups/cutover-$TS-to-mac"
  (cd "$LOCAL" && local_state | xargs -I{} cp -p {} "backups/cutover-$TS-to-mac/")

  echo "== 장부 복사"
  ssh "$HOST" "cd $DIR && ls -A | grep -E '^(\.env|[^/]+\.jsonl?)$'" \
    | rsync -a --files-from=- "$HOST:$DIR/" "$LOCAL/"

  rm -f "$LOCAL/.remote_url"
  "$PY" "$LOCAL/watchdog.py" --install >/dev/null 2>&1 || echo "   (맥 외부 감시 재설치 실패 — python3 watchdog.py --install)"
  echo "완료 — 맥에서 AI 트레이딩 앱을 열면 서버에서 하던 그대로 이어서 돕니다."
  echo "서버는 정지 상태로 둡니다(장부는 서버에도 남아 있음). 다시 옮길 땐 to-server."
  ;;

*) sed -n '2,13p' "$0"; exit 2 ;;
esac

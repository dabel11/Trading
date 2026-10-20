#!/usr/bin/env bash
# 코드 배포 — 맥에서 실행. 커밋된 코드만 서버로 보내고 앱을 재시작한다.
#   deploy/deploy.sh <ssh-host> [git-ref] [--force]
#     git-ref : 배포할 커밋/브랜치 (기본 HEAD)
#     --force : 미국 정규장 중에도 재시작 (기본은 장중 거부 — 재시작 동안 사이클이 빈다)
#
# 장부·키(.env, *.json, *.jsonl)는 절대 보내지 않는다: git archive 는 추적 파일만 담고,
# 상태 파일은 전부 .gitignore 대상이다. 장부 이전은 cutover.sh 가 따로 한다.
set -euo pipefail

HOST="" REF="HEAD" FORCE=0
for a in "$@"; do
  case "$a" in
    --force) FORCE=1 ;;
    -*) echo "알 수 없는 옵션: $a" >&2; exit 2 ;;
    *) if [ -z "$HOST" ]; then HOST="$a"; else REF="$a"; fi ;;
  esac
done
[ -n "$HOST" ] || { sed -n '2,8p' "$0"; exit 2; }

REPO="$(git -C "$(dirname "$0")" rev-parse --show-toplevel)"
DIR="${TRADE_REMOTE_DIR:-/opt/trade}"   # 서버 앱 폴더 (server-setup.sh 와 같아야 함)
COMMIT="$(git -C "$REPO" rev-parse --verify "$REF^{commit}")"

# 미국 정규장(평일 09:30~16:00 ET) 중 재시작 금지 — 휴장일은 따지지 않는다(보수적).
ET="$(TZ=America/New_York date +%u%H%M)"
DOW="${ET:0:1}" HM="${ET:1}"
if [ "$FORCE" = 0 ] && [ "$DOW" -le 5 ] && [ "$HM" -ge 0930 ] && [ "$HM" -lt 1600 ]; then
  echo "미국 정규장 중입니다(ET $(TZ=America/New_York date +%H:%M)). 재시작하면 사이클이 빕니다." >&2
  echo "그래도 배포하려면 --force" >&2
  exit 1
fi

if [ "$REF" = "HEAD" ] && [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
  echo "주의: 커밋 안 된 변경은 배포되지 않습니다 (배포 대상: ${COMMIT:0:8})."
fi

echo "== ${COMMIT:0:8} → $HOST:$DIR"
git -C "$REPO" archive --format=tar "$COMMIT" | ssh "$HOST" "mkdir -p $DIR && tar -x -C $DIR"
ssh "$HOST" "printf '%s %s\n' '$COMMIT' \"\$(date '+%F %T')\" > $DIR/DEPLOYED"

# 서버가 대기 상태(cutover.sh to-mac 이후 — 매매는 맥)면 켜지 않는다. 켜면 서버의 옛 장부로
# 맥과 동시에 매매한다. ACTIVE 표식은 cutover.sh 가 관리하고, 장부가 아직 없는 첫 배포는 켜도 안전하다.
if ssh "$HOST" "test -f $DIR/autotrader_config.json && test ! -f $DIR/ACTIVE"; then
  echo "== 서버는 대기 상태(매매는 맥) — 코드·이미지만 갱신하고 앱은 켜지 않습니다"
  ssh "$HOST" "cd $DIR/deploy && docker compose build -q"
  exit 0
fi

echo "== 이미지 빌드(의존성 바뀐 경우만) + 재시작"
ssh "$HOST" "cd $DIR/deploy && docker compose up -d --build --force-recreate --quiet-pull 2>&1 | tail -5"

echo "== 기동 확인"
ssh "$HOST" 'for i in $(seq 1 30); do curl -fsS -o /dev/null --max-time 3 http://127.0.0.1:${TRADE_PORT:-8502}/ 2>/dev/null && { echo "UI 응답 OK (${i}회차)"; exit 0; }; sleep 2; done; echo "UI 응답 없음 — 로그 확인: cd /opt/trade/deploy && docker compose logs --tail 50 app" >&2; exit 1'
ssh "$HOST" "cd $DIR/deploy && docker compose ps --format 'table {{.Service}}\t{{.Status}}'"

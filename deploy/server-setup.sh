#!/usr/bin/env bash
# 새 서버(Ubuntu 22.04/24.04) 1회 준비 — 맥에서 원격 실행:
#   ssh <서버> 'bash -s' < deploy/server-setup.sh              # 기본
#   ssh <서버> 'bash -s -- --tailscale' < deploy/server-setup.sh   # Tailscale 도 설치
#
# 하는 일: 시간대 KST · 보안 업데이트 자동 · 방화벽(SSH 만 허용) · 메모리 3GB 미만이면 스왑 2GB
#          · Docker(우분투 저장소) · /opt/trade 폴더 · compose 용 uid 기록.
# 여러 번 돌려도 안전하다(이미 된 단계는 건너뜀).
set -euo pipefail

WANT_TAILSCALE=0
[ "${1:-}" = "--tailscale" ] && WANT_TAILSCALE=1

TRADE_DIR=/opt/trade
ME="$(id -un)"
say() { printf '\n== %s\n' "$*"; }

if ! grep -qiE 'ubuntu|debian' /etc/os-release; then
  echo "Ubuntu/Debian 만 지원합니다." >&2; exit 1
fi

say "시간대 Asia/Seoul"
sudo timedatectl set-timezone Asia/Seoul

say "패키지 갱신 + 보안 업데이트 자동 설치"
sudo DEBIAN_FRONTEND=noninteractive apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q \
  ca-certificates curl rsync ufw unattended-upgrades
sudo dpkg-reconfigure -f noninteractive unattended-upgrades

say "방화벽 — SSH 만 허용 (앱 포트 8502 는 열지 않는다)"
sudo ufw allow OpenSSH >/dev/null
sudo ufw --force enable >/dev/null
sudo ufw status | head -5

MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
if [ "$MEM_MB" -lt 3000 ] && ! swapon --show | grep -q .; then
  say "스왑 2GB 생성 (메모리 ${MEM_MB}MB)"
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  say "Docker 설치 (우분투 저장소)"
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io docker-compose-v2 \
    || sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io docker-compose-plugin
  sudo systemctl enable --now docker
fi
sudo usermod -aG docker "$ME"

say "앱 폴더 $TRADE_DIR"
sudo mkdir -p "$TRADE_DIR/deploy"
sudo chown -R "$ME:$ME" "$TRADE_DIR"
# compose 가 컨테이너를 이 계정 uid 로 돌리도록 기록 (장부 파일 소유권 유지)
printf 'TRADE_UID=%s\nTRADE_GID=%s\nTRADE_DIR=%s\n' "$(id -u)" "$(id -g)" "$TRADE_DIR" \
  > "$TRADE_DIR/deploy/.env"

if [ "$WANT_TAILSCALE" = 1 ] && ! command -v tailscale >/dev/null; then
  say "Tailscale 설치"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

say "완료"
echo "- docker 그룹 반영을 위해 SSH 를 한 번 끊고 다시 접속하세요."
if command -v tailscale >/dev/null; then
  echo "- Tailscale 로그인(최초 1회): sudo tailscale up   → 표시되는 링크를 브라우저로 열어 본인 계정으로 승인"
  echo "- 앱 화면 공개(테일넷 내부만): sudo tailscale serve --bg 8502"
fi

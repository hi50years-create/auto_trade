#!/usr/bin/env bash
# 클라우드 무료 VM(오라클/GCP 등, Ubuntu/Debian 계열) 최초 1회 설치 스크립트.
# 사용법: 이 저장소를 clone 한 디렉토리 안에서 실행 -> bash scripts/install.sh
# 사용자명/설치 경로를 하드코딩하지 않고 실행 시점에 자동 감지한다 (오라클은 보통 "ubuntu",
# GCP는 계정마다 리눅스 사용자명이 다르므로).
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="$(whoami)"
SERVICE_NAME="trading_bot.service"

echo "설치 대상: user=$RUN_USER, dir=$PROJECT_DIR"

echo "1) 타임존을 Asia/Seoul 로 설정합니다 (09:00~15:30 시간 필터가 KST 기준이므로 필수)"
sudo timedatectl set-timezone Asia/Seoul
timedatectl | grep "Time zone"

echo "2) 시스템 패키지 갱신 및 Python 3.10+ 설치 확인"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git sqlite3

echo "3) 가상환경 생성 및 의존성 설치"
cd "$PROJECT_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
deactivate

echo "4) .env 파일 확인"
if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "   ⚠️  .env 파일이 없습니다. config/.env.example 을 복사한 뒤 실제 API 키를 채워주세요:"
  echo "   cp $PROJECT_DIR/config/.env.example $PROJECT_DIR/.env && nano $PROJECT_DIR/.env"
  exit 1
fi

echo "5) 로그/데이터 디렉토리 생성"
mkdir -p "$PROJECT_DIR/logs" "$PROJECT_DIR/data"

echo "6) systemd 서비스 파일 생성 (실행 사용자=$RUN_USER, 경로=$PROJECT_DIR 로 채워넣음)"
sudo tee "/etc/systemd/system/$SERVICE_NAME" > /dev/null <<EOF
[Unit]
Description=5-min Morning Breakout Auto Trading Bot (v10)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$PROJECT_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PROJECT_DIR/.venv/bin/python -m src.main
Restart=always
RestartSec=10
StandardOutput=append:$PROJECT_DIR/logs/systemd.log
StandardError=append:$PROJECT_DIR/logs/systemd.log

MemoryMax=512M
TasksMax=200

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "7) 서비스 상태 확인"
sleep 2
sudo systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "설치 완료. 로그 확인: sudo journalctl -u $SERVICE_NAME -f"
echo "GitHub Actions 자동배포를 쓰려면 이 서버에서 'sudo -n systemctl restart $SERVICE_NAME' 이"
echo "비밀번호 없이 실행되도록 아래 명령으로 NOPASSWD 규칙을 추가하세요:"
echo "  echo \"$RUN_USER ALL=(ALL) NOPASSWD: /bin/systemctl restart $SERVICE_NAME, /bin/systemctl is-active $SERVICE_NAME\" | sudo tee /etc/sudoers.d/trading-bot-deploy"
echo "  sudo chmod 440 /etc/sudoers.d/trading-bot-deploy"

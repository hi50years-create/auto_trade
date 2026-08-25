#!/usr/bin/env bash
# Oracle Cloud Free Tier Ubuntu VM 최초 1회 설치 스크립트.
# 사용법: /home/ubuntu/auto_trade 에서 실행 -> bash scripts/install.sh
set -euo pipefail

PROJECT_DIR="/home/ubuntu/auto_trade"
SERVICE_NAME="trading_bot.service"

echo "1) 타임존을 Asia/Seoul 로 설정합니다 (09:00~15:30 시간 필터가 KST 기준이므로 필수)"
sudo timedatectl set-timezone Asia/Seoul
timedatectl | grep "Time zone"

echo "2) 시스템 패키지 갱신 및 Python 3.10+ 설치 확인"
sudo apt-get update -y
sudo apt-get install -y python3 python3-venv python3-pip git

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

echo "6) systemd 서비스 등록"
sudo cp "$PROJECT_DIR/scripts/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl restart "$SERVICE_NAME"

echo "7) 서비스 상태 확인"
sleep 2
sudo systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "설치 완료. 로그 확인: sudo journalctl -u $SERVICE_NAME -f"
echo "GitHub Actions 자동배포를 쓰려면 이 서버에서 'sudo -n systemctl restart $SERVICE_NAME' 이"
echo "비밀번호 없이 실행되도록 /etc/sudoers.d/ 에 ubuntu 계정 NOPASSWD 규칙을 추가하세요. (아래 README 참고)"

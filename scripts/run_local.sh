#!/usr/bin/env bash
# 로컬 Mac에서 장중(08:50~15:30) 봇을 포그라운드로 실행하는 스크립트.
# 절전모드/화면보호기로 인해 프로세스가 멈추지 않도록 caffeinate 로 감싼다.
#
# 사용법: 시장 시작 전(08:50 이전, 여유있게 08:40쯤)에 실행
#   bash scripts/run_local.sh
#
# 주의:
# - 노트북 전원 어댑터를 연결하고, 덮개(lid)를 닫지 마세요 (닫으면 화면이 꺼지며
#   caffeinate 로도 완전히 막기 어려운 절전 상태에 들어갈 수 있습니다).
# - 중지: 이 터미널 창에서 Ctrl+C, 또는 텔레그램에서 /stop_all
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "❌ .env 파일이 없습니다. config/.env.example 을 복사해 채워주세요."
  exit 1
fi

mkdir -p logs data

echo "=================================================="
echo " 5분 시초가 돌파 매매 봇 - 로컬 실행"
echo " 모드: $(grep '^TRADING_MODE=' .env | cut -d= -f2)"
echo " 중지: Ctrl+C 또는 텔레그램 /stop_all"
echo "=================================================="

# -d: 디스플레이 슬립 방지, -i: 유휴 시스템 슬립 방지, -m: 디스크 슬립 방지, -s: AC 전원 연결 시 시스템 슬립 방지
caffeinate -d -i -m -s .venv/bin/python -m src.main

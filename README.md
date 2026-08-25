# 5분 시초가 돌파 매매 자동매매 시스템 (v10)

`trading_bot_requirements-v10.md` 요구사항과 `morning_breakout_backtest-v10.py` 백테스트 로직을
실거래 가능한 형태로 구현한 프로덕션 패키지입니다. 브로커는 **한국투자증권(KIS) Open API v2**를
사용합니다 (키움 openAPI+는 Windows 전용 OCX라 무료 Ubuntu VM 배포와 호환되지 않아 제외).

## ⚠️ 실거래 투입 전 반드시 읽으세요

1. **모의투자(paper)로 먼저 검증하세요.** `.env` 의 `TRADING_MODE=paper` 로 최소 1~2주 이상
   실제 장중에 동작시켜보고, 텔레그램 알림/체결/청산이 의도대로 동작하는지 확인한 뒤에만
   `TRADING_MODE=live` 로 전환하십시오.
2. **KIS API 스펙은 예고 없이 바뀝니다.** 이 코드의 TR_ID/엔드포인트는 2026-08 시점에
   [공식 예제 저장소](https://github.com/koreainvestment/open-trading-api/tree/main/examples_llm)
   를 직접 조회하여 검증했습니다 (예: 현금주문 TR_ID 가 과거 `TTTC0802U/0801U` 에서
   `TTTC0012U/0011U` 로, 대체거래소(NXT) 도입에 따라 `EXCG_ID_DVSN_CD` 파라미터가 신규
   필수값으로 변경됨을 확인·반영했습니다). 그럼에도 아래 항목은 **불확실성이 남아있으니
   실거래 전 재검증**하십시오:
   - `src/data/screener.py` 의 등락률순위/거래량순위 API 파라미터·응답 필드명
   - `src/utils/tick.py` 의 호가단위 구간표 (거래소 개정 가능)
   - 장중 공휴일/임시휴장일 자동 판별 미구현 (`src/utils/time_utils.py` 참고)
3. **이 시스템은 실제 계좌로 실주문을 낼 수 있습니다.** 버그나 API 변경으로 인한 손실은
   전적으로 운영자 책임입니다. 초기에는 소액/모의투자로 충분히 검증하십시오.

## 프로젝트 구조

```
auto_trade/
├── .github/workflows/deploy.yml   # GitHub Actions CI/CD
├── config/.env.example            # 환경변수 템플릿
├── scripts/
│   ├── install.sh                 # Oracle VM 최초 설치 스크립트
│   └── trading_bot.service        # systemd 유닛
├── src/
│   ├── main.py                    # 오케스트레이터 (스케줄러 + 텔레그램 + 감시루프)
│   ├── config.py                  # 환경변수 로더
│   ├── broker/kis_client.py       # KIS REST + WebSocket 클라이언트
│   ├── data/screener.py           # 08:30~08:50 장전 스크리닝
│   ├── news/naver_news.py         # Naver 뉴스 검색
│   ├── ai/gemini_sentiment.py     # Gemini Free Tier 감성분석 (4.5초 쓰로틀)
│   ├── strategy/state_machine.py  # 종목별 실시간 매매 상태머신
│   ├── strategy/risk_manager.py   # 슬롯/지수/시간 필터
│   ├── notify/telegram_bot.py     # 텔레그램 알림 + 원격명령
│   └── db/                        # SQLite 스키마/DAO
└── morning_breakout_backtest-v10.py  # (원본 백테스트, 참고용 보존)
```

## 1. 로컬 준비

```bash
cd auto_trade
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config/.env.example .env   # 이후 .env 를 열어 실제 API 키 입력
cp config/watchlist_manual.txt.example config/watchlist_manual.txt  # 선택: 수동 관심종목 폴백
```

필요한 API 키 발급처:
- KIS: https://apiportal.koreainvestment.com (실전+모의투자 앱키 각각 발급)
- Naver 검색 API: https://developers.naver.com/apps
- Gemini API: https://aistudio.google.com/apikey
- Telegram Bot: BotFather 에게 `/newbot`, chat id는 `/status` 등을 직접 봇에게 보낸 뒤
  `https://api.telegram.org/bot<TOKEN>/getUpdates` 로 확인

로컬에서 문법/모듈 점검만 하려면:
```bash
python3 -m py_compile $(find src -name '*.py')
```

## 2. Oracle Cloud Free Tier VM 배포 (최초 1회, 수동)

```bash
# VM 에 SSH 접속 후
git clone <이 저장소 URL> /home/ubuntu/auto_trade
cd /home/ubuntu/auto_trade
cp config/.env.example .env && nano .env   # 실전/모의 API 키 입력
bash scripts/install.sh
```

`install.sh` 가 타임존을 `Asia/Seoul` 로 설정하고, venv/의존성 설치, systemd 서비스 등록까지
자동으로 수행합니다. (진입 제한 시간 09:00~09:30 판정이 서버 로컬시각 기준이므로 타임존
설정은 필수입니다.)

GitHub Actions 가 비밀번호 없이 서비스를 재시작할 수 있도록 sudoers 규칙을 추가하세요:
```bash
echo "ubuntu ALL=(ALL) NOPASSWD: /bin/systemctl restart trading_bot.service, /bin/systemctl is-active trading_bot.service" \
  | sudo tee /etc/sudoers.d/trading-bot-deploy
sudo chmod 440 /etc/sudoers.d/trading-bot-deploy
```

## 3. GitHub Actions 자동배포 설정

저장소 Settings → Secrets and variables → Actions 에 등록:

| Secret | 설명 |
| --- | --- |
| `SSH_HOST` | Oracle VM 공인 IP |
| `SSH_USER` | `ubuntu` |
| `SSH_PRIVATE_KEY` | VM 접속용 SSH 개인키 전문 |
| `SSH_PORT` | (선택) 기본 22 |
| `TELEGRAM_BOT_TOKEN` | 배포 완료 알림용 |
| `TELEGRAM_CHAT_ID` | 배포 완료 알림 수신할 chat id |

이후 `main` 브랜치에 push 하면 `.github/workflows/deploy.yml` 이 VM에 SSH 접속하여
`git pull` → `pip install -r requirements.txt` → `systemctl restart trading_bot.service` 를
자동 수행하고 결과를 텔레그램으로 보고합니다.

## 4. 텔레그램 원격 명령어

| 명령어 | 기능 |
| --- | --- |
| `/status` | 시스템 상태, 활성 슬롯, 보유 종목 평가손익 |
| `/news [종목명]` | Naver+Gemini 기반 실시간 뉴스 감성 요약 |
| `/supply_demand [종목명]` (`/sd`) | 실시간 체결강도, 호가잔량비 |
| `/re_screen` | 스크리닝 파이프라인 수동 재기동 |
| `/force_sell [종목명]` | 특정 종목 즉시 시장가 청산 |
| `/stop_all` | 긴급 전체 중지 + 보유잔고 전량 시장가 청산 |

`TELEGRAM_ALLOWED_CHAT_IDS` 에 등록되지 않은 사용자의 명령은 모두 거부됩니다.

## 5. 운영 중 로그 확인

```bash
sudo journalctl -u trading_bot.service -f     # systemd 표준출력
tail -f /home/ubuntu/auto_trade/logs/trading_bot.log   # 애플리케이션 로그
```

## 알려진 제한사항 (TODO)

- 외국인/기관 실시간 가집계 순매수액(`/supply_demand`)은 별도 KIS 투자자별 매매동향 API
  연동이 필요하며 현재 미구현 상태입니다 (체결강도/호가잔량비만 제공).
- 공휴일/임시휴장일 자동 판별이 없습니다. 필요 시 한국거래소 개장일 API를 연동하세요.
- 장전 스크리닝(등락률순위/거래대금순위)은 KIS 응답 스키마 재검증이 필요하며, 실패 시
  `config/watchlist_manual.txt` 수동 목록으로 자동 폴백합니다.
- 시장가 청산 시 실제 체결가는 판정가(목표가/손절가/시가)를 기준으로 DB에 기록되며,
  체결 후 실제 체결가로 사후 보정하려면 체결통보 조회 로직을 추가해야 합니다.

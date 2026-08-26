"""[1단계 08:30~08:50] 장 시작 전 종목 필터링 및 유니버스 선정.

※ 주의: KIS 의 순위분석 API(등락률순위/거래량순위) 는 파라미터 항목이 많고 문서 개정이
  잦은 편이라, 이 모듈은 "최선 노력"으로 구현되어 있다. API 응답 스키마가 예상과 다르면
  자동으로 수동 관심종목 파일(config/watchlist_manual.txt)로 폴백한다.
  실거래 투입 전, 반드시 모의투자 환경에서 스크리닝 결과가 실제 상한가/신고가 종목과
  일치하는지 최소 며칠간 검증할 것.
"""
from __future__ import annotations

from pathlib import Path

from src.broker.base import BrokerBase
from src.config import CONFIG, PROJECT_ROOT
from src.utils.logger import get_logger

log = get_logger("screener")

MANUAL_WATCHLIST_PATH = PROJECT_ROOT / "config" / "watchlist_manual.txt"

# 등락률순위 API 에서 상한가로 간주할 최소 등락률 (%) - 상한가는 통상 +30% 근접
LIMIT_UP_THRESHOLD_PCT = 29.5

# 거래량/거래대금순위 API는 개별 종목뿐 아니라 ETF/ETN(특히 레버리지/인버스 상품)이
# 상위권을 대량 차지해 후보 슬롯을 오염시키는 경우가 실측으로 확인됐다 (예: "KODEX 200선물인버스2X").
# v10 전략은 "전일 상한가/신고가 돌파 개별종목"을 찾는 것이 목적이므로 주요 자산운용사
# ETF/ETN 브랜드 접두어를 이름 기준으로 걸러낸다. 새 브랜드가 나오면 이 목록에 추가할 것.
_FUND_PRODUCT_MARKERS = (
    "KODEX", "TIGER", "KBSTAR", "ARIRANG", "KOSEF", "SOL", "ACE", "HANARO",
    "PLUS", "TIMEFOLIO", "WOORI", "마이티", "파워", "히어로즈", "FOCUS", "KTOP",
    "마이다스", "KCGI", "VITA", "대신343", "ETN", "ETF",
)


def _is_fund_product(name: str) -> bool:
    upper = (name or "").upper()
    return any(marker in upper for marker in _FUND_PRODUCT_MARKERS)


def _load_manual_watchlist() -> list[dict]:
    if not MANUAL_WATCHLIST_PATH.exists():
        return []
    candidates = []
    for line in MANUAL_WATCHLIST_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        code = parts[0]
        name = parts[1] if len(parts) > 1 else code
        candidates.append({"code": code, "name": name})
    return candidates


def _pick(row: dict, *keys, default=None):
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return default


def _candidates_from_ranking_apis(broker: BrokerBase) -> list[dict]:
    """코스피/코스닥을 나눠서 각각 조회한다 - 순위 API가 통합(0000) 조회 시 30건으로
    캡핑되어 있어, 시장을 분리하면 최대 60건까지 커버리지를 넓힐 수 있다 (실측 확인됨)."""
    codes_seen: dict[str, dict] = {}

    for market in ("KOSPI", "KOSDAQ"):
        try:
            for row in broker.get_fluctuation_rank(top_n=30, market=market):
                pct = float(_pick(row, "prdy_ctrt", default=0) or 0)
                if pct >= LIMIT_UP_THRESHOLD_PCT:
                    code = _pick(row, "stck_shrn_iscd", "mksc_shrn_iscd")
                    name = _pick(row, "hts_kor_isnm", default=code)
                    if code and not _is_fund_product(name):
                        codes_seen[code] = {"code": code, "name": name, "reason": f"상한가({market})"}
        except Exception:
            log.exception("등락률순위 API 조회 실패 (market=%s)", market)

        try:
            for row in broker.get_volume_rank(top_n=30, market=market):
                code = _pick(row, "mksc_shrn_iscd", "stck_shrn_iscd")
                name = _pick(row, "hts_kor_isnm", default=code)
                if code and code not in codes_seen and not _is_fund_product(name):
                    codes_seen[code] = {"code": code, "name": name, "reason": f"거래대금상위({market})"}
        except Exception:
            log.exception("거래량/거래대금순위 API 조회 실패 (market=%s)", market)

        try:
            for row in broker.get_near_high_rank(top_n=30, market=market):
                code = _pick(row, "mksc_shrn_iscd", "stck_shrn_iscd")
                name = _pick(row, "hts_kor_isnm", default=code)
                if code and code not in codes_seen and not _is_fund_product(name):
                    codes_seen[code] = {"code": code, "name": name, "reason": f"신고가근접({market})"}
        except Exception:
            log.exception("신고가근접순위 API 조회 실패 (market=%s)", market)

    return list(codes_seen.values())


def get_prescreening_candidates(broker: BrokerBase) -> list[dict]:
    """1차 예비 후보 (종목코드/명)만 추린다. 전일 거래대금/돌파패턴 검증은 screen_stock_batch 에서 수행."""
    candidates = _candidates_from_ranking_apis(broker)
    if candidates:
        log.info("자동 스크리닝(순위 API)으로 %d개 후보 확보", len(candidates))
        return candidates

    manual = _load_manual_watchlist()
    if manual:
        log.warning("순위 API 결과 없음 - 수동 관심종목 파일(%d개)로 폴백", len(manual))
        return manual

    log.error("자동/수동 스크리닝 모두 후보 없음. config/watchlist_manual.txt 를 채워주세요.")
    return []


def screen_stock_batch(daily_data: dict) -> tuple[bool, list[str]]:
    """3.1절 조건: 전일 거래대금 500억 이상 + 돌파/상한가 패턴."""
    reasons = []
    prev_trade_amount = daily_data.get("prev_trade_amount", 0) or 0
    if prev_trade_amount < CONFIG.min_prev_trade_amount:
        reasons.append(f"전일 거래대금 부족 ({prev_trade_amount/1e8:.1f}억 < {CONFIG.min_prev_trade_amount/1e8:.0f}억)")
    if not daily_data.get("is_breakout_or_limit_up"):
        reasons.append("전일 주요 저항선/상한가 돌파 패턴 아님")
    return len(reasons) == 0, reasons


def build_daily_info(broker: BrokerBase, code: str) -> dict:
    """일봉 데이터로 전일 종가/고가/거래대금 및 신고가 돌파 여부를 계산한다."""
    df = broker.get_daily_ohlcv(code, days=130)  # 약 6개월치 (52주 근사는 아니지만 120일 신고가 판정엔 충분)
    if df.empty or len(df) < 2:
        return {"prev_close": 0, "prev_high": 0, "prev_trade_amount": 0, "is_breakout_or_limit_up": False}

    prev_row = df.iloc[-1]
    prev_close = float(prev_row["close"])
    prev_high = float(prev_row["high"])
    prev_trade_amount = float(prev_row["trade_amount"])

    prior_high_120d = df.iloc[:-1]["high"].max() if len(df) > 1 else 0
    is_new_high_breakout = prev_high >= prior_high_120d if prior_high_120d else False

    prior_close = float(df.iloc[-2]["close"]) if len(df) >= 2 else prev_close
    change_pct = ((prev_close - prior_close) / prior_close * 100) if prior_close else 0
    is_limit_up = change_pct >= LIMIT_UP_THRESHOLD_PCT

    return {
        "prev_close": prev_close,
        "prev_high": prev_high,
        "prev_trade_amount": prev_trade_amount,
        "is_breakout_or_limit_up": bool(is_new_high_breakout or is_limit_up),
    }

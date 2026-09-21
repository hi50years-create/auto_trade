"""KIS 해외주식(미국) API 클라이언트. 국내 KISClient 를 상속해 인증/세션/토큰캐시/쓰로틀
배관은 그대로 재사용하고, 시세조회/주문 메서드만 해외 전용 엔드포인트로 교체한다.

※ 2026-09-21 기준 공식 예제(open-trading-api/examples_llm/overseas_stock, overseas_stock 현재가는
  overseas-price 그룹)로 검증한 TR_ID/엔드포인트를 쓴다. 국내와 달리 API 그룹마다 거래소코드
  표기가 다르다 (시세조회: EXCD="NAS"/"NYS"/"AMS", 주문: OVRS_EXCG_CD="NASD"/"NYSE"/"AMEX") -
  아래 _QUOTE_EXCD_MAP/_ORDER_EXCG_MAP 으로 분리해서 관리한다.
  미체결수량 조회(inquire-nccs) 응답 필드명은 공식 예제에 명시되어 있지 않아 KIS 표준 명명
  관례(주문수량 ORD_QTY - 체결수량 CCLD_QTY)로 추정 구현했다 - 실거래 전 반드시 모의투자로
  재검증할 것. 원화가 아니라 USD라 tick.usd_round(센트 단위)를 쓰고, KRX 식 계단 호가단위는
  적용하지 않는다.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Callable, Optional

import pandas as pd

from src.broker.base import OrderResult
from src.broker.kis_client import KISClient
from src.config import CONFIG
from src.utils.logger import get_logger
from src.utils.tick import usd_round

log = get_logger("kis_overseas_client")

TR_OVRS_ORDER_BUY = {"real": "TTTT1002U", "demo": "VTTT1002U"}
TR_OVRS_ORDER_SELL = {"real": "TTTT1006U", "demo": "VTTT1001U"}
TR_OVRS_ORDER_CANCEL = {"real": "TTTT1004U", "demo": "VTTT1004U"}
TR_OVRS_NCCS = "TTTS3018R"          # 미체결내역조회 (실전/모의 공통)
TR_OVRS_BALANCE = {"real": "TTTS3012R", "demo": "VTTS3012R"}  # 해외주식 잔고조회 (보유종목용)
TR_OVRS_PRESENT_BALANCE = {"real": "CTRP6504R", "demo": "VTRP6504R"}  # 해외주식 체결기준현재잔고 (예수금용)
TR_OVRS_CURRENT_PRICE = "HHDFS00000300"     # 해외주식 현재가 (실전/모의 공통)
TR_OVRS_DAILY_CHART = "HHDFS76240000"       # 해외주식 기간별시세(일봉)
TR_OVRS_MINUTE_CHART = "HHDFS76950200"      # 해외주식 분봉조회

# 시세조회(overseas-price) 그룹의 거래소코드 - 짧은 표기
_QUOTE_EXCD_MAP = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}


class KISOverseasClient(KISClient):
    def __init__(self, default_exchange: str = "NASD"):
        super().__init__(
            app_key=CONFIG.kis_us_app_key, app_secret=CONFIG.kis_us_app_secret,
            cano=CONFIG.kis_us_cano, acnt_prdt_cd=CONFIG.kis_us_acnt_prdt_cd,
        )
        self.default_exchange = default_exchange

    # ------------------------------------------------------------ 시세
    def get_current_price(self, code: str) -> float:
        data = self._get(
            "/uapi/overseas-price/v1/quotations/price", TR_OVRS_CURRENT_PRICE,
            {"AUTH": "", "EXCD": _QUOTE_EXCD_MAP[self.default_exchange], "SYMB": code},
        )
        try:
            return float(data["output"]["last"])
        except (KeyError, TypeError, ValueError):
            log.error("해외 현재가 파싱 실패 code=%s data=%s", code, data)
            return 0.0

    def get_daily_ohlcv(self, code: str, days: int = 30) -> pd.DataFrame:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=int(days * 1.6) + 10)).strftime("%Y%m%d")
        data = self._get(
            "/uapi/overseas-price/v1/quotations/dailyprice", TR_OVRS_DAILY_CHART,
            {
                "AUTH": "", "EXCD": _QUOTE_EXCD_MAP[self.default_exchange], "SYMB": code,
                "GUBN": "0", "BYMD": end, "MODP": "0",
            },
        )
        rows = data.get("output2", [])
        if not rows:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "trade_amount"])
        df = pd.DataFrame(rows)
        out = pd.DataFrame({
            "date": df["xymd"], "open": df["open"].astype(float), "high": df["high"].astype(float),
            "low": df["low"].astype(float), "close": df["clos"].astype(float),
            "volume": df["tvol"].astype(float), "trade_amount": df["tamt"].astype(float),
        })
        return out[out["date"] >= start].sort_values("date").reset_index(drop=True).tail(days)

    def get_minute_ohlcv_3m(self, code: str) -> pd.DataFrame:
        data = self._get(
            "/uapi/overseas-price/v1/quotations/inquire-time-itemchartprice", TR_OVRS_MINUTE_CHART,
            {
                "AUTH": "", "EXCD": _QUOTE_EXCD_MAP[self.default_exchange], "SYMB": code,
                "NMIN": "1", "PINC": "1", "NEXT": "", "NREC": "120", "FILL": "", "KEYB": "",
            },
        )
        rows = data.get("output2", [])
        if not rows:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows)
        out = pd.DataFrame({
            "time": df["khms"].astype(str).str.zfill(6),
            "open": df["open"].astype(float), "high": df["high"].astype(float),
            "low": df["low"].astype(float), "close": df["last"].astype(float),
            "volume": df["evol"].astype(float),
        })
        out = out.sort_values("time").reset_index(drop=True)
        return self._resample_3min(out)

    def get_index_change_pct(self, market: str) -> float:
        # 미국 시장 폭락 필터는 v1 범위 밖 (계획서 참고) - 항상 0 반환해 지수필터가 걸리지 않게 함.
        return 0.0

    # ------------------------------------------------------------ 주문
    def buy_limit(self, code: str, qty: int, price) -> OrderResult:
        return self._place_overseas_order(code, qty, price, is_buy=True)

    def sell_market(self, code: str, qty: int) -> OrderResult:
        # 해외주식은 순수 시장가 주문이 없어(대부분 거래소가 지정가만 지원), 현재가에 넉넉한
        # 슬리피지 버퍼를 준 지정가로 대체한다 (매도이므로 현재가보다 낮게 broad하게 잡아 체결 보장).
        current = self.get_current_price(code)
        aggressive_price = usd_round(current * 0.97) if current else 0
        return self._place_overseas_order(code, qty, aggressive_price, is_buy=False)

    def sell_limit(self, code: str, qty: int, price) -> OrderResult:
        return self._place_overseas_order(code, qty, price, is_buy=False)

    def _place_overseas_order(self, code: str, qty: int, price, is_buy: bool) -> OrderResult:
        tr_id = (TR_OVRS_ORDER_BUY if is_buy else TR_OVRS_ORDER_SELL)[self.env_dv]
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "OVRS_EXCG_CD": self.default_exchange, "PDNO": code,
            "ORD_QTY": str(qty), "OVRS_ORD_UNPR": f"{usd_round(price):.2f}",
            "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",
        }
        data = self._post("/uapi/overseas-stock/v1/trading/order", tr_id, body)
        ok = data.get("rt_cd") == "0"
        order_no = data.get("output", {}).get("ODNO", "") if ok else ""
        return OrderResult(success=ok, order_no=order_no, message=data.get("msg1", ""), raw=data)

    def cancel_order(self, order_no: str, code: str, qty: int) -> OrderResult:
        tr_id = TR_OVRS_ORDER_CANCEL[self.env_dv]
        body = {
            "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
            "OVRS_EXCG_CD": self.default_exchange, "PDNO": code,
            "ORGN_ODNO": order_no, "RVSE_CNCL_DVSN_CD": "02",
            "ORD_QTY": str(qty), "OVRS_ORD_UNPR": "0",
            "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0",
        }
        data = self._post("/uapi/overseas-stock/v1/trading/order-rvsecncl", tr_id, body)
        ok = data.get("rt_cd") == "0"
        return OrderResult(success=ok, order_no=order_no, message=data.get("msg1", ""), raw=data)

    def get_order_filled_qty(self, order_no: str) -> int:
        # ※ 응답 필드명 미확정 (모듈 docstring 참고) - 실거래 전 재검증 필요.
        data = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-nccs", TR_OVRS_NCCS,
            {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "OVRS_EXCG_CD": self.default_exchange, "SORT_SQN": "DS",
                "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
            },
        )
        rows = data.get("output", [])
        for row in rows:
            if row.get("odno") == order_no:
                try:
                    ord_qty = float(row.get("ft_ord_qty", 0))
                    ccld_qty = float(row.get("ft_ccld_qty", 0))
                    return int(ccld_qty) if ccld_qty > 0 else 0
                except (TypeError, ValueError):
                    return 0
        return -1  # 미체결 목록에서 안 보이면 전량 체결/취소완료로 간주 (국내 로직과 동일한 관례)

    # ------------------------------------------------------------ 계좌
    def get_cash_balance(self) -> float:
        return self.get_account_snapshot()["cash_balance"]

    def get_holdings(self) -> list[dict]:
        return self.get_account_snapshot()["holdings"]

    def get_account_snapshot(self) -> dict:
        data = self._get(
            "/uapi/overseas-stock/v1/trading/inquire-balance", TR_OVRS_BALANCE[self.env_dv],
            {
                "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                "OVRS_EXCG_CD": self.default_exchange, "TR_CRCY_CD": "USD",
                "CTX_AREA_FK200": "", "CTX_AREA_NK200": "",
            },
        )
        holdings = []
        for row in data.get("output1", []):
            try:
                holdings.append({
                    "code": row["ovrs_pdno"], "name": row.get("ovrs_item_name", row["ovrs_pdno"]),
                    "qty": int(float(row["ovrs_cblc_qty"])), "avg_price": float(row["pchs_avg_pric"]),
                    "current_price": float(row["now_pric2"]),
                    "eval_profit_pct": float(row.get("evlu_pfls_rt", 0)),
                })
            except (KeyError, TypeError, ValueError):
                continue

        # 예수금은 위 잔고조회(inquire-balance)가 아니라 별도 엔드포인트(체결기준현재잔고)의
        # output3(딕셔너리 하나, 리스트 아님)에 들어있다 - 2026-09-21 실측으로 확인.
        # 통합증거금 계좌는 통화별 "사용가능금액"(frcr_use_psbl_amt)이 실거래 전까지 0으로 찍히고,
        # 대신 "총자산금액"(tot_asst_amt, 원화환산)에 배정된 고정한도가 반영된다 - 화면엔 이걸 쓴다.
        cash = 0.0
        try:
            bal_data = self._get(
                "/uapi/overseas-stock/v1/trading/inquire-present-balance", TR_OVRS_PRESENT_BALANCE[self.env_dv],
                {
                    "CANO": self.cano, "ACNT_PRDT_CD": self.acnt_prdt_cd,
                    "WCRC_FRCR_DVSN_CD": "02", "NATN_CD": "840", "TR_MKET_CD": "00", "INQR_DVSN_CD": "00",
                },
            )
            output3 = bal_data.get("output3", {})
            frcr_usable = float(output3.get("frcr_use_psbl_amt", 0))
            cash = frcr_usable if frcr_usable > 0 else float(output3.get("tot_asst_amt", 0))
        except Exception:
            log.exception("해외 예수금 조회 실패")

        return {"cash_balance": cash, "holdings": holdings}

    # ------------------------------------------------------------ 실시간 (v1: 미지원, REST 폴백)
    def subscribe_realtime(self, codes: list[str], on_tick: Optional[Callable[[str, dict], None]]) -> None:
        # v1 범위 밖 (계획서 참고) - 해외 실시간 WS는 붙이지 않고 REST 폴링으로 대체한다.
        return None

    def get_realtime_snapshot(self, code: str) -> dict:
        return {"price": self.get_current_price(code)}

"""
한국투자증권(KIS) Open API v2 클라이언트 (REST + WebSocket).

※ 2026-08 기준, 공식 예제 저장소(https://github.com/koreainvestment/open-trading-api)의
  examples_llm/ 을 직접 조회하여 검증한 TR_ID 및 엔드포인트를 사용한다.
  아래 TR_ID 중 특히 국내주식 현금주문(order-cash)은 과거 문서(TTTC0802U/TTTC0801U)와
  달리 최신 스펙에서 TTTC0012U(매수)/TTTC0011U(매도) 로 변경되었고,
  대체거래소(NXT) 도입에 따라 EXCG_ID_DVSN_CD 파라미터가 신규 필수값으로 추가되었다.
  (KIS 는 API 스펙을 예고 없이 바꾸는 경우가 있으므로, 실거래 전환 전 반드시
   https://apiportal.koreainvestment.com 최신 문서 및 모의투자로 재검증할 것.)
"""
from __future__ import annotations

import json
import threading
import time as time_lib
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import requests
import websocket  # websocket-client

from src.broker.base import BrokerBase, OrderResult
from src.config import CONFIG, PROJECT_ROOT
from src.utils.logger import get_logger

log = get_logger("kis_client")

# ---------------------------------------------------------------- 도메인 상수
REAL_REST_BASE = "https://openapi.koreainvestment.com:9443"
PAPER_REST_BASE = "https://openapivts.koreainvestment.com:29443"
REAL_WS_URL = "ws://ops.koreainvestment.com:21000"
PAPER_WS_URL = "ws://ops.koreainvestment.com:31000"

TOKEN_CACHE_PATH = PROJECT_ROOT / ".kis_token_cache.json"

# TR_ID (실전 / 모의) - 2026-08 KIS 공식 examples_llm 기준 검증됨
TR_ORDER_BUY = {"real": "TTTC0012U", "demo": "VTTC0012U"}
TR_ORDER_SELL = {"real": "TTTC0011U", "demo": "VTTC0011U"}
TR_BALANCE = {"real": "TTTC8434R", "demo": "VTTC8434R"}
TR_ORDER_CANCEL = {"real": "TTTC0013U", "demo": "VTTC0013U"}  # 정정취소주문 (매수/매도 공용)
TR_CURRENT_PRICE = "FHKST01010100"
TR_DAILY_CHART = "FHKST03010100"
TR_MINUTE_CHART = "FHKST03010200"
TR_INDEX_PRICE = "FHPUP02100000"
TR_FLUCTUATION_RANK = "FHPST01700000"   # 등락률순위 (상한가 후보 탐색용)
TR_VOLUME_RANK = "FHPST01710000"        # 거래량/거래대금순위
TR_NEAR_NEW_HIGHLOW = "FHPST01870000"   # 신고가 근접 순위
TR_PSBL_RVSECNCL = {"real": "TTTC0084R", "demo": "VTTC0084R"}  # 정정취소가능주문조회 (KIS 는 모의투자시 T->V 자동치환)

WS_TR_TRADE = "H0STCNT0"     # 실시간체결가 (체결강도 포함)
WS_TR_ORDERBOOK = "H0STASP0"  # 실시간호가 (총 매도/매수 잔량 포함)

INDEX_CODE = {"KOSPI": "0001", "KOSDAQ": "1001"}


class KISClient(BrokerBase):
    def __init__(self):
        self.env_dv = "demo" if not CONFIG.is_live else "real"
        self.rest_base = PAPER_REST_BASE if self.env_dv == "demo" else REAL_REST_BASE
        self.ws_url = PAPER_WS_URL if self.env_dv == "demo" else REAL_WS_URL

        self._access_token: Optional[str] = None
        self._token_expire_at: Optional[datetime] = None
        self._approval_key: Optional[str] = None

        self._realtime = _RealtimeFeed(self)
        self._session = requests.Session()

        self._ensure_token()

    # ------------------------------------------------------------ 인증
    def _ensure_token(self):
        cached = self._load_cached_token()
        if cached:
            self._access_token, self._token_expire_at = cached
            log.info("KIS 접근토큰 캐시 재사용 (만료: %s)", self._token_expire_at)
            return
        self._issue_token()

    def _load_cached_token(self):
        if not TOKEN_CACHE_PATH.exists():
            return None
        try:
            data = json.loads(TOKEN_CACHE_PATH.read_text(encoding="utf-8"))
            expire_at = datetime.fromisoformat(data["expire_at"])
            if expire_at > datetime.now() + timedelta(minutes=10) and data.get("env") == self.env_dv:
                return data["access_token"], expire_at
        except Exception:
            return None
        return None

    def _save_token_cache(self, token: str, expire_at: datetime):
        TOKEN_CACHE_PATH.write_text(
            json.dumps({"access_token": token, "expire_at": expire_at.isoformat(), "env": self.env_dv}),
            encoding="utf-8",
        )

    def _issue_token(self):
        url = f"{self.rest_base}/oauth2/tokenP"
        body = {
            "grant_type": "client_credentials",
            "appkey": CONFIG.kis_app_key,
            "appsecret": CONFIG.kis_app_secret,
        }
        res = self._session.post(url, json=body, timeout=10)
        res.raise_for_status()
        data = res.json()
        self._access_token = data["access_token"]
        # KIS 는 access_token_token_expired 를 "YYYY-MM-DD HH:MM:SS" 로 반환
        expire_str = data.get("access_token_token_expired")
        self._token_expire_at = (
            datetime.strptime(expire_str, "%Y-%m-%d %H:%M:%S") if expire_str else datetime.now() + timedelta(hours=23)
        )
        self._save_token_cache(self._access_token, self._token_expire_at)
        log.info("KIS 접근토큰 신규 발급 완료 (만료: %s)", self._token_expire_at)

    def _ensure_ws_approval_key(self) -> str:
        if self._approval_key:
            return self._approval_key
        url = f"{self.rest_base}/oauth2/Approval"
        body = {
            "grant_type": "client_credentials",
            "appkey": CONFIG.kis_app_key,
            "secretkey": CONFIG.kis_app_secret,
        }
        res = self._session.post(url, json=body, timeout=10)
        res.raise_for_status()
        self._approval_key = res.json()["approval_key"]
        return self._approval_key

    def _headers(self, tr_id: str, extra: Optional[dict] = None) -> dict:
        if self._token_expire_at and datetime.now() >= self._token_expire_at - timedelta(minutes=5):
            self._issue_token()
        h = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self._access_token}",
            "appkey": CONFIG.kis_app_key,
            "appsecret": CONFIG.kis_app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if extra:
            h.update(extra)
        return h

    # ------------------------------------------------------------ REST 공통 헬퍼
    def _get(self, path: str, tr_id: str, params: dict) -> dict:
        res = self._session.get(f"{self.rest_base}{path}", headers=self._headers(tr_id), params=params, timeout=10)
        return self._parse(res, path)

    def _post(self, path: str, tr_id: str, body: dict) -> dict:
        res = self._session.post(f"{self.rest_base}{path}", headers=self._headers(tr_id), data=json.dumps(body), timeout=10)
        return self._parse(res, path)

    def _parse(self, res: requests.Response, path: str) -> dict:
        if res.status_code != 200:
            log.error("KIS API 오류 [%s] status=%s body=%s", path, res.status_code, res.text[:500])
            return {"rt_cd": "-1", "msg1": f"HTTP {res.status_code}", "output": {}}
        data = res.json()
        if data.get("rt_cd") != "0":
            log.warning("KIS API 응답 오류 [%s] msg_cd=%s msg1=%s", path, data.get("msg_cd"), data.get("msg1"))
        return data

    # ------------------------------------------------------------ 시세 조회
    def get_current_price(self, code: str) -> float:
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            TR_CURRENT_PRICE,
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code},
        )
        try:
            return float(data["output"]["stck_prpr"])
        except (KeyError, TypeError, ValueError):
            log.error("현재가 파싱 실패 code=%s data=%s", code, data)
            return 0.0

    def get_daily_ohlcv(self, code: str, days: int = 30) -> pd.DataFrame:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=int(days * 1.6) + 10)).strftime("%Y%m%d")
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            TR_DAILY_CHART,
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_DATE_1": start,
                "FID_INPUT_DATE_2": end,
                "FID_PERIOD_DIV_CODE": "D",
                "FID_ORG_ADJ_PRC": "0",
            },
        )
        rows = data.get("output2", [])
        if not rows:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "trade_amount"])
        df = pd.DataFrame(rows)
        out = pd.DataFrame({
            "date": df["stck_bsop_date"],
            "open": df["stck_oprc"].astype(float),
            "high": df["stck_hgpr"].astype(float),
            "low": df["stck_lwpr"].astype(float),
            "close": df["stck_clpr"].astype(float),
            "volume": df["acml_vol"].astype(float),
            "trade_amount": df["acml_tr_pbmn"].astype(float),
        })
        return out.sort_values("date").reset_index(drop=True).tail(days)

    def get_minute_ohlcv_3m(self, code: str) -> pd.DataFrame:
        """당일 1분봉을 조회해 3분봉으로 리샘플링한다.
        KIS 분봉조회는 1회 호출당 최근 30건까지만 반환하므로, 09:00~09:30 진입 구간
        감시 용도로는 충분하나 장마감까지 누적 이력이 필요하면 주기적으로 호출/누적해야 한다.
        """
        now_hhmmss = datetime.now().strftime("%H%M%S")
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            TR_MINUTE_CHART,
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": code,
                "FID_INPUT_HOUR_1": now_hhmmss,
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_ETC_CLS_CODE": "",
            },
        )
        rows = data.get("output2", [])
        if not rows:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows)
        df = pd.DataFrame({
            "time": df["stck_cntg_hour"].astype(str).str.zfill(6),
            "open": df["stck_oprc"].astype(float),
            "high": df["stck_hgpr"].astype(float),
            "low": df["stck_lwpr"].astype(float),
            "close": df["stck_prpr"].astype(float),
            "volume": df["cntg_vol"].astype(float),
        })
        df = df.sort_values("time").reset_index(drop=True)
        return self._resample_3min(df)

    @staticmethod
    def _resample_3min(df_1m: pd.DataFrame) -> pd.DataFrame:
        if df_1m.empty:
            return df_1m
        df_1m["minute"] = df_1m["time"].str[:4].astype(int)  # HHMM
        df_1m["bucket"] = (df_1m["minute"] // 3) * 3  # 3분 단위로 내림
        agg = df_1m.groupby("bucket").agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        ).reset_index()
        agg["time"] = agg["bucket"].apply(lambda b: f"{b // 100:02d}:{b % 100:02d}")
        return agg[["time", "open", "high", "low", "close", "volume"]]

    def get_index_change_pct(self, market: str) -> float:
        code = INDEX_CODE[market]
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-index-price",
            TR_INDEX_PRICE,
            {"FID_COND_MRKT_DIV_CODE": "U", "FID_INPUT_ISCD": code},
        )
        try:
            return float(data["output"]["prdy_ctrt"])
        except (KeyError, TypeError, ValueError):
            log.error("지수 변동률 파싱 실패 market=%s data=%s", market, data)
            return 0.0

    # ------------------------------------------------------------ 스크리닝 (순위 API)
    def get_fluctuation_rank(self, top_n: int = 30) -> list[dict]:
        """등락률 상위 종목 (상한가/급등 후보 탐색용). 필드/파라미터는 KIS 문서 재검증 권장."""
        data = self._get(
            "/uapi/domestic-stock/v1/ranking/fluctuation",
            TR_FLUCTUATION_RANK,
            {
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20170",
                "fid_input_iscd": "0000",
                "fid_rank_sort_cls_code": "0",  # 0: 상승율순
                "fid_input_cnt_1": "0",
                "fid_prc_cls_code": "0",
                "fid_input_price_1": "",
                "fid_input_price_2": "",
                "fid_vol_cnt": "",
                "fid_trgt_cls_code": "0",
                "fid_trgt_exls_cls_code": "0",
                "fid_div_cls_code": "0",
                "fid_rsfl_rate1": "",
                "fid_rsfl_rate2": "",
            },
        )
        return (data.get("output", []) or [])[:top_n]

    def get_volume_rank(self, top_n: int = 30) -> list[dict]:
        """거래대금/거래량 상위 종목. 필드/파라미터는 KIS 문서 재검증 권장."""
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            TR_VOLUME_RANK,
            {
                "fid_cond_mrkt_div_code": "J",
                "fid_cond_scr_div_code": "20171",
                "fid_input_iscd": "0000",
                "fid_div_cls_code": "0",
                "fid_blng_cls_code": "0",
                "fid_trgt_cls_code": "111111111",
                "fid_trgt_exls_cls_code": "0000000000",
                "fid_input_price_1": "",
                "fid_input_price_2": "",
                "fid_vol_cnt": "",
                "fid_input_date_1": "",
            },
        )
        return (data.get("output", []) or [])[:top_n]

    # ------------------------------------------------------------ 주문
    def buy_limit(self, code: str, qty: int, price: int) -> OrderResult:
        return self._place_order(code, qty, price, is_buy=True)

    def sell_market(self, code: str, qty: int) -> OrderResult:
        # 시장가 매도: ORD_DVSN "01", ORD_UNPR "0"
        return self._place_order(code, qty, price=0, is_buy=False, ord_dvsn="01")

    def _place_order(self, code: str, qty: int, price: int, is_buy: bool, ord_dvsn: str = "00") -> OrderResult:
        tr_id = (TR_ORDER_BUY if is_buy else TR_ORDER_SELL)[self.env_dv]
        body = {
            "CANO": CONFIG.kis_cano,
            "ACNT_PRDT_CD": CONFIG.kis_acnt_prdt_cd,
            "PDNO": code,
            "ORD_DVSN": ord_dvsn,  # 00: 지정가, 01: 시장가
            "ORD_QTY": str(qty),
            "ORD_UNPR": str(price),
            "EXCG_ID_DVSN_CD": "KRX",
        }
        if not is_buy:
            body["SLL_TYPE"] = "01"
        data = self._post("/uapi/domestic-stock/v1/trading/order-cash", tr_id, body)
        ok = data.get("rt_cd") == "0"
        order_no = data.get("output", {}).get("ODNO", "") if ok else ""
        return OrderResult(success=ok, order_no=order_no, message=data.get("msg1", ""), raw=data)

    def cancel_order(self, order_no: str, code: str, qty: int) -> OrderResult:
        tr_id = TR_ORDER_CANCEL[self.env_dv]
        body = {
            "CANO": CONFIG.kis_cano,
            "ACNT_PRDT_CD": CONFIG.kis_acnt_prdt_cd,
            "KRX_FWDG_ORD_ORGNO": "",
            "ORGN_ODNO": order_no,
            "ORD_DVSN": "00",
            "RVSE_CNCL_DVSN_CD": "02",  # 02: 취소
            "ORD_QTY": str(qty),
            "ORD_UNPR": "0",
            "QTY_ALL_ORD_YN": "Y",
            "EXCG_ID_DVSN_CD": "KRX",
        }
        data = self._post("/uapi/domestic-stock/v1/trading/order-rvsecncl", tr_id, body)
        ok = data.get("rt_cd") == "0"
        return OrderResult(success=ok, order_no=order_no, message=data.get("msg1", ""), raw=data)

    def get_order_filled_qty(self, order_no: str) -> int:
        """정정취소가능주문조회(TTTC0084R)로 미체결수량을 확인해 (주문수량 - 미체결수량) 을 체결수량으로 간주한다."""
        tr_id = TR_PSBL_RVSECNCL[self.env_dv]
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-psbl-rvsecncl",
            tr_id,
            {
                "CANO": CONFIG.kis_cano,
                "ACNT_PRDT_CD": CONFIG.kis_acnt_prdt_cd,
                "INQR_DVSN_1": "0",  # 0: 주문순
                "INQR_DVSN_2": "0",  # 0: 전체(매도+매수)
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        for row in data.get("output", []) or []:
            if row.get("odno") == order_no:
                ord_qty = int(row.get("ord_qty", 0) or 0)
                psbl_qty = int(row.get("psbl_qty", 0) or 0)  # 취소가능(=미체결) 수량
                return max(ord_qty - psbl_qty, 0)
        # 주문이 목록에 없으면 완전 체결되었거나 이미 취소된 것으로 간주
        return -1

    # ------------------------------------------------------------ 잔고/보유
    def get_cash_balance(self) -> float:
        out1, out2 = self._inquire_balance()
        if out2 is None or out2.empty:
            return 0.0
        try:
            return float(out2.iloc[0]["dnca_tot_amt"])  # 예수금총금액
        except (KeyError, IndexError, ValueError):
            log.error("예수금 파싱 실패 out2=%s", out2)
            return 0.0

    def get_holdings(self) -> list[dict]:
        out1, _ = self._inquire_balance()
        if out1 is None or out1.empty:
            return []
        holdings = []
        for _, row in out1.iterrows():
            qty = int(float(row.get("hldg_qty", 0) or 0))
            if qty <= 0:
                continue
            holdings.append({
                "code": row.get("pdno"),
                "name": row.get("prdt_name"),
                "qty": qty,
                "avg_price": float(row.get("pchs_avg_pric", 0) or 0),
                "current_price": float(row.get("prpr", 0) or 0),
                "eval_profit_pct": float(row.get("evlu_pfls_rt", 0) or 0),
            })
        return holdings

    def _inquire_balance(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        tr_id = TR_BALANCE[self.env_dv]
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-balance",
            tr_id,
            {
                "CANO": CONFIG.kis_cano,
                "ACNT_PRDT_CD": CONFIG.kis_acnt_prdt_cd,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        out1 = pd.DataFrame(data.get("output1", []) or [])
        out2 = pd.DataFrame(data.get("output2", []) or [])
        return out1, out2

    # ------------------------------------------------------------ 실시간 시세
    def subscribe_realtime(self, codes: list[str], on_tick: Callable[[str, dict], None]) -> None:
        self._realtime.subscribe(codes, on_tick)

    def get_realtime_snapshot(self, code: str) -> dict:
        return self._realtime.get_snapshot(code)


class _RealtimeFeed:
    """KIS WebSocket 실시간 체결가(H0STCNT0) + 실시간호가(H0STASP0) 구독 스레드.
    별도 스레드에서 자체 이벤트 루프로 동작하며, 수신 데이터를 스냅샷 딕셔너리에 반영한다.
    """

    def __init__(self, client: "KISClient"):
        self._client = client
        self._ws: Optional[websocket.WebSocketApp] = None
        self._thread: Optional[threading.Thread] = None
        self._snapshots: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._codes: list[str] = []
        self._on_tick: Optional[Callable[[str, dict], None]] = None

    def subscribe(self, codes: list[str], on_tick: Callable[[str, dict], None]):
        self._codes = codes
        self._on_tick = on_tick
        if self._thread and self._thread.is_alive():
            log.info("실시간 피드가 이미 동작 중입니다. 신규 종목은 재연결 시 반영됩니다.")
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="kis-ws-feed")
        self._thread.start()

    def get_snapshot(self, code: str) -> dict:
        with self._lock:
            return dict(self._snapshots.get(code, {}))

    def _run(self):
        while True:
            try:
                approval_key = self._client._ensure_ws_approval_key()
                self._ws = websocket.WebSocketApp(
                    self._client.ws_url,
                    on_open=lambda ws: self._on_open(ws, approval_key),
                    on_message=self._on_message,
                    on_error=lambda ws, e: log.error("WS 오류: %s", e),
                    on_close=lambda ws, code, msg: log.warning("WS 연결 종료 (code=%s, msg=%s), 5초 후 재접속", code, msg),
                )
                self._ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception:
                log.exception("실시간 피드 스레드 예외 발생, 5초 후 재시도")
            time_lib.sleep(5)

    def _on_open(self, ws, approval_key: str):
        log.info("KIS WebSocket 연결 성공, %d개 종목 구독 시작", len(self._codes))
        for code in self._codes:
            for tr_id in (WS_TR_TRADE, WS_TR_ORDERBOOK):
                msg = {
                    "header": {
                        "approval_key": approval_key,
                        "custtype": "P",
                        "tr_type": "1",
                        "content-type": "utf-8",
                    },
                    "body": {"input": {"tr_id": tr_id, "tr_key": code}},
                }
                ws.send(json.dumps(msg))
                time_lib.sleep(0.1)

    def _on_message(self, ws, message: str):
        if message[0] not in ("0", "1"):
            # JSON 형태의 PINGPONG/구독응답 메시지
            return
        try:
            parts = message.split("|")
            tr_id = parts[1]
            body = parts[3]
            rows = body.split("^")
            if tr_id == WS_TR_TRADE:
                self._handle_trade(rows)
            elif tr_id == WS_TR_ORDERBOOK:
                self._handle_orderbook(rows)
        except Exception:
            log.exception("실시간 메시지 파싱 실패: %s", message[:200])

    def _handle_trade(self, f: list[str]):
        # H0STCNT0 필드 순서 (공식 컬럼 정의 기준, 0-index)
        # 0: MKSC_SHRN_ISCD, 1: STCK_CNTG_HOUR, 2: STCK_PRPR, ... 18: CTTR(체결강도)
        try:
            code = f[0]
            price = float(f[2])
            cttr = float(f[18]) if len(f) > 18 and f[18] not in ("", None) else None
            acml_vol = float(f[13]) if len(f) > 13 and f[13] not in ("", None) else None
            with self._lock:
                snap = self._snapshots.setdefault(code, {})
                snap["price"] = price
                if cttr is not None:
                    snap["vol_power"] = cttr
                if acml_vol is not None:
                    snap["acml_vol"] = acml_vol
                snap["updated_at"] = datetime.now().isoformat()
            if self._on_tick:
                self._on_tick(code, dict(self._snapshots[code]))
        except (IndexError, ValueError):
            log.debug("체결 필드 파싱 실패: %s", f[:5])

    def _handle_orderbook(self, f: list[str]):
        # H0STASP0: 41: TOTAL_ASKP_RSQN(총매도호가잔량), 42: TOTAL_BIDP_RSQN(총매수호가잔량)
        try:
            code = f[0]
            total_ask = float(f[41]) if len(f) > 41 and f[41] not in ("", None) else 0.0
            total_bid = float(f[42]) if len(f) > 42 and f[42] not in ("", None) else 0.0
            ratio = (total_bid / total_ask * 100.0) if total_ask > 0 else 0.0
            with self._lock:
                snap = self._snapshots.setdefault(code, {})
                snap["ask_bid_ratio"] = ratio
                snap["total_ask_qty"] = total_ask
                snap["total_bid_qty"] = total_bid
                snap["updated_at"] = datetime.now().isoformat()
        except (IndexError, ValueError):
            log.debug("호가 필드 파싱 실패: %s", f[:5])

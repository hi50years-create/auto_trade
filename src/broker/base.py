"""브로커 어댑터 추상 인터페이스.
KIS 이외의 증권사 API를 나중에 추가하고 싶다면 이 인터페이스만 구현하면 된다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd


@dataclass
class OrderResult:
    success: bool
    order_no: str = ""
    message: str = ""
    raw: Optional[dict] = None


class BrokerBase(ABC):
    @abstractmethod
    def get_current_price(self, code: str) -> float:
        ...

    @abstractmethod
    def get_daily_ohlcv(self, code: str, days: int = 30) -> pd.DataFrame:
        ...

    @abstractmethod
    def get_minute_ohlcv_3m(self, code: str) -> pd.DataFrame:
        """당일 3분봉 OHLCV (시가 이탈/재돌파 판정용)."""
        ...

    @abstractmethod
    def get_index_change_pct(self, market: str) -> float:
        """market: 'KOSPI' | 'KOSDAQ'"""
        ...

    @abstractmethod
    def buy_limit(self, code: str, qty: int, price: int) -> OrderResult:
        ...

    @abstractmethod
    def sell_market(self, code: str, qty: int) -> OrderResult:
        ...

    @abstractmethod
    def cancel_order(self, order_no: str, code: str, qty: int) -> OrderResult:
        ...

    @abstractmethod
    def get_order_filled_qty(self, order_no: str) -> int:
        ...

    @abstractmethod
    def get_cash_balance(self) -> float:
        ...

    @abstractmethod
    def get_holdings(self) -> list[dict]:
        ...

    @abstractmethod
    def get_account_snapshot(self) -> dict:
        """예수금+보유종목을 단일 API 호출로 함께 반환한다: {"cash_balance": float, "holdings": list[dict]}."""
        ...

    @abstractmethod
    def subscribe_realtime(self, codes: list[str], on_tick: Optional[Callable[[str, dict], None]]) -> None:
        """실시간 체결/호가 구독 시작 (비동기 백그라운드)."""
        ...

    @abstractmethod
    def get_realtime_snapshot(self, code: str) -> dict:
        """가장 최근 수신된 실시간 체결강도/호가잔량비 스냅샷."""
        ...

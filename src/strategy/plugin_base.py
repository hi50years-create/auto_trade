"""진입/청산 판정 로직을 종목별 상태머신(state_machine.StockWatcher)에서 분리해낸 전략 플러그인
인터페이스. 슬롯 관리/주문 집행/DB 기록/텔레그램 알림 같은 공통 배관은 그대로 StockWatcher가
맡고, "언제 사길래/언제 팔까"만 Strategy 구현체가 결정한다.

새 전략을 추가하려면 이 인터페이스만 구현하면 된다 (예: RSI반등, 볼린저밴드복귀 등).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class EntrySignal:
    price: float
    reason: str
    structural_stop_price: float | None = None


@dataclass
class ExitSignal:
    price: float
    reason: str
    prefer_limit: bool = False


class Strategy(ABC):
    """종목 1개를 감시하는 동안 딱 1개 인스턴스가 만들어진다 (이동평균/저점 추적 등 내부 상태를
    인스턴스 필드에 들고 있어도 된다 - 종목별로 독립적이므로 안전하다)."""

    # IDLE -> WAIT_FOR_BREAKOUT 전이에 "실시간가가 당일 시가 아래로 떨어지는" 선행조건이
    # 필요한 전략이면 True (기존 시가돌파+저점반등 전략의 전제조건). 그런 전제가 필요 없는
    # 전략(예: 이동평균 교차)은 False로 두면 감시 시작과 동시에 바로 봉 판정을 시작한다.
    requires_dip_below_open: bool = True

    @abstractmethod
    async def on_bar(self, broker, code: str, bar, day_open: float) -> EntrySignal | None:
        """완성된 봉(3분봉 등) 하나가 들어올 때마다 호출된다. 진입 신호가 없으면 None."""
        ...

    def on_tick_holding(
        self, rt_price: float, buy_price: float, day_open: float, structural_stop_price: float | None
    ) -> ExitSignal | None:
        """포지션 보유 중 실시간 틱마다 호출된다. 공통 목표익절/고정손절/장마감청산은
        StockWatcher가 이미 처리하므로, 여기서는 전략 고유의 추가 청산 조건만 반환하면 된다.
        기본 구현은 없음(공통 규칙만으로 충분한 전략은 오버라이드하지 않아도 됨)."""
        return None

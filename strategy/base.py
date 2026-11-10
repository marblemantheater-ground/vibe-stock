"""전략 공통 인터페이스.

전략은 틱을 받아 매매 신호(Signal)만 만든다. 주문 전송·잔고 확인·중복 주문 방지는
주문 관리자(4단계)가 맡는다. 덕분에 전략을 바꿔도 주문 로직은 그대로 재사용된다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol

from broker.account import Holding
from core.ws_client import Tick
from market.bars import Bar
from market.clock import KST, Session


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def label(self) -> str:
        return "매수" if self is Side.BUY else "매도"


@dataclass(frozen=True)
class Signal:
    symbol: str
    exchange: str      # 주문용 거래소 코드 (NASD/NYSE/AMEX)
    side: Side
    qty: int
    price: float       # 신호 발생 시점 가격 (주문 가격 결정은 4단계)
    reason: str
    created_at: datetime = field(default_factory=lambda: datetime.now(KST))

    def __str__(self) -> str:
        return f"{self.side.label} {self.symbol} {self.qty}주 @ ${self.price:,.2f} ({self.reason})"


class StrategyContext(Protocol):
    """전략이 참고하는 계좌 상태 (읽기 전용)."""

    def position(self, symbol: str) -> Holding | None: ...

    def position_count(self) -> int: ...


class Strategy(ABC):
    name: str = "base"

    def seed(self, symbol: str, bars: list[Bar]) -> None:
        """시작 시 과거 분봉 주입 (필요한 전략만 구현)."""

    @abstractmethod
    def on_tick(self, tick: Tick, session: Session, ctx: StrategyContext) -> list[Signal]:
        """틱 1건을 처리하고 발생한 신호를 반환한다. 빠르게 끝나야 하며 I/O 를 하지 않는다."""

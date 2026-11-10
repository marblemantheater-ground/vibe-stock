"""이동평균 돌파 + 등락률 필터 전략.

매수: 단기 이평이 장기 이평을 상향 돌파(골든크로스)한 봉 마감 시점에,
      전일 대비 등락률이 [min_change_rate, max_change_rate] 범위 안이면 매수
매도: 보유 중 ① 익절(+take_profit_pct%) ② 손절(-stop_loss_pct%) → 틱마다 검사
      ③ 데드크로스 → 봉 마감 시 검사
"""
from __future__ import annotations

import logging
import time
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from core.ws_client import Tick
from market.bars import MAX_HISTORY_BARS, Bar, BarBuilder
from market.clock import Session
from market.watchlist import WatchItem
from strategy.base import Side, Signal, Strategy, StrategyContext

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MaCrossParams:
    bar_minutes: int = 1
    short_window: int = 5
    long_window: int = 20
    min_change_rate: float = 0.0
    max_change_rate: float = 5.0
    take_profit_pct: float = 3.0
    stop_loss_pct: float = 2.0
    budget_per_trade_usd: float = 1000.0
    max_positions: int = 5
    cooldown_sec: float = 300.0
    sessions: tuple[Session, ...] = (Session.REGULAR,)

    @classmethod
    def from_toml(cls, path: Path) -> MaCrossParams:
        with path.open("rb") as f:
            raw = tomllib.load(f).get("ma_cross", {})
        known = {f.name for f in fields(cls)}
        unknown = set(raw) - known
        if unknown:
            raise ValueError(f"{path.name}: 알 수 없는 설정 {sorted(unknown)}")
        if "sessions" in raw:
            raw["sessions"] = tuple(Session(s.upper()) for s in raw["sessions"])
        params = cls(**raw)
        params.validate()
        return params

    def validate(self) -> None:
        if not 0 < self.short_window < self.long_window:
            raise ValueError("short_window < long_window 이어야 합니다")
        if self.long_window + 1 > MAX_HISTORY_BARS:
            raise ValueError(f"long_window 는 {MAX_HISTORY_BARS - 1} 이하여야 합니다 (과거 분봉 조회 한도)")
        if self.min_change_rate > self.max_change_rate:
            raise ValueError("min_change_rate <= max_change_rate 이어야 합니다")
        if self.bar_minutes not in (1, 2, 3, 5, 10, 15, 30, 60):
            raise ValueError("bar_minutes 는 1/2/3/5/10/15/30/60 중 하나여야 합니다")


def _sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / n


class MovingAverageCrossStrategy(Strategy):
    name = "ma_cross"

    def __init__(self, params: MaCrossParams, watchlist: list[WatchItem]):
        self.p = params
        self._items = {w.symbol: w for w in watchlist}
        self._bars = {w.symbol: BarBuilder(params.bar_minutes) for w in watchlist}
        self._cooldown_until: dict[str, float] = {}

    def seed(self, symbol: str, bars: list[Bar]) -> None:
        if symbol in self._bars:
            self._bars[symbol].seed(bars)

    def ready(self, symbol: str) -> bool:
        return len(self._bars[symbol].closed) > self.p.long_window

    def on_tick(self, tick: Tick, session: Session, ctx: StrategyContext) -> list[Signal]:
        builder = self._bars.get(tick.symbol)
        if builder is None:
            return []
        closed_bar = builder.update(tick)   # 세션과 무관하게 봉은 계속 쌓는다

        if session not in self.p.sessions or self._in_cooldown(tick.symbol):
            return []

        pos = ctx.position(tick.symbol)
        signal = self._check_exit(tick, pos) if pos else None
        if signal is None and closed_bar is not None and self.ready(tick.symbol):
            signal = self._check_cross(tick, builder, pos, ctx)

        if signal is None:
            return []
        self._cooldown_until[tick.symbol] = time.monotonic() + self.p.cooldown_sec
        return [signal]

    # ── 조건 ────────────────────────────────────────────────
    def _check_exit(self, tick: Tick, pos) -> Signal | None:
        if pos.avg_price <= 0 or pos.orderable_qty <= 0:
            return None
        pnl_pct = (tick.price / pos.avg_price - 1) * 100
        if pnl_pct >= self.p.take_profit_pct:
            return self._signal(tick, Side.SELL, int(pos.orderable_qty), f"익절 {pnl_pct:+.2f}%")
        if pnl_pct <= -self.p.stop_loss_pct:
            return self._signal(tick, Side.SELL, int(pos.orderable_qty), f"손절 {pnl_pct:+.2f}%")
        return None

    def _check_cross(self, tick: Tick, builder: BarBuilder, pos, ctx: StrategyContext) -> Signal | None:
        closes = builder.closes()
        s, l = self.p.short_window, self.p.long_window
        prev_short, prev_long = _sma(closes[:-1], s), _sma(closes[:-1], l)
        cur_short, cur_long = _sma(closes, s), _sma(closes, l)
        ma_text = f"MA{s} {cur_short:,.2f} / MA{l} {cur_long:,.2f}"

        if prev_short <= prev_long and cur_short > cur_long:   # 골든크로스
            if pos:
                return None
            rate = tick.change_rate
            if not self.p.min_change_rate <= rate <= self.p.max_change_rate:
                log.info("[%s] 골든크로스지만 등락률 %.2f%% 가 범위 밖 → 매수 보류", tick.symbol, rate)
                return None
            if ctx.position_count() >= self.p.max_positions:
                log.info("[%s] 골든크로스지만 최대 보유 종목 수(%d) 도달", tick.symbol, self.p.max_positions)
                return None
            qty = int(self.p.budget_per_trade_usd // tick.price)
            if qty < 1:
                log.info("[%s] 1주 가격($%.2f)이 1회 예산보다 큼 → 매수 보류", tick.symbol, tick.price)
                return None
            return self._signal(tick, Side.BUY, qty, f"골든크로스 {ma_text}, 등락률 {rate:+.2f}%")

        if prev_short >= prev_long and cur_short < cur_long and pos and pos.orderable_qty > 0:
            return self._signal(tick, Side.SELL, int(pos.orderable_qty), f"데드크로스 {ma_text}")
        return None

    # ── 보조 ────────────────────────────────────────────────
    def _in_cooldown(self, symbol: str) -> bool:
        return time.monotonic() < self._cooldown_until.get(symbol, 0.0)

    def _signal(self, tick: Tick, side: Side, qty: int, reason: str) -> Signal:
        return Signal(tick.symbol, self._items[tick.symbol].exchange, side, qty, tick.price, reason)

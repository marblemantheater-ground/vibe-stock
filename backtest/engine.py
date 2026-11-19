"""백테스트: 저장된 1분봉으로 실제 매매와 같은 전략 코드(MovingAverageCrossStrategy)를 돌려 본다.

재현 방식
- 1분봉 하나를 가상 틱 4개로 바꾼다: 시가 → (양봉이면 저가 → 고가, 음봉이면 고가 → 저가) → 종가
  봉 안에서 보유 종목의 익절·손절 가격을 지나가면 그 가격의 틱을 끼워 넣는다 (고가에 팔린 것처럼 부풀리지 않게)
- 전략은 그 틱으로 N분봉을 만들고 신호를 낸다 (봉 단위·이평·등락률·세션·신호 간격 모두 실제와 같은 코드)
- 등락률(전일 대비)은 전 거래일 정규장 마지막 분봉 종가 기준
- 체결: 신호 가격에서 fill_slippage_pct 만큼 불리하게 즉시 전량 체결 (미체결·부분 체결은 없다고 가정)
- 주문 규칙: 하루 최대 주문 수, 종목당 하루 매수 횟수, 최대 보유 종목 수, 현금 부족 시 매수 안 함
- 자금: 거래일 시작 현금으로 종목당 예산 계산 (capital_usage_pct), 수수료는 [fees] 기준
- 손실 차단: 하루 손실 한도·최대 낙폭(일 마감 고점 기준). 발동 시 block_buys 또는 liquidate (확인 시간은 생략)
- 거래일 사이에도 보유는 이어지고, 기간이 끝나면 마지막 가격으로 평가한다 (미청산으로 표시)

한계: 호가·체결 가능 수량, 지정가 미체결, 실시간 틱의 순서·간격은 재현하지 못한다. 분봉 기간이 짧으면 거래 수가 적어
결과의 우연성이 크다.
"""
from __future__ import annotations

import heapq
import logging
import math
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from typing import Callable

from broker.order import OrderParams
from core.ws_client import Tick
from market.bars import Bar
from market.clock import ET, KST, MarketClock, Session
from market.minute import MinuteStore
from market.watchlist import WatchItem
from report.annual import FeeParams
from risk.guard import RiskParams
from strategy.base import Side, Signal
from strategy.ma_cross import MaCrossParams, MovingAverageCrossStrategy

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BacktestParams:
    start: date
    end: date                         # 포함
    symbols: tuple[str, ...]
    start_cash: float = 100_000.0
    fill_slippage_pct: float = 0.05   # 신호 가격보다 이만큼 불리하게 체결 (매수 위, 매도 아래)


@dataclass
class _Pos:
    qty: int
    avg_price: float           # 체결가 평균 (수수료 제외, KIS 매입평균가와 같은 기준)
    buy_fee: float
    opened: datetime
    reason: str

    @property
    def orderable_qty(self) -> int:
        return self.qty


class _Ctx:
    def __init__(self, positions: dict[str, _Pos], buys_today: Callable[[], int] = lambda: 0):
        self._p = positions
        self.buys_today = buys_today
        self.sold_at: dict[str, datetime] = {}

    def last_sell_at(self, symbol: str) -> datetime | None:
        return self.sold_at.get(symbol)

    def entry_time(self, symbol: str) -> datetime | None:
        p = self._p.get(symbol)
        return p.opened if p else None

    def position(self, symbol: str):
        return self._p.get(symbol)

    def position_count(self) -> int:
        return len(self._p)


@dataclass
class _Day:
    date: date
    start_equity: float
    start_cash: float = 0.0
    orders: int = 0
    buys: dict[str, int] = field(default_factory=dict)


def _tick(symbol: str, price: float, prev_close: float | None, t: datetime, volume: float = 0.0) -> Tick:
    """가상 틱. 체결강도·호가는 분봉에 없어 0 → 그 거르개는 백테스트에서 적용되지 않는다."""
    rate = (price / prev_close - 1) * 100 if prev_close else 0.0
    return Tick(symbol, symbol, price, price, price, price, 0.0, rate, 0.0, 0.0, volume, 0.0, 0.0, "", t, 0.0)


def _path(bar: Bar) -> list[float]:
    mid = [bar.low, bar.high] if bar.close >= bar.open else [bar.high, bar.low]
    return [bar.open, *mid, bar.close]


class Backtest:
    def __init__(self, bt: BacktestParams, ma: MaCrossParams, order: OrderParams, risk: RiskParams, fees: FeeParams,
                 store: MinuteStore, progress: Callable[[float], None] | None = None):
        self.bt, self.ma, self.order, self.risk, self.fees = bt, ma, order, risk, fees
        self.store = store
        self.progress = progress or (lambda _p: None)
        self.clock = MarketClock()
        self._windows: dict[date, list] = {}

    # ── 보조 ────────────────────────────────────────────────
    def _session(self, t: datetime) -> Session:
        et = t.astimezone(ET)
        ws = self._windows.get(et.date())
        if ws is None:
            ws = self._windows[et.date()] = self.clock.windows(et.date())
        for w in ws:
            if w.start <= et < w.end:
                return w.session
        return Session.CLOSED

    def _load(self) -> dict[str, list[Bar]]:
        # 이동평균 준비용으로 시작일 전 며칠치도 읽는다 (그 구간은 매매하지 않음)
        warm_days = max(3, math.ceil(self.ma.long_window * self.ma.bar_minutes / 390) + 2)
        lo = datetime.combine(self.bt.start - timedelta(days=warm_days * 7 // 5 + 3), datetime.min.time(), ET)
        hi = datetime.combine(self.bt.end + timedelta(days=1), datetime.min.time(), ET)
        data = {}
        for s in self.bt.symbols:
            bars = self.store.load(s, lo.astimezone(KST), hi.astimezone(KST))
            if bars:
                data[s] = bars
        return data

    # ── 실행 ────────────────────────────────────────────────
    def run(self) -> dict:
        t0 = time.monotonic()
        data = self._load()
        if not data:
            raise ValueError("선택한 기간·종목에 저장된 분봉이 없습니다. 먼저 분봉을 수집하세요 (--collect-minute)")
        items = [WatchItem(s, "NASD") for s in data]
        strategy = MovingAverageCrossStrategy(self.ma, items)
        sim_now = [0.0]
        strategy.now = lambda: sim_now[0]

        cash = self.bt.start_cash
        positions: dict[str, _Pos] = {}
        ctx = _Ctx(positions, lambda: sum(day.buys.values()) if day else 0)
        last_price: dict[str, float] = {}
        prev_close: dict[str, float] = {}           # 전 거래일 정규장 종가
        today_reg_close: dict[str, float] = {}
        first_price: dict[str, float] = {}
        trades: list[dict] = []
        fills: list[dict] = []
        equity_curve: list[dict] = []
        fees_total = 0.0
        day: _Day | None = None
        peak: float | None = None
        halt: str | None = None                       # None / DAILY_LOSS / DAILY_TARGET / MAX_DRAWDOWN
        liquidate = False
        skipped = {"cash": 0, "max_orders": 0, "buys_per_symbol": 0, "halt": 0}   # 건너뛴 매수 신호
        halts: list[dict] = []

        def equity() -> float:
            return cash + sum(p.qty * last_price.get(s, p.avg_price) for s, p in positions.items())

        def close_day() -> None:
            nonlocal peak, halt
            if day is None or day.date < self.bt.start:
                return
            eq = equity()
            bench = (sum(last_price[s] / first_price[s] for s in first_price) / len(first_price)
                     * self.bt.start_cash) if first_price else self.bt.start_cash
            equity_curve.append({"date": day.date.isoformat(), "equity": round(eq, 2), "benchmark": round(bench, 2),
                                 "cash": round(cash, 2), "positions": len(positions)})
            peak = eq if peak is None else max(peak, eq)
            if halt in ("DAILY_LOSS", "DAILY_TARGET"):
                halt = None   # 다음 거래일 자동 해제

        def execute(sig: Signal, t: datetime, trading: bool) -> None:
            nonlocal cash, fees_total
            if not trading:
                return
            if sig.side is Side.BUY:
                if halt:
                    skipped["halt"] += 1
                    return
                if day.orders >= self.order.max_orders_per_day:
                    skipped["max_orders"] += 1
                    return
                if self.order.max_buys_per_symbol and day.buys.get(sig.symbol, 0) >= self.order.max_buys_per_symbol:
                    skipped["buys_per_symbol"] += 1
                    return
                price = sig.price * (1 + self.bt.fill_slippage_pct / 100)
                amount = price * sig.qty
                fee = self.fees.of("BUY", amount, sig.qty)
                if amount + fee > cash:
                    skipped["cash"] += 1
                    return
                cash -= amount + fee
                fees_total += fee
                positions[sig.symbol] = _Pos(sig.qty, price, fee, t, sig.reason)
                day.orders += 1
                day.buys[sig.symbol] = day.buys.get(sig.symbol, 0) + 1
                fills.append({"time": t.isoformat(), "symbol": sig.symbol, "side": "BUY", "qty": sig.qty,
                              "price": round(price, 4), "reason": sig.reason})
                return
            pos = positions.get(sig.symbol)
            if pos is None:
                return
            sell(sig.symbol, sig.price, t, sig.reason)   # 실제와 같게 매도는 하루 주문 한도와 무관

        def sell(symbol: str, signal_price: float, t: datetime, reason: str, count_order: bool = True) -> None:
            nonlocal cash, fees_total
            pos = positions.pop(symbol)
            price = signal_price * (1 - self.bt.fill_slippage_pct / 100)
            amount = price * pos.qty
            fee = self.fees.of("SELL", amount, pos.qty)
            cash += amount - fee
            fees_total += fee
            if count_order and day is not None:
                day.orders += 1
            pnl = amount - fee - pos.qty * pos.avg_price - pos.buy_fee
            kind = reason.split()[0] if reason else "기타"
            trades.append({"symbol": symbol, "qty": pos.qty, "entry_time": pos.opened.isoformat(),
                           "exit_time": t.isoformat(), "entry_price": round(pos.avg_price, 4), "exit_price": round(price, 4),
                           "pnl": round(pnl, 2), "pnl_pct": round(pnl / (pos.qty * pos.avg_price + pos.buy_fee) * 100, 3),
                           "fees": round(pos.buy_fee + fee, 2), "exit": kind, "reason": reason,
                           "hold_min": round((t - pos.opened).total_seconds() / 60)})
            ctx.sold_at[symbol] = t
            fills.append({"time": t.isoformat(), "symbol": symbol, "side": "SELL", "qty": pos.qty,
                          "price": round(price, 4), "reason": reason})

        # 종목별 분봉을 시간순으로 합친다
        total = sum(len(b) for b in data.values())
        def stream(sym: str, bars: list[Bar]):   # 함수로 감싸야 종목마다 자기 데이터를 쓴다 (generator 늦은 바인딩 방지)
            return ((b.start, sym, b) for b in bars)

        merged = heapq.merge(*(stream(s, bars) for s, bars in data.items()), key=lambda x: (x[0], x[1]))
        tp_pct, sl_pct = self.ma.take_profit_pct, self.ma.stop_loss_pct

        for n, (t, sym, bar) in enumerate(merged):
            if n % 20000 == 0:
                self.progress(n / total)
            et = t.astimezone(ET)
            d = et.date()
            session = self._session(t)
            if session is Session.CLOSED:
                continue
            if day is None or d != day.date:
                close_day()
                # 새 거래일: 전일 정규장 종가 갱신, 예산 재계산, 일일 카운터 초기화
                prev_close.update(today_reg_close)
                today_reg_close.clear()
                day = _Day(d, equity(), cash)
                if d >= self.bt.start and self.ma.capital_usage_pct > 0:   # 실제와 같게 거래일 시작 현금으로 종목당 예산
                    strategy.p = replace(self.ma, budget_per_trade_usd=cash * self.ma.capital_usage_pct / 100
                                         / self.ma.max_positions)
            trading = self.bt.start <= d <= self.bt.end
            if trading and sym not in first_price:
                first_price[sym] = bar.open
            sim_now[0] = t.timestamp()

            prices = _path(bar)
            pos = positions.get(sym)
            if pos and trading:   # 봉 안에서 익절·손절가를 지나가면 그 가격 틱을 끼워 넣는다
                tp, sl = pos.avg_price * (1 + tp_pct / 100) * (1 + 1e-9), pos.avg_price * (1 - sl_pct / 100) * (1 - 1e-9)
                path = [prices[0]]
                for a, b in zip(prices, prices[1:]):
                    for lvl in sorted((x for x in (tp, sl) if min(a, b) < x < max(a, b)), reverse=a > b):
                        path.append(lvl)
                    path.append(b)
                prices = path
            for k, price in enumerate(prices):
                tick_time = t + timedelta(seconds=min(59, k * 59 // max(1, len(prices) - 1)))
                last_price[sym] = price
                tick = _tick(sym, price, prev_close.get(sym), tick_time, bar.volume / len(prices))   # 봉 거래량을 나눠 실음
                for sig in strategy.on_tick(tick, session, ctx):
                    execute(sig, tick_time, trading)
            if session is Session.REGULAR:
                today_reg_close[sym] = bar.close

            # 손실 차단 (분봉마다 평가)
            if trading and day is not None:
                eq = equity()
                if halt != "MAX_DRAWDOWN" and self.risk.max_drawdown_pct and peak and \
                        (peak - eq) / peak * 100 >= self.risk.max_drawdown_pct:
                    halt = "MAX_DRAWDOWN"
                    liquidate = self.risk.max_drawdown_action == "liquidate"
                    halts.append({"time": t.isoformat(), "kind": "최대 낙폭", "equity": round(eq, 2)})
                elif not halt and self.risk.daily_loss_limit_pct and day.start_equity and \
                        (day.start_equity - eq) / day.start_equity * 100 >= self.risk.daily_loss_limit_pct:
                    halt = "DAILY_LOSS"
                    liquidate = self.risk.daily_loss_action == "liquidate"
                    halts.append({"time": t.isoformat(), "kind": "하루 손실", "equity": round(eq, 2)})
                target = strategy.p.daily_profit_target_pct
                if not halt and target and day.start_cash and eq - day.start_equity >= day.start_cash * target / 100:
                    halt, liquidate = "DAILY_TARGET", True   # 하루 목표 달성 → 전부 매도, 그날 종료
                    halts.append({"time": t.isoformat(), "kind": "하루 목표", "equity": round(eq, 2)})
                if liquidate and positions:
                    label = {"MAX_DRAWDOWN": "손실 차단 청산 (최대 낙폭)", "DAILY_LOSS": "손실 차단 청산 (하루 손실)",
                             "DAILY_TARGET": "목표달성 청산 (하루 목표)"}[halt]
                    for s in list(positions):
                        sell(s, last_price[s], t, label)
                if not halt:
                    liquidate = False
        close_day()

        end_time = max(b[-1].start for b in data.values())
        open_positions = [{"symbol": s, "qty": p.qty, "avg_price": round(p.avg_price, 4),
                           "last_price": round(last_price.get(s, p.avg_price), 4),
                           "pnl": round(p.qty * (last_price.get(s, p.avg_price) - p.avg_price) - p.buy_fee, 2)}
                          for s, p in positions.items()]
        self.progress(1.0)
        result = self._summary(trades, fills, equity_curve, open_positions, fees_total, skipped, list(data),
                               time.monotonic() - t0, end_time)
        result["halts"] = halts
        return result

    # ── 결과 ────────────────────────────────────────────────
    def _summary(self, trades, fills, curve, open_positions, fees_total, skipped, symbols, elapsed, end_time) -> dict:
        start_cash = self.bt.start_cash
        end_eq = curve[-1]["equity"] if curve else start_cash
        bench = curve[-1]["benchmark"] if curve else start_cash
        peak, mdd = start_cash, 0.0
        for row in curve:
            peak = max(peak, row["equity"])
            mdd = max(mdd, (peak - row["equity"]) / peak * 100)
        wins = [t["pnl"] for t in trades if t["pnl"] > 0]
        losses = [t["pnl"] for t in trades if t["pnl"] <= 0]
        by_exit: dict[str, dict] = {}
        for t in trades:
            e = by_exit.setdefault(t["exit"], {"exit": t["exit"], "count": 0, "pnl": 0.0})
            e["count"] += 1
            e["pnl"] = round(e["pnl"] + t["pnl"], 2)
        by_symbol: dict[str, dict] = {}
        for t in trades:
            e = by_symbol.setdefault(t["symbol"], {"symbol": t["symbol"], "trades": 0, "wins": 0, "pnl": 0.0})
            e["trades"] += 1
            e["wins"] += t["pnl"] > 0
            e["pnl"] = round(e["pnl"] + t["pnl"], 2)
        return {
            "period": {"start": self.bt.start.isoformat(), "end": self.bt.end.isoformat(), "days": len(curve)},
            "symbols": symbols,
            "params": {"start_cash": start_cash, "fill_slippage_pct": self.bt.fill_slippage_pct},
            "start_cash": start_cash, "end_equity": round(end_eq, 2),
            "return_pct": round((end_eq / start_cash - 1) * 100, 3),
            "benchmark_return_pct": round((bench / start_cash - 1) * 100, 3),
            "max_drawdown_pct": round(mdd, 3),
            "trades": len(trades), "wins": len(wins), "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1) if trades else None,
            "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
            "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
            "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
            "realized_pnl": round(sum(t["pnl"] for t in trades), 2),
            "unrealized_pnl": round(sum(p["pnl"] for p in open_positions), 2),
            "fees": round(fees_total, 2),
            "avg_hold_min": round(sum(t["hold_min"] for t in trades) / len(trades)) if trades else None,
            "skipped": skipped,
            "by_exit": sorted(by_exit.values(), key=lambda e: -e["count"]),
            "by_symbol": sorted(by_symbol.values(), key=lambda e: e["pnl"]),
            "equity_curve": curve, "trade_list": trades, "fills": fills[-2000:], "open_positions": open_positions,
            "data_end": end_time.astimezone(ET).isoformat(timespec="minutes"),
            "elapsed_sec": round(elapsed, 1),
        }

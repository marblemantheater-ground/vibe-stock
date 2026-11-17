"""대시보드용 상태 모음: 매매 프로그램의 객체들을 읽어 화면에 보낼 JSON 을 만든다 (읽기 전용).

매매 루프와 같은 asyncio 이벤트 루프에서 돌기 때문에 잠금 없이 읽어도 값이 섞이지 않는다.
거래일마다 바뀌는 객체(주문 관리자·손실 차단·감시 목록·웹소켓)는 attach_day()/detach_day() 로 연결한다.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from itertools import count
from typing import TYPE_CHECKING

from market.clock import KST, MarketClock, SessionWindow

if TYPE_CHECKING:
    from broker.account import AccountService
    from broker.order import OrderManager
    from core.ws_client import KisWebSocket, Tick
    from risk.guard import RiskGuard, RiskParams
    from strategy.ma_cross import MaCrossParams, MovingAverageCrossStrategy

_LOG_KEEP = 300


class EventLog(logging.Handler):
    """화면의 '최근 이벤트' 목록: 경고·오류, 알림 표시 로그, [신호]·[주문 …] 같은 태그 로그만 모은다 (시세 로그 제외)."""

    def __init__(self, keep: int = _LOG_KEEP):
        super().__init__(level=logging.INFO)
        self.items: deque[dict] = deque(maxlen=keep)
        self._ids = count(1)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(("httpx", "httpcore", "uvicorn", "web.")):
            return False
        if record.levelno >= logging.WARNING or getattr(record, "notify", False):
            return True
        msg = record.getMessage()
        return msg.startswith("[") and not msg.startswith("[시세]")

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.items.append({
                "id": next(self._ids),
                "time": datetime.fromtimestamp(record.created, KST).strftime("%H:%M:%S"),
                "level": record.levelname,
                "message": record.getMessage(),
            })
        except Exception:
            self.handleError(record)


def _r(v: float | None, n: int = 2) -> float | None:
    return None if v is None else round(v, n)


class DashboardState:
    def __init__(self, env: str, clock: MarketClock, ma_params: MaCrossParams, risk_params: RiskParams,
                 manual_symbols: set[str]):
        self.env = env
        self.clock = clock
        self.ma_params = ma_params
        self.risk_params = risk_params
        self.manual_symbols = manual_symbols
        self.started_at = datetime.now(KST)
        self.events = EventLog()
        self.phase = "starting"            # starting / waiting / trading
        self.next_window: SessionWindow | None = None
        self.dry_run: bool | None = None
        self.account: AccountService | None = None
        self.trading_date: str | None = None
        self.orders: OrderManager | None = None
        self.guard: RiskGuard | None = None
        self.strategy: MovingAverageCrossStrategy | None = None
        self.ws: KisWebSocket | None = None
        self.subscribed: dict | None = None   # HoldingWatcher.subscribed (symbol → WatchItem)
        self.targets: set[str] = set()
        self.ticks: dict[str, Tick] = {}
        self.tick_count = 0
        self.last_tick_at: datetime | None = None

    # ── 매매 루프에서 호출 ──────────────────────────────────
    def waiting(self, nxt: SessionWindow) -> None:
        self.phase, self.next_window = "waiting", nxt

    def attach_day(self, trading_date, orders, guard, strategy, ws, watcher, targets, ma_params) -> None:
        self.phase, self.next_window = "trading", None
        self.trading_date = trading_date.isoformat()
        self.orders, self.guard, self.strategy, self.ws = orders, guard, strategy, ws
        self.subscribed, self.targets, self.ma_params = watcher.subscribed, targets, ma_params
        self.dry_run = orders.dry_run
        self.ticks.clear()

    def detach_day(self) -> None:
        """거래일 종료: 실시간 객체만 떼고 마지막 주문 목록은 화면에 남긴다."""
        self.phase = "waiting"
        self.guard, self.strategy, self.ws, self.subscribed = None, None, None, None

    def on_tick(self, tick: Tick) -> None:
        self.ticks[tick.symbol] = tick
        self.tick_count += 1
        self.last_tick_at = datetime.now(KST)

    # ── 화면용 JSON ────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "status": self._status(),
            "account": self._account(),
            "risk": self._risk(),
            "positions": self._positions(),
            "orders": self._orders(),
            "watch": self._watch(),
            "events": list(self.events.items)[-120:],
            "strategy": self._strategy(),
        }

    def _status(self) -> dict:
        session = self.clock.session()
        nxt = self.next_window or (self.clock.next_window() if self.phase != "trading" else None)
        return {
            "env": self.env,
            "server_time": datetime.now(KST).isoformat(timespec="seconds"),
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "phase": self.phase,
            "session": session.value,
            "session_label": session.label,
            "next_session": None if nxt is None else {
                "label": nxt.session.label, "start": nxt.start.astimezone(KST).isoformat(timespec="minutes")},
            "trading_date": self.trading_date,
            "ws_connected": bool(self.ws and self.ws.connected.is_set()),
            "last_tick_at": self.last_tick_at.isoformat(timespec="seconds") if self.last_tick_at else None,
            "tick_count": self.tick_count,
            "dry_run": self.dry_run,
        }

    def _price(self, symbol: str, fallback: float) -> float:
        t = self.ticks.get(symbol)
        return t.price if t and t.price > 0 else fallback

    def _account(self) -> dict | None:
        snap = self.account.snapshot if self.account else None
        if snap is None:
            return None
        eq = self.guard.equity() if self.guard else snap.orderable_usd + sum(
            h.quantity * self._price(s, h.current_price) for s, h in snap.holdings.items())
        state = self.guard.state if self.guard else {}
        start = state.get("day_start_equity")
        return {
            "orderable_usd": _r(snap.orderable_usd),
            "exchange_rate": _r(snap.exchange_rate),
            "equity": _r(eq),
            "equity_krw": _r(eq * snap.exchange_rate, 0) if eq and snap.exchange_rate else None,
            "day_start_equity": _r(start),
            "day_pnl": _r(eq - start) if eq and start else None,
            "day_pnl_pct": _r((eq / start - 1) * 100) if eq and start else None,
            "holdings_eval": _r(sum(h.quantity * self._price(s, h.current_price) for s, h in snap.holdings.items())),
            "fetched_at": snap.fetched_at.astimezone(KST).isoformat(timespec="seconds"),
        }

    def _risk(self) -> dict:
        p = self.risk_params
        out = {"daily_limit_pct": p.daily_loss_limit_pct, "max_drawdown_pct": p.max_drawdown_pct,
               "daily_action": p.daily_loss_action, "drawdown_action": p.max_drawdown_action,
               "halt": None, "daily_loss_pct": None, "drawdown_pct": None, "peak_equity": None}
        if not self.guard:
            return out
        state = self.guard.state
        if (halt := state.get("halt")):
            out["halt"] = {"kind": halt["kind"], "label": self.guard.halt_kind.label,
                           "reason": halt.get("reason", ""), "since": halt.get("since", "")}
        eq = self.guard.equity()
        start, peak = state.get("day_start_equity"), state.get("peak_equity")
        out["peak_equity"] = _r(peak)
        if eq and start:
            out["daily_loss_pct"] = _r(max((start - eq) / start * 100, 0.0))
        if eq and peak:
            out["drawdown_pct"] = _r(max((peak - eq) / peak * 100, 0.0))
        return out

    def _positions(self) -> list[dict]:
        snap = self.account.snapshot if self.account else None
        if snap is None:
            return []
        tp, sl = self.ma_params.take_profit_pct, self.ma_params.stop_loss_pct
        rows = []
        for sym, h in sorted(snap.holdings.items()):
            price = self._price(sym, h.current_price)
            pnl_pct = (price / h.avg_price - 1) * 100 if h.avg_price else 0.0
            tick = self.ticks.get(sym)
            rows.append({
                "symbol": sym, "name": h.name, "exchange": h.exchange, "qty": h.quantity,
                "orderable_qty": h.orderable_qty, "avg_price": _r(h.avg_price, 4), "price": _r(price, 4),
                "change_rate": _r(tick.change_rate) if tick else None,
                "eval": _r(h.quantity * price), "pnl": _r((price - h.avg_price) * h.quantity),
                "pnl_pct": _r(pnl_pct),
                "tp_price": _r(h.avg_price * (1 + tp / 100), 4), "sl_price": _r(h.avg_price * (1 - sl / 100), 4),
                "to_tp_pct": _r(tp - pnl_pct), "to_sl_pct": _r(pnl_pct + sl),
                "live": tick is not None,
            })
        return rows

    def _orders(self) -> list[dict]:
        if not self.orders:
            return []
        return [{
            "time": o.placed_at.astimezone(KST).strftime("%H:%M:%S"),
            "side": o.side.value, "symbol": o.symbol, "qty": o.qty, "limit_price": _r(o.limit_price, 4),
            "status": o.status.value, "status_label": o.status.label, "filled_qty": o.filled_qty,
            "avg_fill_price": _r(o.avg_fill_price, 4), "reason": o.signal.reason, "message": o.message,
            "odno": o.odno, "external": o.adopted,
        } for o in reversed(self.orders.orders)]

    def _watch(self) -> list[dict]:
        if not self.subscribed:
            return []
        snap = self.account.snapshot if self.account else None
        held = set(snap.holdings) if snap else set()
        pending = {o.symbol for o in self.orders.open_orders()} if self.orders else set()
        rows = []
        for sym, item in self.subscribed.items():
            source = ("보유" if sym in held else "미체결" if sym in pending
                      else "관심" if sym in self.manual_symbols else "선정")
            tick = self.ticks.get(sym)
            ind = self.strategy.indicators(sym) if self.strategy else None
            rows.append({
                "symbol": sym, "exchange": item.exchange, "source": source,
                "price": _r(tick.price, 4) if tick else None,
                "change_rate": _r(tick.change_rate) if tick else None,
                "strength": _r(tick.strength) if tick else None,
                "volume": tick.cum_volume if tick else None,
                "ma_short": _r(ind["ma_short"], 4) if ind else None,
                "ma_long": _r(ind["ma_long"], 4) if ind else None,
                "trend": ind["trend"] if ind else None,
                "bars": ind["bars"] if ind else 0,
                "ready": bool(ind and ind["ready"]),
                "buy_target": sym in self.targets,
            })
        return rows

    def _strategy(self) -> dict:
        p = self.ma_params
        return {
            "bar_minutes": p.bar_minutes, "short_window": p.short_window, "long_window": p.long_window,
            "min_change_rate": p.min_change_rate, "max_change_rate": p.max_change_rate,
            "take_profit_pct": p.take_profit_pct, "stop_loss_pct": p.stop_loss_pct,
            "budget_per_trade_usd": _r(p.budget_per_trade_usd), "max_positions": p.max_positions,
            "sessions": [s.label for s in p.sessions],
        }


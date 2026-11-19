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

from core.notifier import NOTIFY
from market.clock import KST, MarketClock, SessionWindow
from risk.guard import HaltKind, RiskStore, apply_command_offline

if TYPE_CHECKING:
    from broker.account import AccountService
    from broker.order import OrderManager
    from config.live import LiveConfig
    from config.presets import PresetStore
    from strategy.positions import PositionBook
    from core.ws_client import KisWebSocket, Tick
    from risk.guard import RiskGuard, RiskParams
    from strategy.ma_cross import MaCrossParams, MovingAverageCrossStrategy

_LOG_KEEP = 300
log = logging.getLogger(__name__)


class EventLog(logging.Handler):
    """화면의 '최근 이벤트' 목록: 경고·오류, 알림 표시 로그, [신호]·[주문 …] 같은 태그 로그만 모은다 (시세 로그 제외)."""

    def __init__(self, keep: int = _LOG_KEEP):
        super().__init__(level=logging.INFO)
        self.items: deque[dict] = deque(maxlen=keep)
        self._ids = count(1)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(("httpx", "httpcore", "uvicorn")):
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
                 manual_symbols: set[str], risk_store: RiskStore | None = None):
        self.env = env
        self.clock = clock
        self.ma_params = ma_params
        self.risk_params = risk_params
        self.manual_symbols = manual_symbols
        self.risk_store = risk_store
        self.config: LiveConfig | None = None   # 설정 화면 (없으면 읽기 전용)
        self.presets: PresetStore | None = None  # 거래 옵션
        self.book: PositionBook | None = None    # 보유 종목별 산 때 옵션 기준
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
        if self.config:
            self.ma_params = self.config.ma

    def on_tick(self, tick: Tick) -> None:
        self.ticks[tick.symbol] = tick
        self.tick_count += 1
        self.last_tick_at = datetime.now(KST)

    def on_config(self, cfg: LiveConfig) -> None:
        """설정이 바뀌면 화면 계산(익절·손절가, 손실 한도 게이지)에 쓰는 값도 바꾼다.
        거래일 중 전략 값(종목당 예산 등)은 매매 루프가 따로 넣어 준다."""
        self.risk_params = cfg.risk
        if self.strategy is None:
            self.ma_params = cfg.ma

    def settings_view(self) -> dict:
        if self.config is None:
            return {"editable": False, "groups": [], "file": None}
        self.config.poll(force=True)
        return {"editable": True, "env": self.env, "trading": self.phase == "trading",
                "preset": self.presets.active_name if self.presets else None, **self.config.describe()}

    def update_settings(self, values: dict) -> dict:
        if self.config is None:
            return {"ok": False, "message": "설정 화면을 쓸 수 없습니다"}
        try:
            changes = self.config.update(values)
        except (ValueError, OSError) as e:
            return {"ok": False, "message": str(e)}
        if not changes:
            return {"ok": True, "message": "바뀐 설정이 없습니다", "changes": [], **self.settings_view()}
        later = [c for c in changes if c.field.apply != "now"]
        msg = f"설정 {len(changes)}개를 저장했습니다. "
        if self.phase == "trading":
            msg += "지금 매매에 바로 적용했습니다" if not later else                 f"{len(changes) - len(later)}개는 바로 적용, {', '.join(c.field.label for c in later)}은(는) 다음 거래일부터 적용됩니다"
        else:
            msg += "다음 세션부터 적용됩니다"
        return {"ok": True, "message": msg, "changes": [c.text() for c in changes], **self.settings_view()}

    # ── 화면에서 보낸 조작 ──────────────────────────────────
    async def control(self, command: str) -> dict:
        """일시정지(pause) / 재개(resume) / 긴급 정지(kill).
        거래일 중이면 손실 차단 장치에 바로 적용, 장 외 시간이면 상태 파일에 저장 → 다음 세션 시작 시 적용."""
        label = {"kill": "긴급 정지", "pause": "일시정지", "resume": "재개"}[command]
        if self.guard:
            before = self.guard.halt_kind
            if command == "kill":
                changed = await self.guard.trigger(HaltKind.KILL, "대시보드에서 긴급 정지")
            elif command == "pause":
                changed = await self.guard.trigger(HaltKind.PAUSE, "대시보드에서 일시정지")
            else:
                changed = before is not None
                self.guard.resume()
            if not changed:
                cur = self.guard.halt_kind
                msg = f"이미 {cur.label} 상태입니다" if cur else "매매 중지 상태가 아닙니다"
                return {"ok": False, "message": msg}
            msg = {"kill": "긴급 정지했습니다. 미체결 취소와 보유 종목 매도를 진행합니다",
                   "pause": "일시정지했습니다. 신규 매수를 멈추고 매도는 계속합니다",
                   "resume": ("일시정지를 해제하고 매매를 재개했습니다" if before is HaltKind.PAUSE
                              else f"{before.label if before else ''} 중지를 해제하고 매매를 재개했습니다")}[command]
            return {"ok": True, "message": msg}
        if self.risk_store is None:
            return {"ok": False, "message": "손실 차단 상태 파일을 쓸 수 없습니다"}
        result = apply_command_offline(self.risk_store, command)
        later = " 다음 세션이 시작되면 청산합니다." if command == "kill" else ""
        log.warning("[대시보드 %s] 장 외 시간 → %s.%s", label, result, later, extra=NOTIFY)
        ok = not result.startswith(("이미", "해제할"))
        return {"ok": ok, "message": result + "." + later}

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
               "halt": None, "daily_loss_pct": None, "drawdown_pct": None, "peak_equity": None,
               "target": None, "limits": self._limits()}
        state = self.guard.state if self.guard else (self.risk_store.load() if self.risk_store else {})
        if (halt := state.get("halt")):
            out["halt"] = {"kind": halt["kind"], "label": HaltKind(halt["kind"]).label,
                           "reason": halt.get("reason", ""), "since": halt.get("since", "")}
        if not self.guard:
            return out
        eq = self.guard.equity()
        start, peak = state.get("day_start_equity"), state.get("peak_equity")
        out["peak_equity"] = _r(peak)
        if eq and start:
            out["daily_loss_pct"] = _r(max((start - eq) / start * 100, 0.0))
        if eq and peak:
            out["drawdown_pct"] = _r(max((peak - eq) / peak * 100, 0.0))
        if (prog := self.guard.target_progress(eq)):
            out["target"] = {"pct": prog["pct"], "goal": _r(prog["goal"]), "gain": _r(prog["gain"]),
                             "ratio": _r(prog["ratio"], 4), "start_cash": _r(prog["start_cash"]),
                             "done_today": state.get("target_done_day") == state.get("day")}
        return out

    def _limits(self) -> dict:
        """하루 매수 횟수·보유 종목 수 (거래 옵션 한도 대비)."""
        p = self.ma_params
        snap = self.account.snapshot if self.account else None
        held = {s for s, h in snap.holdings.items() if h.quantity > 0} if snap else set()
        pending = self.orders.pending_buy_symbols() if self.orders else set()
        buys = sum(self.orders.buys_today(s) for s in {o.symbol for o in self.orders.orders}) if self.orders else 0
        return {"buys_today": buys, "max_buys": p.max_buys_per_day, "positions": len(held | pending),
                "max_positions": p.max_positions, "target_pct": p.daily_profit_target_pct}

    def _positions(self) -> list[dict]:
        snap = self.account.snapshot if self.account else None
        if snap is None:
            return []
        rows = []
        current = self.presets.active_name if self.presets else ""
        for sym, h in sorted(snap.holdings.items()):
            rule = self.book.get(sym) if self.book else None   # 산 때 옵션 기준 (없으면 지금 설정)
            tp = rule.take_profit_pct if rule else self.ma_params.take_profit_pct
            sl = rule.stop_loss_pct if rule else self.ma_params.stop_loss_pct
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
                "preset": rule.preset_name if rule else current, "preset_recorded": rule is not None,
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
            "preset": self.presets.active_name if self.presets else None,
        }


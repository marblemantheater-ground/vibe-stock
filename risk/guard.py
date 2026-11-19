"""손실 차단 장치: 하루 손실 한도 · 최대 낙폭 한도 · 긴급 정지(킬 스위치).

총자산 = 주문가능 USD + 보유 종목 평가금액 (정산 보고서와 같은 기준). 보유 종목은 실시간 체결가로 평가한다.

1) 하루 손실 한도: 그날 시작 총자산 대비 daily_loss_limit_pct% 이상 손실이 confirm_sec 동안 이어지면 발동
   → 그날 신규 매수 중지 (다음 거래일 자동 해제)
2) 최대 낙폭 한도: 고점(일별 마감 총자산의 최고치) 대비 max_drawdown_pct% 이상 하락하면 발동
   → 사용자가 해제(--resume)할 때까지 신규 매수 중지 (재시작해도 유지)
3) 긴급 정지: python main.py --kill (kill.bat, 대시보드 버튼)
   → 미체결 주문 전부 취소 + 보유 종목 전부 매도 + 신규 매수 중지 (해제할 때까지 유지)
4) 일시정지: python main.py --pause (대시보드 버튼)
   → 신규 매수만 중지, 매도는 계속 (재개할 때까지 유지)

발동 시 동작(action)
  "block_buys": 신규 매수만 막는다. 익절·손절·데드크로스 매도는 계속 동작해 보유 종목을 지킨다.
  "liquidate" : 위 + 미체결 전부 취소 + 보유 전부 매도 (긴급 정지와 같은 처리)

상태는 data/risk/{env}.json 에 저장된다. 실행 중인 프로그램에는 명령 파일(data/risk/{env}.kill / .resume)로
전달한다 (다른 창에서 --kill 을 실행하면 몇 초 안에 반영).
"""
from __future__ import annotations

import json
import logging
import os
import time
import tomllib
from dataclasses import dataclass, fields
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Callable

from broker.account import AccountService, AccountSnapshot
from broker.order import OrderManager
from config.toggles import effective as _effective, parse_off, uses as _uses
from core.exceptions import KisError
from core.notifier import NOTIFY
from market.clock import KST
from strategy.base import Side, Signal

log = logging.getLogger(__name__)

_ACTIONS = ("block_buys", "liquidate")
_CHECK_INTERVAL_SEC = 0.5     # 한도 검사 주기 (틱이 몰려도 이 간격으로만)
_COMMAND_POLL_SEC = 2.0       # 명령 파일 확인 주기
_LIQ_STEP_SEC = 5.0           # 청산 주문 점검 주기
_LIQ_REFRESH_SEC = 30.0       # 청산 중 잔고 재조회 주기 (현재가·남은 수량 갱신)
_WARN_RATIO = 0.5             # 한도의 절반에 도달하면 미리 알림


class HaltKind(str, Enum):
    PAUSE = "PAUSE"
    DAILY_LOSS = "DAILY_LOSS"
    DAILY_TARGET = "DAILY_TARGET"
    MAX_DRAWDOWN = "MAX_DRAWDOWN"
    KILL = "KILL"

    @property
    def label(self) -> str:
        return {"PAUSE": "일시정지", "DAILY_LOSS": "하루 손실 한도", "DAILY_TARGET": "하루 목표 달성", "MAX_DRAWDOWN": "최대 낙폭 한도",
                "KILL": "긴급 정지"}[self.value]

    @property
    def severity(self) -> int:
        return {"PAUSE": 0, "DAILY_LOSS": 1, "DAILY_TARGET": 1, "MAX_DRAWDOWN": 2, "KILL": 3}[self.value]

    @property
    def daily(self) -> bool:
        """그날만 유효 (다음 거래일 자동 해제)."""
        return self in (HaltKind.DAILY_LOSS, HaltKind.DAILY_TARGET)


RISK_TOGGLES = {"daily_loss": "하루 손실 한도", "max_drawdown": "최대 낙폭 한도"}
_RISK_ZERO_OFF = {"daily_loss": "daily_loss_limit_pct", "max_drawdown": "max_drawdown_pct"}


@dataclass(frozen=True)
class RiskParams:
    daily_loss_limit_pct: float = 5.0      # 0 = 끔
    daily_loss_action: str = "block_buys"
    max_drawdown_pct: float = 15.0         # 0 = 끔
    max_drawdown_action: str = "block_buys"
    confirm_sec: float = 10.0              # 한도 초과가 이 시간 동안 이어져야 발동 (순간적인 이상 시세 무시)
    liquidation_slippage_pct: float = 1.0  # 청산 매도 지정가 = 현재가 - 1% (빨리 체결되도록 일반 주문보다 넓게)
    off: tuple[str, ...] = ()              # 꺼 둔 한도 (RISK_TOGGLES 이름, 값은 남겨 둠)

    @classmethod
    def from_toml(cls, path: Path) -> RiskParams:
        with path.open("rb") as f:
            return cls.from_dict(tomllib.load(f).get("risk", {}))

    @classmethod
    def from_dict(cls, raw: dict) -> RiskParams:
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"[risk]: 알 수 없는 설정 {sorted(unknown)}")
        raw = dict(raw)
        if "off" in raw:
            raw["off"] = parse_off(raw["off"], RISK_TOGGLES, "risk")
        p = cls(**raw)
        if not 0 <= p.daily_loss_limit_pct < 100 or not 0 <= p.max_drawdown_pct < 100:
            raise ValueError("[risk] daily_loss_limit_pct, max_drawdown_pct 는 0 이상 100 미만이어야 합니다 (0 = 끔)")
        for name in ("daily_loss_action", "max_drawdown_action"):
            if getattr(p, name) not in _ACTIONS:
                raise ValueError(f"[risk] {name} 는 {' / '.join(_ACTIONS)} 중 하나여야 합니다")
        if p.confirm_sec < 0 or not 0 <= p.liquidation_slippage_pct < 10:
            raise ValueError("[risk] confirm_sec >= 0, 0 <= liquidation_slippage_pct < 10 이어야 합니다")
        return p

    def uses(self, name: str) -> bool:
        return _uses(self, name, _RISK_ZERO_OFF)

    def effective(self) -> RiskParams:
        """매매에 쓰는 값: 꺼진 한도는 0 (= 끔)."""
        return _effective(self, {n: {k: 0.0} for n, k in _RISK_ZERO_OFF.items()})

    def action_for(self, kind: HaltKind) -> str:
        if kind in (HaltKind.KILL, HaltKind.DAILY_TARGET):   # 목표 달성: 전부 팔아 수익 확정
            return "liquidate"
        if kind is HaltKind.PAUSE:
            return "block_buys"
        return self.daily_loss_action if kind is HaltKind.DAILY_LOSS else self.max_drawdown_action


class RiskStore:
    """손실 차단 상태 파일 + 실행 중인 프로그램에 보내는 명령 파일."""

    def __init__(self, root: Path, env: str):
        self.path = root / f"{env}.json"
        self._flags = {"kill": root / f"{env}.kill", "pause": root / f"{env}.pause", "resume": root / f"{env}.resume"}

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            log.error("손실 차단 상태 파일을 읽지 못함 (%s) → 새로 시작: %s", self.path, e)
            return {}

    def save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def send(self, command: str) -> Path:
        flag = self._flags[command]
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(datetime.now(KST).isoformat(timespec="seconds"), encoding="utf-8")
        return flag

    def take_commands(self) -> list[str]:
        """도착한 명령을 읽고 지운다. 긴급 정지가 해제보다 우선 (둘 다 있으면 정지만)."""
        found = []
        for cmd, flag in self._flags.items():
            if flag.exists():
                try:
                    flag.unlink()
                except OSError:
                    continue
                found.append(cmd)
        return ["kill"] if "kill" in found else found


def _now_text() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def apply_command_offline(store: RiskStore, command: str) -> str:
    """매매 프로그램이 꺼져 있을 때 상태 파일을 직접 고친다 (호출자는 매매 프로그램 잠금을 쥐고 있어야 함)."""
    state = store.load()
    halt = state.get("halt")
    if command == "kill":
        state["halt"] = {"kind": HaltKind.KILL.value, "reason": "사용자 긴급 정지 명령", "since": _now_text(),
                         "day": state.get("day")}
        store.save(state)
        return "긴급 정지 상태로 저장했습니다"
    if command == "pause":
        if halt:
            return f"이미 매매 중지 상태입니다 ({HaltKind(halt['kind']).label})"
        state["halt"] = {"kind": HaltKind.PAUSE.value, "reason": "사용자 일시정지", "since": _now_text(),
                         "day": state.get("day")}
        store.save(state)
        return "일시정지 상태로 저장했습니다 (신규 매수 중지)"
    if not halt:
        return "해제할 매매 중지 상태가 없습니다"
    kind = HaltKind(halt["kind"])
    state["halt"] = None
    if kind is HaltKind.MAX_DRAWDOWN:
        state["peak_equity"] = None        # 다음 시작 때의 총자산을 새 고점으로
    elif kind is HaltKind.DAILY_LOSS:
        state["day_start_equity"] = None   # 다음 시작 때의 총자산을 오늘 기준점으로
    elif kind is HaltKind.DAILY_TARGET:
        state["target_done_day"] = state.get("day")   # 오늘은 목표로 다시 멈추지 않음
    store.save(state)
    return "일시정지를 해제했습니다" if kind is HaltKind.PAUSE else f"{kind.label} 중지를 해제했습니다"


def describe_state(state: dict) -> list[str]:
    halt = state.get("halt")
    peak = state.get("peak_equity")
    lines = [
        f"매매 상태: {'중지 — ' + HaltKind(halt['kind']).label + ' (' + halt.get('reason', '') + ', ' + halt.get('since', '') + ')' if halt else '정상'}",
        f"고점 총자산: {'$' + format(peak, ',.2f') if peak else '-'} ({state.get('peak_date') or '-'})",
        f"오늘({state.get('day') or '-'}) 시작 총자산: "
        f"{'$' + format(state['day_start_equity'], ',.2f') if state.get('day_start_equity') else '-'}",
    ]
    return lines


class RiskGuard:
    """매매 루프에서 1초마다 check() 를 호출한다. 주문 관리자의 매수를 막는 관문(buy_gate)도 제공."""

    def __init__(self, params: RiskParams, store: RiskStore, account: AccountService, orders: OrderManager,
                 trading_date: date, order_timeout_sec: float = 60.0):
        self.p = params
        self._store = store
        self._account = account
        self._orders = orders
        self._today = trading_date.isoformat()
        self._liq_resubmit_sec = order_timeout_sec + 5
        self.state = store.load()
        self._prices: dict[str, float] = {}
        self._breach_since: dict[HaltKind, float] = {}
        self._warned: set[HaltKind] = set()
        self._snap_ref: AccountSnapshot | None = None
        self._snap_reserved = 0.0
        self._last_cmd_poll = 0.0
        self._last_check = -1e9
        self._last_liq_step = 0.0
        self._last_liq_refresh = 0.0
        self._last_liq_order: dict[str, float] = {}
        self._liq_done_logged = False
        orders.buy_gate = self.buy_block_reason
        self.target_pct: Callable[[], float] = lambda: 0.0   # 하루 목표 수익 % (거래 옵션 값, 0 = 끔)
        self.sell_meta: Callable[[str], dict | None] = lambda _sym: None   # 청산 매도에 붙일 거래 옵션 정보

    def set_params(self, params: RiskParams, order_timeout_sec: float) -> None:
        """설정 화면·파일에서 바뀐 한도를 바로 반영 (이미 발동한 중지는 그대로 유지)."""
        self.p = params
        self._liq_resubmit_sec = order_timeout_sec + 5

    # ── 상태 ────────────────────────────────────────────────
    @property
    def halt(self) -> dict | None:
        return self.state.get("halt")

    @property
    def halt_kind(self) -> HaltKind | None:
        return HaltKind(self.halt["kind"]) if self.halt else None

    def buy_block_reason(self) -> str | None:
        if not self.halt:
            return None
        return f"매매 중지 중 — {self.halt_kind.label}: {self.halt.get('reason', '')}"

    def _save(self) -> None:
        try:
            self._store.save(self.state)
        except OSError as e:
            log.error("손실 차단 상태 저장 실패: %s", e)

    @property
    def liquidated(self) -> bool:
        """청산 모드에서 보유 종목과 미체결 주문이 모두 없어졌는지."""
        return self._liq_done_logged

    async def start(self, start_equity: float | None, start_cash: float | None = None) -> None:
        """거래일 시작: 기준점(오늘 시작 총자산·예수금·고점)을 정하고 남아 있는 중지 상태를 이어받는다."""
        s = self.state
        if s.get("day") != self._today:
            s["day"], s["day_start_equity"], s["day_start_cash"] = self._today, start_equity, start_cash
            s["target_done_day"] = None
            if self.halt_kind and self.halt_kind.daily:
                log.info("[손실 차단] 새 거래일 → %s 중지 자동 해제", self.halt_kind.label, extra=NOTIFY)
                s["halt"] = None
        else:   # 같은 날 재시작이면 처음 기준점 유지
            if not s.get("day_start_equity"):
                s["day_start_equity"] = start_equity
            if not s.get("day_start_cash"):
                s["day_start_cash"] = start_cash
        if start_equity and (not s.get("peak_equity") or start_equity > s["peak_equity"]):
            s["peak_equity"], s["peak_date"] = start_equity, self._today
        self._save()

        p = self.p
        log.info("손실 차단: 하루 손실 %s, 최대 낙폭 %s, 긴급 정지 = run.bat --kill",
                 f"{p.daily_loss_limit_pct:g}%({p.daily_loss_action})" if p.daily_loss_limit_pct else "끔",
                 f"{p.max_drawdown_pct:g}%({p.max_drawdown_action})" if p.max_drawdown_pct else "끔")
        for line in describe_state(s):
            log.info("  %s", line)
        if self.halt:
            log.warning("[손실 차단] 이전 매매 중지 상태 유지: %s → 해제하려면 run.bat --resume",
                        self.buy_block_reason(), extra=NOTIFY)
        await self._consume_commands_async()   # 꺼져 있는 동안 도착한 명령
        if self.halt and self.p.action_for(self.halt_kind) == "liquidate":
            await self._orders.cancel_all_open(include_adopted=True)   # 청산 매도가 막히지 않게

    def on_settled(self, end_equity: float | None) -> None:
        """장 마감 정산 후: 마감 총자산이 고점보다 높으면 고점 갱신."""
        if end_equity and end_equity > (self.state.get("peak_equity") or 0):
            self.state["peak_equity"], self.state["peak_date"] = end_equity, self._today
            log.info("[손실 차단] 총자산 고점 갱신: $%s", f"{end_equity:,.2f}")
        self._save()

    # ── 평가 ────────────────────────────────────────────────
    def on_tick(self, symbol: str, price: float) -> None:
        if price > 0:
            self._prices[symbol] = price

    def equity(self) -> float | None:
        """총자산(실시간): 주문가능 USD + 잔고 조회 시점에 걸려 있던 매수 주문 예약금 + 보유 수량 × 최신 체결가.
        미체결 매수는 주문가능금액에서 빠져 있으므로 다시 더해 '주문을 냈을 뿐인데 손실로 보이는' 일을 막는다."""
        snap = self._account.snapshot
        if snap is None:
            return None
        if snap is not self._snap_ref:   # 잔고가 새로 조회됐을 때 그 시점의 매수 예약금을 기록
            self._snap_ref = snap
            self._snap_reserved = sum(o.remaining * o.limit_price for o in self._orders.open_orders()
                                      if o.side is Side.BUY)
        holdings = sum(h.quantity * self._prices.get(sym, h.current_price) for sym, h in snap.holdings.items())
        return snap.orderable_usd + self._snap_reserved + holdings

    async def check(self, tradable: bool = True) -> None:
        """명령 확인 → 한도 검사 → (청산 중이면) 청산 주문. tradable=False 면 주문은 내지 않는다."""
        now = time.monotonic()
        if now - self._last_check < _CHECK_INTERVAL_SEC:
            return
        self._last_check = now
        if now - self._last_cmd_poll >= _COMMAND_POLL_SEC:
            self._last_cmd_poll = now
            await self._consume_commands_async()

        eq = self.equity()
        if eq:
            await self._evaluate(eq, now)
        if self.halt and self.p.action_for(self.halt_kind) == "liquidate" and tradable:
            if now - self._last_liq_step >= _LIQ_STEP_SEC:
                self._last_liq_step = now
                await self._liquidate_step(now)

    async def _evaluate(self, eq: float, now: float) -> None:
        checks: list[tuple[HaltKind, float, float, str]] = []
        start = self.state.get("day_start_equity")
        if self.p.daily_loss_limit_pct and start:
            loss = (start - eq) / start * 100
            checks.append((HaltKind.DAILY_LOSS, loss, self.p.daily_loss_limit_pct,
                           f"오늘 시작 ${start:,.2f} → 현재 ${eq:,.2f} ({-loss:+.2f}%)"))
        peak = self.state.get("peak_equity")
        if self.p.max_drawdown_pct and peak:
            dd = (peak - eq) / peak * 100
            checks.append((HaltKind.MAX_DRAWDOWN, dd, self.p.max_drawdown_pct,
                           f"고점 ${peak:,.2f} → 현재 ${eq:,.2f} ({-dd:+.2f}%)"))

        await self._check_target(eq, now)
        for kind, value, limit, text in checks:
            if value < limit:
                self._breach_since.pop(kind, None)
                if value >= limit * _WARN_RATIO and kind not in self._warned:
                    self._warned.add(kind)
                    log.warning("[손실 차단 주의] %s의 %.0f%% 도달: %s (한도 %g%%)", kind.label,
                                value / limit * 100, text, limit, extra=NOTIFY)
                continue
            if self.halt and self.halt_kind.severity >= kind.severity:
                continue
            since = self._breach_since.setdefault(kind, now)
            if now - since >= self.p.confirm_sec:
                await self.trigger(kind, f"{text}, 한도 {limit:g}%")

    def target_progress(self, eq: float | None = None) -> dict | None:
        """하루 목표: 오늘 손익(총자산 변화 = 실현 + 평가) ÷ (하루 시작 예수금 × 목표 %)."""
        pct = self.target_pct()
        cash, start = self.state.get("day_start_cash"), self.state.get("day_start_equity")
        eq = self.equity() if eq is None else eq
        if not pct or not cash or not start or eq is None:
            return None
        goal = cash * pct / 100
        return {"pct": pct, "start_cash": cash, "goal": goal, "gain": eq - start, "ratio": (eq - start) / goal}

    async def _check_target(self, eq: float, now: float) -> None:
        prog = self.target_progress(eq)
        if prog is None or self.state.get("target_done_day") == self._today:
            return
        if prog["gain"] < prog["goal"]:
            self._breach_since.pop(HaltKind.DAILY_TARGET, None)
            return
        if self.halt and self.halt_kind.severity >= HaltKind.DAILY_TARGET.severity:
            return
        since = self._breach_since.setdefault(HaltKind.DAILY_TARGET, now)
        if now - since >= self.p.confirm_sec:
            await self.trigger(HaltKind.DAILY_TARGET,
                               f"오늘 손익 ${prog['gain']:+,.2f} ≥ 목표 ${prog['goal']:,.2f} "
                               f"(시작 예수금 ${prog['start_cash']:,.2f}의 {prog['pct']:g}%)")

    # ── 발동 · 해제 ─────────────────────────────────────────
    async def trigger(self, kind: HaltKind, reason: str) -> bool:
        """매매 중지 발동. 이미 같거나 더 높은 단계로 중지 중이면 아무것도 하지 않고 False."""
        if self.halt and self.halt_kind.severity >= kind.severity:
            if kind in (HaltKind.KILL, HaltKind.PAUSE):
                log.info("[%s] 이미 %s 상태입니다", kind.label, self.halt_kind.label)
            return False
        self.state["halt"] = {"kind": kind.value, "reason": reason, "since": _now_text(), "day": self._today}
        self._save()
        self._breach_since.clear()
        self._liq_done_logged = False
        action = self.p.action_for(kind)
        until = "오늘 장 마감까지 (다음 거래일 자동 재개)" if kind.daily else "재개(--resume)할 때까지"
        what = "미체결 전부 취소 + 보유 전부 매도 + 신규 매수 중지" if action == "liquidate" else "신규 매수 중지 (매도는 계속)"
        level = logging.WARNING if kind in (HaltKind.PAUSE, HaltKind.DAILY_TARGET) else logging.ERROR
        log.log(level, "[%s 발동] %s → %s, %s", kind.label, reason, what, until, extra=NOTIFY)

        if action == "liquidate":
            await self._orders.cancel_all_open(include_adopted=True)
            self._last_liq_step = 0.0
        else:
            for o in self._orders.open_orders():
                if o.side is Side.BUY and not o.adopted:
                    await self._orders.cancel(o)
        return True

    def resume(self) -> None:
        if not self.halt:
            log.info("[손실 차단] 해제 명령: 중지 상태가 아닙니다")
            return
        kind = self.halt_kind
        self.state["halt"] = None
        eq = self.equity()
        if kind is HaltKind.MAX_DRAWDOWN:
            self.state["peak_equity"], self.state["peak_date"] = eq, self._today
        elif kind is HaltKind.DAILY_LOSS:
            self.state["day_start_equity"] = eq
        elif kind is HaltKind.DAILY_TARGET:
            self.state["target_done_day"] = self._today   # 사용자가 재개 → 오늘은 목표로 다시 멈추지 않음
        self._save()
        self._breach_since.clear()
        self._warned.clear()
        extra = (f" (기준 총자산을 현재 ${eq:,.2f} 로 재설정)"
                 if eq and kind in (HaltKind.DAILY_LOSS, HaltKind.MAX_DRAWDOWN) else "")
        log.warning("[손실 차단 해제] %s 중지 해제 → 매매 재개%s", kind.label, extra, extra=NOTIFY)

    async def _consume_commands_async(self) -> None:
        cmds = self._store.take_commands()
        if "kill" in cmds:
            await self.trigger(HaltKind.KILL, "사용자 긴급 정지 명령")
            return
        if "resume" in cmds:
            self.resume()
        if "pause" in cmds:
            await self.trigger(HaltKind.PAUSE, "사용자 일시정지")

    # ── 청산 ────────────────────────────────────────────────
    async def _liquidate_step(self, now: float) -> None:
        if now - self._last_liq_refresh >= _LIQ_REFRESH_SEC and not self._orders.open_orders():
            self._last_liq_refresh = now
            try:
                await self._account.refresh()
            except KisError as e:
                log.warning("[청산] 잔고 조회 실패 (다음 주기 재시도): %s", e)
        snap = self._account.snapshot
        remaining = {s: h for s, h in (snap.holdings.items() if snap else []) if h.quantity > 0}
        if not remaining:
            if not self._liq_done_logged and not self._orders.open_orders():
                self._liq_done_logged = True
                log.warning("[%s] 청산 완료: 보유 종목 없음. 신규 매수는 계속 중지됩니다", self.halt_kind.label,
                            extra=NOTIFY)
            return
        self._liq_done_logged = False
        for sym, h in remaining.items():
            if self._orders.open_orders(sym):
                continue   # 청산 주문 진행 중 (60초 미체결 시 자동 취소 → 아래에서 새 가격으로 다시 냄)
            if now - self._last_liq_order.get(sym, -1e9) < self._liq_resubmit_sec:
                continue
            qty = int(h.orderable_qty)
            price = self._prices.get(sym, h.current_price)
            if qty < 1 or price <= 0:
                continue
            self._last_liq_order[sym] = now
            signal = Signal(sym, h.exchange, Side.SELL, qty, price, f"{self.halt_kind.label} 청산", meta=self.sell_meta(sym))
            log.warning("[청산 매도] %s", signal)
            self._orders.submit(signal, force=True, slippage_pct=self.p.liquidation_slippage_pct)

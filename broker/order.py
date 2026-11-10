"""주문 관리자: 신호 → 사전 검증 → 지정가 주문 → 체결 추적 → 잔고 동기화.

경쟁 상태(race condition) 방지 원칙
- submit() 의 사전 검증과 주문 등록은 동기 코드라 다른 코루틴이 끼어들 수 없다.
  → 같은 종목에 미체결 주문이 있으면 새 주문을 거부해 중복 주문을 막는다.
- 실제 전송과 체결내역 조회는 같은 락으로 직렬화한다.
  → 주문번호를 받기 전에 조회 결과가 먼저 반영되어 주문을 놓치는 일이 없다.
- 체결 여부의 기준은 체결내역 조회 API 다. 체결통보(웹소켓)는 조회를 앞당기는 신호로만 쓴다.
"""
from __future__ import annotations

import asyncio
import logging
import time
import tomllib
from dataclasses import dataclass, field, fields
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path

from broker.account import AccountService
from core.api_client import KisClient
from core.exceptions import KisApiError, KisError, KisNetworkError
from market.clock import ET, KST
from strategy.base import Side, Signal

log = logging.getLogger(__name__)

_ORDER_PATH = "/uapi/overseas-stock/v1/trading/order"
_CANCEL_PATH = "/uapi/overseas-stock/v1/trading/order-rvsecncl"
_CCNL_PATH = "/uapi/overseas-stock/v1/trading/inquire-ccnl"
_MAX_PAGES = 10
_UNKNOWN_GIVE_UP_SEC = 60.0   # 전송 결과 불확실 주문을 체결내역에서 못 찾으면 미접수로 확정


class OrderStatus(str, Enum):
    PENDING = "PENDING"        # 전송 중
    SUBMITTED = "SUBMITTED"    # 접수 (미체결)
    PARTIAL = "PARTIAL"        # 부분 체결
    FILLED = "FILLED"          # 전량 체결
    CANCELED = "CANCELED"      # 취소 (부분 체결 후 잔량 취소 포함)
    REJECTED = "REJECTED"      # 거부
    UNKNOWN = "UNKNOWN"        # 전송 결과 불확실 → 체결내역으로 확인 중
    DRY_RUN = "DRY_RUN"        # 주문 미전송 (모의 실행)

    @property
    def is_open(self) -> bool:
        return self in (OrderStatus.PENDING, OrderStatus.SUBMITTED, OrderStatus.PARTIAL, OrderStatus.UNKNOWN)

    @property
    def label(self) -> str:
        return {
            "PENDING": "전송중", "SUBMITTED": "접수", "PARTIAL": "부분체결", "FILLED": "체결완료",
            "CANCELED": "취소", "REJECTED": "거부", "UNKNOWN": "확인중", "DRY_RUN": "미전송",
        }[self.value]


@dataclass(eq=False)
class Order:
    signal: Signal
    limit_price: float
    status: OrderStatus = OrderStatus.PENDING
    odno: str = ""        # 서버가 준 원래 형식 그대로 (취소 요청 시 사용)
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    message: str = ""
    cancel_requested: bool = False
    cost_basis: float = 0.0   # 매도 주문 시점의 매입평균가 (실현손익 계산용)
    created_at: float = field(default_factory=time.monotonic)
    placed_at: datetime = field(default_factory=lambda: datetime.now(KST))

    @property
    def symbol(self) -> str:
        return self.signal.symbol

    @property
    def side(self) -> Side:
        return self.signal.side

    @property
    def qty(self) -> int:
        return self.signal.qty

    @property
    def remaining(self) -> float:
        return max(self.qty - self.filled_qty, 0.0)

    def __str__(self) -> str:
        fill = f", 체결 {self.filled_qty:g}주 @ ${self.avg_fill_price:,.2f}" if self.filled_qty else ""
        no = f" #{self.odno}" if self.odno else ""
        return (f"{self.side.label} {self.symbol} {self.qty}주 지정가 ${self.limit_price:,.2f}"
                f"{no} [{self.status.label}{fill}]")


@dataclass(frozen=True)
class OrderParams:
    slippage_pct: float = 0.3
    order_timeout_sec: float = 60.0
    poll_interval_sec: float = 5.0
    max_orders_per_day: int = 20
    dry_run: bool = False
    allow_real_trading: bool = False

    @classmethod
    def from_toml(cls, path: Path) -> OrderParams:
        with path.open("rb") as f:
            raw = tomllib.load(f).get("order", {})
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"{path.name} [order]: 알 수 없는 설정 {sorted(unknown)}")
        params = cls(**raw)
        if not 0 <= params.slippage_pct < 5:
            raise ValueError("slippage_pct 는 0 이상 5 미만이어야 합니다")
        if params.poll_interval_sec < 1 or params.order_timeout_sec < params.poll_interval_sec:
            raise ValueError("poll_interval_sec >= 1, order_timeout_sec >= poll_interval_sec 이어야 합니다")
        return params


def _round_price(price: float) -> float:
    """미국주식 호가 단위: $1 이상 0.01, 미만 0.0001."""
    unit = Decimal("0.01") if price >= 1 else Decimal("0.0001")
    return float(Decimal(str(price)).quantize(unit, rounding=ROUND_HALF_UP))


def _num(v: object) -> float:
    try:
        return float(str(v).replace(",", "")) if v not in (None, "") else 0.0
    except ValueError:
        return 0.0


def _norm_odno(v: object) -> str:
    """비교용 주문번호 (앞자리 0 제거). API 호출에는 원래 형식을 쓴다."""
    return str(v or "").strip().lstrip("0")


class OrderManager:
    def __init__(self, client: KisClient, account: AccountService, params: OrderParams):
        self._c = client
        self._s = client.settings
        self._account = account
        self.p = params
        self.orders: list[Order] = []
        self._by_odno: dict[str, Order] = {}
        self._lock = asyncio.Lock()
        self._sync_now = asyncio.Event()
        self._send_tasks: set[asyncio.Task] = set()
        self.dry_run = params.dry_run or (self._s.is_real and not params.allow_real_trading)
        if self._s.is_real and not params.allow_real_trading and not params.dry_run:
            log.warning("실전 계좌지만 allow_real_trading = false → 주문을 보내지 않습니다 (신호만 기록)")

    # ── 조회용 ──────────────────────────────────────────────
    def open_orders(self, symbol: str | None = None) -> list[Order]:
        return [o for o in self.orders if o.status.is_open and (symbol is None or o.symbol == symbol)]

    def pending_buy_symbols(self) -> set[str]:
        return {o.symbol for o in self.open_orders() if o.side is Side.BUY}

    def request_sync(self) -> None:
        """체결통보 수신 등으로 즉시 체결내역을 확인하고 싶을 때 호출."""
        self._sync_now.set()

    # ── 주문 ────────────────────────────────────────────────
    def submit(self, signal: Signal) -> Order | None:
        """사전 검증 후 주문을 등록하고 전송은 백그라운드로 진행한다 (틱 처리를 막지 않음)."""
        # 시장가 대신 현재가에서 slippage_pct 만큼 불리한 지정가 → 즉시 체결되면서 가격 상한/하한 보장
        direction = 1 if signal.side is Side.BUY else -1
        limit = _round_price(signal.price * (1 + direction * self.p.slippage_pct / 100))
        reason = self._precheck(signal, limit)
        if reason:
            log.warning("[주문 보류] %s → %s", signal, reason)
            return None

        order = Order(signal, limit)
        if signal.side is Side.SELL:
            holding = self._account.snapshot.holdings.get(signal.symbol)
            order.cost_basis = holding.avg_price if holding else 0.0
        self.orders.append(order)
        if self.dry_run:
            order.status = OrderStatus.DRY_RUN
            log.info("[주문-미전송] %s", order)
            return order

        task = asyncio.create_task(self._send(order), name=f"order-{signal.symbol}")
        self._send_tasks.add(task)
        task.add_done_callback(self._send_tasks.discard)
        return order

    def _precheck(self, signal: Signal, limit: float) -> str | None:
        if signal.qty <= 0:
            return "수량 0"
        if self.open_orders(signal.symbol):
            return "같은 종목에 미체결 주문이 있음"
        sent_today = sum(1 for o in self.orders if o.status is not OrderStatus.REJECTED)
        if sent_today >= self.p.max_orders_per_day:
            return f"일일 주문 한도({self.p.max_orders_per_day}건) 도달"
        snap = self._account.snapshot
        if snap is None:
            return "잔고 정보 없음"

        if signal.side is Side.BUY:
            reserved = sum(o.remaining * o.limit_price for o in self.open_orders() if o.side is Side.BUY)
            available = snap.orderable_usd - reserved
            cost = signal.qty * limit
            if cost > available:
                return f"주문가능금액 부족 (필요 ${cost:,.2f} / 가능 ${available:,.2f})"
        else:
            holding = snap.holdings.get(signal.symbol)
            reserved = sum(o.remaining for o in self.open_orders(signal.symbol) if o.side is Side.SELL)
            available = (holding.orderable_qty if holding else 0) - reserved
            if signal.qty > available:
                return f"매도가능수량 부족 (요청 {signal.qty}주 / 가능 {available:g}주)"
        return None

    async def _send(self, order: Order) -> None:
        sig = order.signal
        tr_id = self._s.tr_id("overseas_buy" if sig.side is Side.BUY else "overseas_sell")
        price_text = f"{order.limit_price:.2f}" if order.limit_price >= 1 else f"{order.limit_price:.4f}"
        body = {
            "CANO": self._s.cano, "ACNT_PRDT_CD": self._s.acnt_prdt_cd,
            "OVRS_EXCG_CD": sig.exchange, "PDNO": sig.symbol, "ORD_QTY": str(sig.qty),
            "OVRS_ORD_UNPR": price_text, "ORD_SVR_DVSN_CD": "0", "ORD_DVSN": "00",   # 00: 지정가
        }
        async with self._lock:
            try:
                resp = await self._c.post(_ORDER_PATH, tr_id, body)
            except KisApiError as e:
                order.status, order.message = OrderStatus.REJECTED, f"{e.msg_cd} {e.msg}"
                log.warning("[주문 거부] %s → %s", order, order.message)
                return
            except KisError as e:
                # 서버 도달 여부를 모름 → 재전송하지 않고 체결내역 조회로 확인 (중복 주문 방지)
                order.status, order.message = OrderStatus.UNKNOWN, str(e)
                log.error("[주문 결과 불확실] %s → 체결내역으로 확인합니다: %s", order, e)
                self._sync_now.set()
                return
            out = resp.body.get("output") or {}
            order.odno = str(out.get("ODNO", "")).strip()
            order.status = OrderStatus.SUBMITTED
            self._by_odno[_norm_odno(order.odno)] = order
            log.info("[주문 접수] %s (%s)", order, sig.reason)
        self._sync_now.set()

    async def cancel(self, order: Order) -> None:
        if not order.odno or not order.status.is_open or order.cancel_requested:
            return
        order.cancel_requested = True
        body = {
            "CANO": self._s.cano, "ACNT_PRDT_CD": self._s.acnt_prdt_cd,
            "OVRS_EXCG_CD": order.signal.exchange, "PDNO": order.symbol, "ORGN_ODNO": order.odno,
            "RVSE_CNCL_DVSN_CD": "02", "ORD_QTY": str(int(order.remaining)), "OVRS_ORD_UNPR": "0",
            "MGCO_APTM_ODNO": "", "ORD_SVR_DVSN_CD": "0",
        }
        async with self._lock:
            try:
                await self._c.post(_CANCEL_PATH, self._s.tr_id("overseas_cancel"), body)
                log.info("[취소 요청] %s", order)
            except KisApiError as e:
                log.warning("[취소 거부] %s → %s %s (이미 체결됐을 수 있음)", order, e.msg_cd, e.msg)
            except KisError as e:
                order.cancel_requested = False   # 다음 동기화 때 다시 시도
                log.warning("[취소 실패] %s → %s", order, e)
        self._sync_now.set()

    async def cancel_all_open(self) -> None:
        for order in self.open_orders():
            await self.cancel(order)

    # ── 체결 동기화 ─────────────────────────────────────────
    async def run(self) -> None:
        """미체결 주문이 있는 동안 주기적으로 체결내역을 조회한다. 취소(cancel)될 때까지 실행."""
        while True:
            try:
                await asyncio.wait_for(self._sync_now.wait(), timeout=self.p.poll_interval_sec)
            except TimeoutError:
                pass
            self._sync_now.clear()
            if not self.open_orders():
                continue
            try:
                await self.sync()
            except KisError as e:
                log.warning("체결내역 동기화 실패 (다음 주기에 재시도): %s", e)

    async def sync(self) -> None:
        async with self._lock:
            rows = await self._fetch_today_orders()

        filled_changed = False
        for row in rows:
            order = self._by_odno.get(_norm_odno(row.get("odno"))) or self._match_unknown(row)
            if order is not None:
                filled_changed |= self._apply(order, row)

        now = time.monotonic()
        for order in self.open_orders():
            age = now - order.created_at
            if order.status is OrderStatus.UNKNOWN and age > _UNKNOWN_GIVE_UP_SEC:
                order.status = OrderStatus.REJECTED
                order.message = "체결내역에 없음 → 미접수로 판단"
                log.warning("[주문 미접수 확정] %s", order)
            elif order.odno and age > self.p.order_timeout_sec and not order.cancel_requested:
                log.info("[미체결 %.0f초 경과] 잔량 취소: %s", age, order)
                await self.cancel(order)

        if filled_changed:
            try:
                await self._account.refresh()
                log.info("체결 반영 → 잔고 동기화 완료 (주문가능 $%s)",
                         f"{self._account.snapshot.orderable_usd:,.2f}" if self._account.snapshot else "-")
            except KisError as e:
                log.warning("체결 후 잔고 동기화 실패: %s", e)

    def _match_unknown(self, row: dict) -> Order | None:
        """전송 결과가 불확실했던 주문을 체결내역의 종목·방향·수량으로 찾아 연결한다."""
        odno = str(row.get("odno", "")).strip()
        side = Side.SELL if row.get("sll_buy_dvsn_cd") == "01" else Side.BUY
        for order in self.orders:
            if (order.status is OrderStatus.UNKNOWN and order.symbol == row.get("pdno")
                    and order.side is side and order.qty == int(_num(row.get("ft_ord_qty")))):
                order.odno, order.status = odno, OrderStatus.SUBMITTED
                self._by_odno[_norm_odno(odno)] = order
                log.info("[주문 확인] 전송 결과 불확실했던 주문이 접수된 것으로 확인: %s", order)
                return order
        return None

    def _apply(self, order: Order, row: dict) -> bool:
        """체결내역 한 행을 주문에 반영. 체결 수량이 늘었으면 True."""
        prev_status, prev_filled = order.status, order.filled_qty
        filled = _num(row.get("ft_ccld_qty"))
        unfilled = _num(row.get("nccs_qty"))
        state = str(row.get("prcs_stat_name", ""))
        order.filled_qty = filled
        if filled:
            order.avg_fill_price = _num(row.get("ft_ccld_unpr3"))

        if filled >= order.qty:
            order.status = OrderStatus.FILLED
        elif "거부" in state:
            order.status = OrderStatus.REJECTED
            order.message = str(row.get("rjct_rson", "")).strip()
        elif unfilled <= 0 and ("완료" in state or order.cancel_requested):
            order.status = OrderStatus.CANCELED
        elif filled > 0:
            order.status = OrderStatus.PARTIAL

        if order.status is not prev_status or order.filled_qty != prev_filled:
            tag = {"FILLED": "체결", "PARTIAL": "부분체결", "CANCELED": "취소완료", "REJECTED": "거부"}
            log.info("[%s] %s", tag.get(order.status.value, "상태변경"), order)
        return order.filled_qty > prev_filled

    async def _fetch_today_orders(self) -> list[dict]:
        # 미국 현지 날짜와 한국 날짜가 다르므로 둘 다 포함되게 조회
        start = (datetime.now(ET) - timedelta(days=1)).strftime("%Y%m%d")
        end = datetime.now(KST).strftime("%Y%m%d")
        all_symbols = "%" if self._s.is_real else ""   # 모의투자는 공란만 허용
        rows: list[dict] = []
        fk = nk = tr_cont = ""
        for _ in range(_MAX_PAGES):
            resp = await self._c.get(_CCNL_PATH, self._s.tr_id("overseas_ccnl"), {
                "CANO": self._s.cano, "ACNT_PRDT_CD": self._s.acnt_prdt_cd,
                "PDNO": all_symbols, "ORD_STRT_DT": start, "ORD_END_DT": end,
                "SLL_BUY_DVSN": "00", "CCLD_NCCS_DVSN": "00", "OVRS_EXCG_CD": all_symbols,
                "SORT_SQN": "DS", "ORD_DT": "", "ORD_GNO_BRNO": "", "ODNO": "",
                "CTX_AREA_NK200": nk, "CTX_AREA_FK200": fk,
            }, tr_cont=tr_cont)
            rows.extend(resp.body.get("output") or [])
            if not resp.has_next:
                break
            fk = resp.body.get("ctx_area_fk200", "")
            nk = resp.body.get("ctx_area_nk200", "")
            tr_cont = "N"
        return rows

    # ── 종료 ────────────────────────────────────────────────
    async def close(self) -> None:
        """전송 중인 주문을 기다리고, 남은 미체결 주문을 취소한 뒤 최종 상태를 확인한다."""
        if self._send_tasks:
            await asyncio.gather(*self._send_tasks, return_exceptions=True)
        if self.open_orders():
            log.info("세션 종료: 미체결 주문 %d건 취소", len(self.open_orders()))
            await self.cancel_all_open()
            try:
                await self.sync()
            except KisError as e:
                log.warning("최종 체결내역 확인 실패: %s", e)
        if self.orders:
            log.info("=== 이번 세션 주문 %d건 ===", len(self.orders))
            for o in self.orders:
                log.info("  %s", o)

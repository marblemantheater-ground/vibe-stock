"""해외(미국)주식 계좌 조회: 주문가능 외화(USD) + 보유잔고."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

from core.api_client import KisClient

log = logging.getLogger(__name__)

_BALANCE_PATH = "/uapi/overseas-stock/v1/trading/inquire-balance"
_PSAMOUNT_PATH = "/uapi/overseas-stock/v1/trading/inquire-psamount"
_MAX_PAGES = 20


def _num(value: object) -> float:
    try:
        return float(str(value).replace(",", "")) if value not in (None, "") else 0.0
    except ValueError:
        return 0.0


@dataclass(frozen=True)
class Holding:
    symbol: str
    name: str
    exchange: str
    quantity: float
    orderable_qty: float
    avg_price: float
    current_price: float
    purchase_amount: float
    eval_amount: float
    pnl_amount: float
    pnl_rate: float


@dataclass(frozen=True)
class AccountSnapshot:
    orderable_usd: float
    exchange_rate: float
    holdings: dict[str, Holding] = field(default_factory=dict)
    fetched_at: datetime = field(default_factory=datetime.now)

    @property
    def total_purchase(self) -> float:
        return sum(h.purchase_amount for h in self.holdings.values())

    @property
    def total_eval(self) -> float:
        return sum(h.eval_amount for h in self.holdings.values())

    @property
    def total_pnl(self) -> float:
        return sum(h.pnl_amount for h in self.holdings.values())

    @property
    def total_pnl_rate(self) -> float:
        return self.total_pnl / self.total_purchase * 100 if self.total_purchase else 0.0


class AccountService:
    """계좌 상태의 단일 진실 원천(source of truth).

    refresh() 는 락으로 직렬화되어, 매매 루프·체결 이벤트가 동시에 동기화를 요청해도
    조회가 섞이지 않고 snapshot 이 통째로 교체된다.
    """

    def __init__(self, client: KisClient):
        self._c = client
        self._s = client.settings
        self._lock = asyncio.Lock()
        self.snapshot: AccountSnapshot | None = None

    async def refresh(self) -> AccountSnapshot:
        async with self._lock:
            orderable_usd, exrt = await self.fetch_buying_power()
            holdings = await self.fetch_holdings()
            self.snapshot = AccountSnapshot(
                orderable_usd=orderable_usd, exchange_rate=exrt, holdings=holdings
            )
            return self.snapshot

    async def fetch_buying_power(self) -> tuple[float, float]:
        """(주문가능 USD, 적용환율). 기준 종목은 계산용일 뿐 금액에는 영향 없음."""
        resp = await self._c.get(
            _PSAMOUNT_PATH,
            self._s.tr_id("overseas_psamount"),
            {
                "CANO": self._s.cano,
                "ACNT_PRDT_CD": self._s.acnt_prdt_cd,
                "OVRS_EXCG_CD": self._s.ref_exchange,
                "OVRS_ORD_UNPR": "100",
                "ITEM_CD": self._s.ref_symbol,
            },
        )
        out = resp.body.get("output", {})
        return _num(out.get("ord_psbl_frcr_amt")), _num(out.get("exrt"))

    async def fetch_holdings(self) -> dict[str, Holding]:
        holdings: dict[str, Holding] = {}
        for exchange in self._s.balance_exchanges:
            for row in await self._fetch_balance_rows(exchange):
                qty = _num(row.get("ovrs_cblc_qty"))
                if qty <= 0:
                    continue  # 당일 전량 매도된 종목은 0주로 남아 있음
                symbol = row.get("ovrs_pdno", "").strip()
                holdings[symbol] = Holding(
                    symbol=symbol,
                    name=row.get("ovrs_item_name", "").strip(),
                    exchange=row.get("ovrs_excg_cd", exchange),
                    quantity=qty,
                    orderable_qty=_num(row.get("ord_psbl_qty")),
                    avg_price=_num(row.get("pchs_avg_pric")),
                    current_price=_num(row.get("now_pric2")),
                    purchase_amount=_num(row.get("frcr_pchs_amt1")),
                    eval_amount=_num(row.get("ovrs_stck_evlu_amt")),
                    pnl_amount=_num(row.get("frcr_evlu_pfls_amt")),
                    pnl_rate=_num(row.get("evlu_pfls_rt")),
                )
        return holdings

    async def _fetch_balance_rows(self, exchange: str) -> list[dict]:
        rows: list[dict] = []
        fk, nk, tr_cont = "", "", ""
        for _ in range(_MAX_PAGES):
            resp = await self._c.get(
                _BALANCE_PATH,
                self._s.tr_id("overseas_balance"),
                {
                    "CANO": self._s.cano,
                    "ACNT_PRDT_CD": self._s.acnt_prdt_cd,
                    "OVRS_EXCG_CD": exchange,
                    "TR_CRCY_CD": "USD",
                    "CTX_AREA_FK200": fk,
                    "CTX_AREA_NK200": nk,
                },
                tr_cont=tr_cont,
            )
            rows.extend(resp.body.get("output1") or [])
            if not resp.has_next:
                break
            fk = resp.body.get("ctx_area_fk200", "")
            nk = resp.body.get("ctx_area_nk200", "")
            tr_cont = "N"
        else:
            log.warning("잔고 연속조회가 %d페이지를 넘어 중단 (%s)", _MAX_PAGES, exchange)
        return rows

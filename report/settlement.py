"""5단계: 거래일 정산 — 매매내역 저장, 실현/평가 손익·수익률 계산, 일별 요약 누적.

저장 위치 (data/ 는 git 에서 제외 → PC 마다 따로 쌓인다)
    data/reports/{env}/trades/YYYY-MM-DD.csv    주문 1건 = 1행 (재시작해도 같은 날 파일에 이어서 기록)
    data/reports/{env}/daily/YYYY-MM-DD.json    그날 정산 보고서 (보유 종목·매매내역 포함)
    data/reports/{env}/daily_summary.csv        하루 1행 누적 요약 (같은 날 다시 정산하면 덮어씀)

날짜는 미국 현지(ET) 거래일 기준. CSV 는 엑셀(Windows/macOS)에서 한글이 깨지지 않도록 UTF-8 BOM 으로 저장한다.

손익 기준
- 실현손익: 매도 체결가 - 매도 주문 시점의 매입평균가 (수수료·세금 제외)
- 총자산(추정): 주문가능 USD + 보유 평가금액. 입출금이 있던 날은 수익률이 왜곡된다.
"""
from __future__ import annotations

import csv
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from broker.account import AccountService, AccountSnapshot
from broker.order import Order, OrderStatus
from core.exceptions import KisError
from market.clock import KST

log = logging.getLogger(__name__)

_CSV_ENCODING = "utf-8-sig"

TRADE_FIELDS = [
    "date", "placed_at", "symbol", "exchange", "side", "qty", "limit_price", "status", "odno",
    "filled_qty", "avg_fill_price", "fill_amount", "cost_basis", "realized_pnl", "reason", "message",
]
SUMMARY_FIELDS = [
    "date", "env", "orders", "filled_orders", "buy_amount", "sell_amount", "realized_pnl",
    "wins", "losses", "start_equity", "end_equity", "equity_change", "return_pct", "unrealized_pnl",
]


def _num(v: object) -> float:
    try:
        return float(v) if v not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _equity(snap: AccountSnapshot | None) -> float:
    return snap.orderable_usd + snap.total_eval if snap else 0.0


def trade_row(order: Order, trading_date: date) -> dict:
    filled = order.filled_qty
    realized = ""
    if order.side.value == "SELL" and filled and order.cost_basis:
        realized = round((order.avg_fill_price - order.cost_basis) * filled, 4)
    return {
        "date": trading_date.isoformat(),
        "placed_at": order.placed_at.astimezone(KST).isoformat(timespec="seconds"),
        "symbol": order.symbol,
        "exchange": order.signal.exchange,
        "side": order.side.value,
        "qty": order.qty,
        "limit_price": order.limit_price,
        "status": order.status.value,
        "odno": order.odno,
        "filled_qty": filled,
        "avg_fill_price": order.avg_fill_price,
        "fill_amount": round(filled * order.avg_fill_price, 4),
        "cost_basis": order.cost_basis or "",
        "realized_pnl": realized,
        "reason": order.signal.reason,
        "message": order.message,
    }


@dataclass
class DailyReport:
    date: str
    env: str
    generated_at: str
    orders: int
    status_counts: dict[str, int]
    filled_orders: int
    buy_amount: float
    sell_amount: float
    realized_pnl: float
    wins: int
    losses: int
    start_equity: float
    end_equity: float
    unrealized_pnl: float
    holdings: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)

    @property
    def equity_change(self) -> float:
        return self.end_equity - self.start_equity

    @property
    def return_pct(self) -> float:
        return self.equity_change / self.start_equity * 100 if self.start_equity else 0.0

    @property
    def win_rate(self) -> float:
        closed = self.wins + self.losses
        return self.wins / closed * 100 if closed else 0.0

    def summary_row(self) -> dict:
        row = {k: getattr(self, k) for k in SUMMARY_FIELDS}
        return {k: round(v, 4) if isinstance(v, float) else v for k, v in row.items()}

    def to_json(self) -> dict:
        data = asdict(self)
        data.update(equity_change=round(self.equity_change, 4), return_pct=round(self.return_pct, 4),
                    win_rate=round(self.win_rate, 2))
        return data


def build_report(trading_date: date, env: str, rows: list[dict], start_equity: float,
                 end_snap: AccountSnapshot | None) -> DailyReport:
    status_counts: dict[str, int] = {}
    buy_amount = sell_amount = realized = 0.0
    wins = losses = filled_orders = 0
    for r in rows:
        status_counts[r["status"]] = status_counts.get(r["status"], 0) + 1
        amount = _num(r["fill_amount"])
        if not _num(r["filled_qty"]):
            continue
        filled_orders += 1
        if r["side"] == "BUY":
            buy_amount += amount
        else:
            sell_amount += amount
            if r["realized_pnl"] != "":
                pnl = _num(r["realized_pnl"])
                realized += pnl
                wins += pnl > 0
                losses += pnl < 0

    holdings = [
        {"symbol": h.symbol, "quantity": h.quantity, "avg_price": h.avg_price, "current_price": h.current_price,
         "eval_amount": h.eval_amount, "pnl_amount": h.pnl_amount, "pnl_rate": h.pnl_rate}
        for h in (end_snap.holdings.values() if end_snap else [])
    ]
    return DailyReport(
        date=trading_date.isoformat(), env=env,
        generated_at=datetime.now(KST).isoformat(timespec="seconds"),
        orders=len(rows), status_counts=status_counts, filled_orders=filled_orders,
        buy_amount=buy_amount, sell_amount=sell_amount, realized_pnl=realized, wins=wins, losses=losses,
        start_equity=start_equity, end_equity=_equity(end_snap),
        unrealized_pnl=end_snap.total_pnl if end_snap else 0.0,
        holdings=holdings, trades=rows,
    )


def _atomic_write(path: Path, write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding=_CSV_ENCODING if path.suffix == ".csv" else "utf-8", newline="") as f:
        write(f)
    os.replace(tmp, path)


class SettlementStore:
    def __init__(self, data_dir: Path, env: str):
        self.env = env
        self.root = data_dir / "reports" / env
        self.summary_path = self.root / "daily_summary.csv"

    def trades_path(self, d: date) -> Path:
        return self.root / "trades" / f"{d.isoformat()}.csv"

    def report_path(self, d: date) -> Path:
        return self.root / "daily" / f"{d.isoformat()}.json"

    def append_trades(self, d: date, orders: list[Order]) -> None:
        if not orders:
            return
        path = self.trades_path(d)
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        with path.open("a", encoding=_CSV_ENCODING if new_file else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=TRADE_FIELDS)
            if new_file:
                w.writeheader()
            w.writerows(trade_row(o, d) for o in orders)

    def load_trades(self, d: date) -> list[dict]:
        path = self.trades_path(d)
        if not path.exists():
            return []
        with path.open(encoding=_CSV_ENCODING, newline="") as f:
            return list(csv.DictReader(f))

    def saved_start_equity(self, d: date) -> float | None:
        """같은 날 이미 정산한 적이 있으면 그때의 시작 자산을 유지 (재시작 시 기준점이 바뀌지 않게)."""
        try:
            return float(json.loads(self.report_path(d).read_text(encoding="utf-8"))["start_equity"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def load_report(self, d: date) -> DailyReport | None:
        try:
            data = json.loads(self.report_path(d).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return DailyReport(**{k: data[k] for k in DailyReport.__dataclass_fields__ if k in data})

    def save_report(self, report: DailyReport) -> None:
        d = date.fromisoformat(report.date)
        _atomic_write(self.report_path(d),
                      lambda f: json.dump(report.to_json(), f, ensure_ascii=False, indent=2))

        rows: dict[str, dict] = {}
        if self.summary_path.exists():
            with self.summary_path.open(encoding=_CSV_ENCODING, newline="") as f:
                rows = {r["date"]: r for r in csv.DictReader(f)}
        rows[report.date] = report.summary_row()

        def write(f):
            w = csv.DictWriter(f, fieldnames=SUMMARY_FIELDS, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows[k] for k in sorted(rows))
        _atomic_write(self.summary_path, write)


async def settle_day(store: SettlementStore, account: AccountService, orders: list[Order],
                     trading_date: date, start_snap: AccountSnapshot | None) -> DailyReport | None:
    """주문 기록 → 최종 잔고 조회 → 보고서 계산·저장. 실패해도 예외를 밖으로 내보내지 않는다."""
    try:
        store.append_trades(trading_date, orders)
    except OSError as e:
        log.error("매매내역 저장 실패: %s", e)

    try:
        end_snap = await account.refresh()
    except KisError as e:
        end_snap = account.snapshot
        log.warning("정산용 최종 잔고 조회 실패 → 마지막 잔고로 계산: %s", e)

    try:
        start_equity = store.saved_start_equity(trading_date)
        if start_equity is None:
            start_equity = _equity(start_snap)
        report = build_report(trading_date, store.env, store.load_trades(trading_date), start_equity, end_snap)
        store.save_report(report)
    except (OSError, ValueError, KeyError) as e:
        log.error("정산 보고서 생성/저장 실패: %s", e)
        return None

    log_report(report)
    log.info("정산 저장: %s", store.report_path(trading_date))
    return report


def log_report(r: DailyReport) -> None:
    log.info(f"===== {r.date} 정산 ({r.env}) =====")
    status = ", ".join(f"{OrderStatus(k).label} {v}" for k, v in sorted(r.status_counts.items())) or "없음"
    log.info(f"주문 {r.orders}건 ({status})")
    log.info(f"체결: 매수 ${r.buy_amount:,.2f} / 매도 ${r.sell_amount:,.2f}")
    log.info(f"실현손익: ${r.realized_pnl:+,.2f} (익절 {r.wins} / 손절 {r.losses}, 승률 {r.win_rate:.1f}%) — 수수료 제외")
    log.info(f"평가손익(보유 {len(r.holdings)}종목): ${r.unrealized_pnl:+,.2f}")
    log.info(f"총자산(추정): ${r.start_equity:,.2f} → ${r.end_equity:,.2f} "
             f"({r.equity_change:+,.2f}, {r.return_pct:+.2f}%)")

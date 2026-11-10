"""진입점.

1단계: 설정 로드 → 토큰 확보 → 예수금/잔고 조회
2단계: 장 세션 판별 → 세션 동안 웹소켓 실시간 시세 수신 → 장 종료 후 다음 세션까지 대기
3단계: 과거 분봉으로 전략 준비 → 틱마다 전략 실행 → 매수/매도 신호
4단계: 신호 → 지정가 주문 → 체결 확인(조회 + 체결통보) → 잔고 동기화, 장 종료 시 미체결 취소
5단계: 거래일 종료(또는 중단) 시 매매내역·손익·수익률 정산 → data/reports/ 에 저장

사용법:
    python main.py                    # 상시 실행 (휴장 중이면 다음 세션까지 대기)
    python main.py --once             # 거래일 하루만 실행하고 정산 후 종료 (작업 스케줄러/cron 용)
    python main.py --stream-test 60   # 세션과 무관하게 60초간 실행 (신호 발생 시 주문도 나감) 후 정산
    python main.py --report           # 가장 최근 정산 보고서 출력 (--report 2026-11-10 처럼 날짜 지정 가능)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import date, datetime, timedelta

if sys.version_info < (3, 11):  # asyncio.wait_for 의 TimeoutError 동작 등 3.11+ 기준으로 작성됨
    sys.exit(f"Python 3.11 이상이 필요합니다 (현재 {sys.version.split()[0]})")

from broker.account import AccountService, AccountSnapshot, Holding
from broker.order import OrderManager, OrderParams
from config.settings import BASE_DIR, load_settings
from core.api_client import KisClient
from core.exceptions import KisApiError, KisAuthError, KisError
from core.logger import setup_logging
from core.ws_client import KisWebSocket, Tick
from market.bars import fetch_minute_bars
from market.clock import ET, MarketClock, Session, fmt_kst
from market.watchlist import WatchItem, load_watchlist
from report.settlement import SettlementStore, log_report, settle_day
from strategy.base import Strategy
from strategy.ma_cross import MaCrossParams, MovingAverageCrossStrategy

log = logging.getLogger("main")

_BOOTSTRAP_ATTEMPTS = 5
_WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.csv"
_STRATEGY_PATH = BASE_DIR / "config" / "strategy.toml"
_TICK_LOG_INTERVAL = 30.0     # 종목별 시세 로그 출력 간격(초)
_MAX_IDLE_SLEEP = 300.0       # 대기 중 시계를 다시 확인하는 최대 간격 (PC 절전 복귀 대비)


async def bootstrap(client: KisClient, account: AccountService) -> AccountSnapshot:
    """토큰 확보 + 첫 잔고 조회. 일시적 장애는 재시도, 인증 오류는 즉시 중단."""
    for attempt in range(1, _BOOTSTRAP_ATTEMPTS + 1):
        try:
            await client.tokens.get_token()
            return await account.refresh()
        except KisAuthError:
            raise
        except KisApiError as e:
            log.error("계좌 조회 거부: %s", e)
            raise
        except KisError as e:
            wait = min(10 * attempt, 60)
            log.warning("초기화 실패 (%d/%d): %s → %d초 후 재시도", attempt, _BOOTSTRAP_ATTEMPTS, e, wait)
            await asyncio.sleep(wait)
    raise KisError("초기화 재시도 횟수 초과")


def log_snapshot(snap: AccountSnapshot) -> None:
    log.info(f"주문가능 USD: ${snap.orderable_usd:,.2f} (환율 {snap.exchange_rate:,.2f})")
    if not snap.holdings:
        log.info("보유 종목 없음")
        return
    for h in snap.holdings.values():
        log.info(
            f"  {h.symbol:<6} {h.exchange:<4} {h.quantity:>6g}주  평단 ${h.avg_price:,.2f}  "
            f"현재 ${h.current_price:,.2f}  손익 ${h.pnl_amount:,.2f} ({h.pnl_rate:.2f}%)"
        )
    log.info(
        f"합계: 매입 ${snap.total_purchase:,.2f}  평가 ${snap.total_eval:,.2f}  "
        f"손익 ${snap.total_pnl:,.2f} ({snap.total_pnl_rate:.2f}%)"
    )


class TickReporter:
    """종목별로 일정 간격마다 최신 시세를 로그로 남긴다 (틱마다 찍으면 로그 폭주)."""

    def __init__(self, interval: float = _TICK_LOG_INTERVAL):
        self._interval = interval
        self._last_logged: dict[str, float] = {}
        self.count = 0

    def on_tick(self, tick: Tick) -> None:
        self.count += 1
        now = time.monotonic()
        if now - self._last_logged.get(tick.symbol, 0.0) < self._interval:
            return
        self._last_logged[tick.symbol] = now
        t = f"{tick.kst_time:%H:%M:%S}" if tick.kst_time else "-"
        log.info(
            f"[시세] {tick.symbol:<5} ${tick.price:,.2f} ({tick.change_rate:+.2f}%) "
            f"매수 {tick.bid:,.2f} / 매도 {tick.ask:,.2f}  누적 {tick.cum_volume:,.0f}주  "
            f"[{tick.market_type} {t}]"
        )


class AccountContext:
    """전략에 계좌 상태를 읽기 전용으로 제공 (잔고 스냅샷 + 진행 중인 매수 주문)."""

    def __init__(self, account: AccountService, orders: OrderManager):
        self._account = account
        self._orders = orders

    def position(self, symbol: str) -> Holding | None:
        snap = self._account.snapshot
        return snap.holdings.get(symbol) if snap else None

    def position_count(self) -> int:
        snap = self._account.snapshot
        held = set(snap.holdings) if snap else set()
        return len(held | self._orders.pending_buy_symbols())   # 체결 대기 중인 매수도 보유로 계산


async def warm_up(client: KisClient, strategy: MovingAverageCrossStrategy,
                  watchlist: list[WatchItem]) -> None:
    """과거 분봉으로 이동평균 계산에 필요한 봉을 미리 채운다. 실패한 종목은 실시간 봉으로 채워진다."""
    log.info("과거 분봉 조회 중 (%d종목)...", len(watchlist))
    for item in watchlist:
        try:
            bars = await fetch_minute_bars(client, item, strategy.p.bar_minutes)
        except KisError as e:
            log.warning("[%s] 과거 분봉 조회 실패 → 실시간 봉으로 채움: %s", item.symbol, e)
            continue
        strategy.seed(item.symbol, bars)
        log.info("[%s] 과거 %d분봉 %d개 로드 (전략 준비 %s)", item.symbol, strategy.p.bar_minutes,
                 len(bars), "완료" if strategy.ready(item.symbol) else "부족")


async def stream_quotes(client: KisClient, account: AccountService, clock: MarketClock,
                        watchlist: list[WatchItem], params: MaCrossParams, order_params: OrderParams,
                        store: SettlementStore, until: datetime | timedelta) -> None:
    """until 시각까지 (timedelta 면 준비가 끝난 시점부터 그 시간 동안) 실시간 시세를 받아 전략을 실행하고 주문한다.
    거래일마다 전략·주문 관리자를 새로 만들고, 끝날 때(오류·중단 포함) 미체결 취소 후 정산한다."""
    trading_date = clock.now().astimezone(ET).date()
    start_snap = account.snapshot
    strategy: Strategy = MovingAverageCrossStrategy(params, watchlist)
    orders = OrderManager(client, account, order_params)
    ctx = AccountContext(account, orders)
    await warm_up(client, strategy, watchlist)

    ws = KisWebSocket(client)
    for item in watchlist:
        await ws.subscribe_ticks(item.symbol, item.quote_exchange)
    if client.settings.hts_id:
        await ws.subscribe_notices(client.settings.hts_id)
        ws.on_notice = orders.request_sync
    else:
        log.info("KIS_HTS_ID 미설정 → 체결통보 없이 %.0f초 주기 조회로 체결 확인", order_params.poll_interval_sec)
    if isinstance(until, timedelta):
        until = clock.now() + until
    ws_task = asyncio.create_task(ws.run(), name="kis-websocket")
    order_task = asyncio.create_task(orders.run(), name="order-sync")
    reporter = TickReporter()
    session = clock.session()
    log.info("실시간 시세 수신 시작: %d종목, %s 까지", len(watchlist), fmt_kst(until))
    try:
        while clock.now() < until:
            if ws_task.done():
                ws_task.result()   # 복구 불가 예외면 여기서 전파
                break
            current = clock.session()
            if current != session:
                log.info("세션 전환: %s → %s", session.label, current.label)
                session = current
            try:
                tick = await asyncio.wait_for(ws.ticks.get(), timeout=1.0)
            except TimeoutError:
                continue
            reporter.on_tick(tick)
            try:
                signals = strategy.on_tick(tick, session, ctx)
            except Exception:
                log.exception("[%s] 전략 처리 중 오류 → 이 틱은 건너뜀", tick.symbol)
                continue
            for signal in signals:
                log.info("[신호] %s", signal)
                orders.submit(signal)
    finally:
        await orders.close()
        order_task.cancel()
        await ws.stop()
        await asyncio.gather(ws_task, order_task, return_exceptions=True)
        log.info("실시간 시세 수신 종료 (수신 틱 %d건)", reporter.count)
        await settle_day(store, account, orders.orders, trading_date, start_snap)


async def sleep_until(clock: MarketClock, target: datetime) -> None:
    """target 까지 대기. 벽시계를 주기적으로 다시 확인해 절전 복귀/시간 보정에도 정확하다."""
    while (remaining := (target - clock.now()).total_seconds()) > 0:
        await asyncio.sleep(min(remaining, _MAX_IDLE_SLEEP))


async def run_forever(client: KisClient, account: AccountService, clock: MarketClock,
                      watchlist: list[WatchItem], params: MaCrossParams, order_params: OrderParams,
                      store: SettlementStore, once: bool = False) -> None:
    """세션 대기 → 거래일 매매 → 정산을 반복한다. once 면 거래일 하나를 마치고 반환."""
    while True:
        now = clock.now()
        if not clock.is_covered(now.date()):
            log.warning("%d년 휴장일 정보가 없습니다. market/clock.py 의 HOLIDAYS 를 갱신하세요.", now.year)

        window = clock.current(now)
        if window is None:
            nxt = clock.next_window(now)
            log.info("현재 휴장. 다음 세션: %s (%.1f시간 후)", nxt,
                     (nxt.start - now).total_seconds() / 3600)
            await sleep_until(clock, nxt.start)
            continue

        log.info("현재 세션: %s", window)
        try:
            log_snapshot(await account.refresh())
        except KisError as e:
            log.error("세션 시작 잔고 동기화 실패 (계속 진행): %s", e)

        day_end = clock.trading_day_end(now)
        assert day_end is not None
        try:
            await stream_quotes(client, account, clock, watchlist, params, order_params, store, until=day_end)
        except Exception:
            log.exception("시세 수신 중 예기치 못한 오류 → 30초 후 재시작")
            await asyncio.sleep(30)
            continue
        log.info("거래일 종료 (%s)", fmt_kst(day_end))
        if once:
            log.info("--once: 거래일 정산을 마치고 종료합니다")
            return


def show_report(store: SettlementStore, which: str) -> int:
    if which == "latest":
        saved = sorted(store.report_path(date.today()).parent.glob("*.json"))
        if not saved:
            log.error("저장된 정산 보고서가 없습니다: %s", store.report_path(date.today()).parent)
            return 1
        which = saved[-1].stem
    try:
        d = date.fromisoformat(which)
    except ValueError:
        log.error("날짜 형식 오류 (예: 2026-11-10): %s", which)
        return 2
    report = store.load_report(d)
    if report is None:
        log.error("%s 정산 보고서가 없습니다", which)
        return 1
    log_report(report)
    for t in report.trades:
        log.info(f"  {t['placed_at'][11:19]} {t['side']:<4} {t['symbol']:<5} {t['qty']:>4}주  "
                 f"{t['status']:<9} 체결 {t['filled_qty']}주 @ ${float(t['avg_fill_price'] or 0):,.2f}  "
                 f"실현 {t['realized_pnl'] or '-'}  ({t['reason']})")
    return 0


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="vibe-stock 미국주식 자동매매")
    p.add_argument("--stream-test", type=int, metavar="SEC",
                   help="세션과 무관하게 SEC초 동안 실행(주문 포함)하고 정산 후 종료")
    p.add_argument("--once", action="store_true", help="거래일 하루만 실행하고 정산 후 종료")
    p.add_argument("--report", nargs="?", const="latest", metavar="YYYY-MM-DD",
                   help="저장된 정산 보고서 출력 (날짜 생략 시 가장 최근)")
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings()
        watchlist = load_watchlist(_WATCHLIST_PATH)
        params = MaCrossParams.from_toml(_STRATEGY_PATH)
        order_params = OrderParams.from_toml(_STRATEGY_PATH)
    except (ValueError, OSError) as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2

    setup_logging(settings.log_dir, settings.log_level)
    store = SettlementStore(BASE_DIR / "data", settings.env)
    if args.report:
        return show_report(store, args.report)
    log.info("=== vibe-stock 시작 (환경: %s, 계좌: %s-**) ===", settings.env.upper(), settings.cano[:4] + "****")
    if not watchlist:
        log.critical("매매 대상 종목이 없습니다: %s", _WATCHLIST_PATH)
        return 2
    log.info("매매 대상 %d종목: %s", len(watchlist), ", ".join(w.symbol for w in watchlist))
    log.info("전략: 이동평균 돌파 (%d분봉 MA%d/MA%d, 등락률 %.1f~%.1f%%, 익절 %.1f%% 손절 %.1f%%, 세션 %s)",
             params.bar_minutes, params.short_window, params.long_window,
             params.min_change_rate, params.max_change_rate, params.take_profit_pct,
             params.stop_loss_pct, "/".join(s.label for s in params.sessions))
    if order_params.dry_run or (settings.is_real and not order_params.allow_real_trading):
        log.warning("주문 모드: 미전송 (신호만 기록) — config/strategy.toml [order] 확인")
    else:
        log.info("주문 모드: %s 계좌로 실제 주문 전송", "실전" if settings.is_real else "모의투자")

    clock = MarketClock()
    async with KisClient(settings) as client:
        account = AccountService(client)
        try:
            snap = await bootstrap(client, account)
        except KisError as e:
            log.critical("초기화 실패로 종료: %s", e)
            return 1
        log.info("토큰 만료 예정: %s", client.tokens.expires_at)
        log_snapshot(snap)
        log.info("현재 시장 상태: %s", clock.session().label)

        if args.stream_test:
            await stream_quotes(client, account, clock, watchlist, params, order_params, store,
                                until=timedelta(seconds=args.stream_test))
            return 0
        await run_forever(client, account, clock, watchlist, params, order_params, store, once=args.once)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logging.getLogger("main").info("사용자 중단")

"""진입점.

1단계: 설정 로드 → 토큰 확보 → 예수금/잔고 조회
2단계: 장 세션 판별 → 세션 동안 웹소켓 실시간 시세 수신 → 장 종료 후 다음 세션까지 대기

사용법:
    python main.py                    # 상시 실행 (휴장 중이면 다음 세션까지 대기)
    python main.py --stream-test 60   # 세션과 무관하게 60초간 실시간 시세 수신 테스트
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime, timedelta

if sys.version_info < (3, 11):  # asyncio.wait_for 의 TimeoutError 동작 등 3.11+ 기준으로 작성됨
    sys.exit(f"Python 3.11 이상이 필요합니다 (현재 {sys.version.split()[0]})")

from broker.account import AccountService, AccountSnapshot
from config.settings import BASE_DIR, load_settings
from core.api_client import KisClient
from core.exceptions import KisApiError, KisAuthError, KisError
from core.logger import setup_logging
from core.ws_client import KisWebSocket, Tick
from market.clock import MarketClock, Session, fmt_kst
from market.watchlist import WatchItem, load_watchlist

log = logging.getLogger("main")

_BOOTSTRAP_ATTEMPTS = 5
_WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.csv"
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


async def stream_quotes(client: KisClient, clock: MarketClock, watchlist: list[WatchItem],
                        until: datetime) -> None:
    """until 시각까지 실시간 시세를 받아 처리한다."""
    ws = KisWebSocket(client)
    for item in watchlist:
        await ws.subscribe_ticks(item.symbol, item.quote_exchange)
    ws_task = asyncio.create_task(ws.run(), name="kis-websocket")
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
            # TODO(3단계): strategy.on_tick(tick, session) → 조건 충족 시 주문
    finally:
        await ws.stop()
        await asyncio.gather(ws_task, return_exceptions=True)
        log.info("실시간 시세 수신 종료 (수신 틱 %d건)", reporter.count)


async def sleep_until(clock: MarketClock, target: datetime) -> None:
    """target 까지 대기. 벽시계를 주기적으로 다시 확인해 절전 복귀/시간 보정에도 정확하다."""
    while (remaining := (target - clock.now()).total_seconds()) > 0:
        await asyncio.sleep(min(remaining, _MAX_IDLE_SLEEP))


async def run_forever(client: KisClient, account: AccountService, clock: MarketClock,
                      watchlist: list[WatchItem]) -> None:
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
            await stream_quotes(client, clock, watchlist, until=day_end)
        except Exception:
            log.exception("시세 수신 중 예기치 못한 오류 → 30초 후 재시작")
            await asyncio.sleep(30)
            continue
        log.info("거래일 종료 (%s)", fmt_kst(day_end))
        # TODO(5단계): 당일 매매내역·수익률 정산 및 저장


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="vibe-stock 미국주식 자동매매")
    p.add_argument("--stream-test", type=int, metavar="SEC",
                   help="세션과 무관하게 SEC초 동안 실시간 시세 수신만 테스트하고 종료")
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings()
        watchlist = load_watchlist(_WATCHLIST_PATH)
    except (ValueError, OSError) as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2

    setup_logging(settings.log_dir, settings.log_level)
    log.info("=== vibe-stock 시작 (환경: %s, 계좌: %s-**) ===", settings.env.upper(), settings.cano[:4] + "****")
    if not watchlist:
        log.critical("매매 대상 종목이 없습니다: %s", _WATCHLIST_PATH)
        return 2
    log.info("매매 대상 %d종목: %s", len(watchlist), ", ".join(w.symbol for w in watchlist))

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
            await stream_quotes(client, clock, watchlist,
                                until=clock.now() + timedelta(seconds=args.stream_test))
            return 0
        await run_forever(client, account, clock, watchlist)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logging.getLogger("main").info("사용자 중단")

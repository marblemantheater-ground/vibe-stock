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
    python main.py --annual           # 올해 연간 정산: 수수료·원화 환산·예상 양도소득세 (--annual 2026 처럼 연도 지정)
    python main.py --collect-minute   # 백테스트용 1분봉 수집 → data/minute/ (KIS 는 최근 약 한 달치만 제공 → 매일 이어 붙임)
    python main.py --backtest 20      # 저장된 분봉 최근 20거래일로 현재 설정 백테스트 (화면: 대시보드 백테스트 탭)
    python main.py --collect-daily    # 미국 전체 종목 일봉 수집 → data/daily/ (장 마감 후 실행, 끊겨도 이어서 진행)
    python main.py --screen           # 수집한 일봉으로 그랜빌 120일선 조건 검사 → data/screen/
    python main.py --kill             # 긴급 정지: 미체결 전부 취소 + 보유 전부 매도 + 신규 매수 중지
    python main.py --pause            # 일시정지: 신규 매수만 중지 (매도는 계속)
    python main.py --resume           # 손실 한도·긴급 정지·일시정지로 멈춘 매매 재개
    python main.py --risk-status      # 손실 차단 상태(고점·오늘 기준 자산·중지 여부) 출력
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
import tomllib
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta

if sys.version_info < (3, 11):  # asyncio.wait_for 의 TimeoutError 동작 등 3.11+ 기준으로 작성됨
    sys.exit(f"Python 3.11 이상이 필요합니다 (현재 {sys.version.split()[0]})")

from broker.account import AccountService, AccountSnapshot, Holding
from broker.order import OrderManager, OrderParams
from config.live import Change, LiveConfig
from config.presets import PresetStore
from config.settings import BASE_DIR, load_settings
from core.api_client import KisClient
from core.exceptions import KisApiError, KisAuthError, KisError
from core.instance_lock import AlreadyRunning, InstanceLock
from core.notifier import NOTIFY, setup_slack
from core.logger import setup_logging
from core.ws_client import KisWebSocket, Tick
from market.bars import fetch_minute_bars
from market.clock import ET, KST, MarketClock, Session, fmt_kst
from risk.guard import HaltKind, RiskGuard, RiskParams, RiskStore, apply_command_offline, describe_state
from market.daily import DEFAULT_DAILY_COUNT, collect_daily
from market.gainers import Gainer, GainerLog, GainersParams, fetch_rank_rows, load_master_info, pick
from market.universe import load_universe
from market.watchlist import MAX_SYMBOLS, QUOTE_EXCHANGE, WatchItem, load_screen_result, load_watchlist, merge_watchlists
from backtest.runs import RunStore, run_backtest
from market.minute import MinuteStore, collect_minutes
from report.annual import available_years, compute_annual, log_annual, save_annual
from report.settlement import SettlementStore, log_report, save_progress, settle_day
from sim.premarket import PremarketSim, SimStore
from strategy.ma_cross import TOGGLES as MA_TOGGLES, MovingAverageCrossStrategy
from strategy.positions import PositionBook
from strategy.screener import ScreenerParams, run_screen
from web.server import DashboardParams, DashboardServer
from web.state import DashboardState

log = logging.getLogger("main")

_BOOTSTRAP_ATTEMPTS = 5
_WATCHLIST_PATH = BASE_DIR / "config" / "watchlist.csv"
_STRATEGY_PATH = BASE_DIR / "config" / "strategy.toml"
_TICK_LOG_INTERVAL = 30.0     # 종목별 시세 로그 출력 간격(초)
_MAX_IDLE_SLEEP = 300.0       # 대기 중 시계를 다시 확인하는 최대 간격 (PC 절전 복귀 대비)
_PROGRESS_SAVE_SEC = 60.0     # 장중 정산 중간 저장 간격(초)


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


async def log_deposit(account: AccountService, is_real: bool) -> None:
    """예수금 상세는 참고용 → 조회 실패해도 매매는 계속한다."""
    try:
        d = await account.fetch_deposit()
    except KisError as e:
        log.warning("예수금 상세 조회 실패 (매매에는 영향 없음): %s", e)
        return
    log.info(f"예수금 USD: ${d.usd_deposit:,.2f}  출금가능 ${d.usd_withdrawable:,.2f} "
             f"(익일 ${d.usd_withdrawable_next:,.2f})")
    log.info(f"예수금 KRW: {d.krw_deposit:,.0f}원  출금가능 {d.krw_withdrawable:,.0f}원  "
             f"총자산 {d.total_asset_krw:,.0f}원")
    if not is_real and not d.usd_deposit:
        log.info("  (모의투자는 이 조회에서 USD 예수금이 0으로 나옴 → 주문에는 위 주문가능 USD 를 사용)")


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
            f"매수 {tick.bid:,.2f}({tick.bid_size:,.0f}) / 매도 {tick.ask:,.2f}({tick.ask_size:,.0f})  누적 {tick.cum_volume:,.0f}주  "
            f"[{tick.market_type} {t}]"
        )


class AccountContext:
    """전략에 계좌 상태를 읽기 전용으로 제공 (잔고 스냅샷 + 진행 중인 매수 주문)."""

    def __init__(self, account: AccountService, orders: OrderManager, book: PositionBook | None = None):
        self._account = account
        self._orders = orders
        self._book = book

    def position(self, symbol: str) -> Holding | None:
        snap = self._account.snapshot
        return snap.holdings.get(symbol) if snap else None

    def position_count(self) -> int:
        snap = self._account.snapshot
        held = set(snap.holdings) if snap else set()
        return len(held | self._orders.pending_buy_symbols())   # 체결 대기 중인 매수도 보유로 계산

    def buys_today(self) -> int:
        """오늘 이 프로그램이 낸 매수 횟수 (체결됐거나 진행 중인 것. 체결 없이 취소·거부된 주문은 제외)."""
        return sum(self._orders.buys_today(s) for s in {o.symbol for o in self._orders.orders})

    def last_sell_at(self, symbol: str) -> datetime | None:
        return self._orders.last_sell_at(symbol)

    def entry_time(self, symbol: str) -> datetime | None:
        """보유 종목을 산 시각 (시간 청산용): 오늘 매수 체결 시각 → 없으면 산 옵션 기록의 매수 시각."""
        if (t := self._orders.last_buy_at(symbol)):
            return t
        rec = self._book.entries().get(symbol) if self._book else None
        try:
            return datetime.fromisoformat(rec["bought_at"]) if rec else None
        except (KeyError, ValueError):
            return None


async def warm_up(client: KisClient, strategy: MovingAverageCrossStrategy,
                  watchlist: list[WatchItem]) -> None:
    """과거 분봉으로 이동평균 계산에 필요한 봉을 미리 채운다. 실패한 종목은 실시간 봉으로 채워진다.
    종목을 동시에 조회한다 (요청 간격은 KisClient 가 지킴) → 40종목도 모의서버에서 1분 안팎."""
    log.info("과거 분봉 조회 중 (%d종목)...", len(watchlist))

    async def one(item: WatchItem) -> None:
        try:
            bars = await fetch_minute_bars(client, item, strategy.p.bar_minutes)
        except KisError as e:
            log.warning("[%s] 과거 분봉 조회 실패 → 실시간 봉으로 채움: %s", item.symbol, e)
            return
        strategy.seed(item.symbol, bars)
        log.info("[%s] 과거 %d분봉 %d개 로드 (전략 준비 %s)", item.symbol, strategy.p.bar_minutes,
                 len(bars), "완료" if strategy.ready(item.symbol) else "부족")
        if minutes := strategy.exit_minutes(item.symbol):   # 산 옵션의 봉 단위가 다르면 데드크로스용 봉도
            try:
                strategy.seed_exit(item.symbol, await fetch_minute_bars(client, item, minutes))
            except KisError as e:
                log.warning("[%s] 산 옵션 기준 %d분봉 조회 실패 → 실시간 봉으로 채움: %s", item.symbol, minutes, e)

    await asyncio.gather(*(one(item) for item in watchlist))


class GainerSelector:
    """급등주 선정: 프리마켓·정규장 동안 refresh_min 분마다 상승률 순위로 다시 고르고 감시·매수 대상을 바꾼다.
    빠진 종목은 신규 매수만 멈추고, 보유 중이면 산 옵션의 매도 기준으로 계속 감시한다."""

    def __init__(self, client: KisClient, cfg: LiveConfig, clock: MarketClock, trading_date: date):
        self._c, self._cfg, self._clock, self._date = client, cfg, clock, trading_date
        self._log = GainerLog(BASE_DIR / "data", client.settings.env)
        self._master = load_master_info(BASE_DIR / "data" / "universe") if self.params.enabled else None
        if self.params.enabled and self._master is None:
            log.warning("[급등주] 종목 마스터 파일이 없어 ETF 를 이름으로만 거릅니다 (--collect-daily 한 번 실행 권장)")
        self.watcher: HoldingWatcher | None = None
        self.current: list[Gainer] = []
        self.picked_at: datetime | None = None
        self._next = 0.0
        self._task: asyncio.Task | None = None

    @property
    def params(self) -> GainersParams:
        return self._cfg.params("gainers")

    @property
    def symbols(self) -> set[str]:
        return {g.symbol for g in self.current}

    def active(self, session: Session) -> bool:
        return self.params.enabled and session in (Session.PRE, Session.REGULAR)

    async def select(self) -> list[Gainer]:
        p = self.params
        if self._master is None:
            self._master = load_master_info(BASE_DIR / "data" / "universe")
        rows = await fetch_rank_rows(self._c)
        if not any(rows.values()):   # 장중에 순위가 통째로 비면 서버 문제로 보고 기존 목록 유지
            raise KisError("상승률 순위를 하나도 받지 못함")
        picked = pick(rows, p, self._master)
        old = self.symbols
        self.current, self.picked_at = picked, self._clock.now()
        self._next = time.monotonic() + p.refresh_min * 60
        try:
            self._log.append(self._date, picked)
        except OSError as e:
            log.warning("[급등주] 선정 기록 저장 실패: %s", e)
        new = [g.symbol for g in picked if g.symbol not in old]
        gone = sorted(old - self.symbols)
        log.info("[급등주] %d종목 선정 (등락률 %g%% 이상, $%g 이상, 거래량 %s주 이상, %d분 뒤 다시): %s%s%s",
                 len(picked), p.min_change_rate, p.min_price, f"{p.min_volume:,}", p.refresh_min,
                 ", ".join(f"{g.symbol} {g.rate:+.1f}%" for g in picked) or "조건에 맞는 종목 없음",
                 f" | 새로 {', '.join(new)}" if old and new else "", f" | 빠짐 {', '.join(gone)}" if gone else "")
        return picked

    def check(self, session: Session) -> None:
        """매매 루프에서 매초 호출. 때가 되면 다시 고르기를 백그라운드로 (틱 처리를 막지 않게)."""
        if not self.active(session) or time.monotonic() < self._next or (self._task and not self._task.done()):
            return
        self._next = time.monotonic() + 60   # 실패해도 1분 뒤 재시도
        self._task = asyncio.create_task(self._refresh(), name="gainers")

    async def _refresh(self) -> None:
        before = self.symbols
        try:
            picked = await self.select()
        except KisError as e:
            log.warning("[급등주] 다시 고르기 실패 → 1분 뒤 재시도 (기존 목록 유지): %s", e)
            return
        if not self.watcher:
            return
        try:
            for sym in sorted(before - self.symbols):
                await self.watcher.release_target(sym, "급등주에서 빠짐")
            for g in picked:
                if g.symbol not in before:
                    await self.watcher.add_target(g.item)
        except Exception:
            log.exception("[급등주] 감시 종목 바꾸기 실패 (다음 선정 때 다시 맞춤)")

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def view(self) -> dict:
        p = self.params
        return {"enabled": p.enabled, "refresh_min": p.refresh_min, "top_n": p.top_n,
                "min_change_rate": p.min_change_rate, "min_price": p.min_price, "min_volume": p.min_volume,
                "picked_at": self.picked_at.astimezone(KST).strftime("%H:%M") if self.picked_at else None,
                "rows": [{"symbol": g.symbol, "exchange": g.exchange, "name": g.name, "rate": round(g.rate, 2),
                          "price": g.price, "volume": g.volume} for g in self.current]}


def build_trading_list(clock: MarketClock, watchlist: list[WatchItem], account: AccountService,
                       orders: OrderManager, gp: GainersParams | None = None,
                       gainers: list[WatchItem] = ()) -> tuple[list[WatchItem], set[str]]:
    """오늘 실시간으로 받을 종목: 보유(익절·손절 감시) > 미체결 > 급등주 > 관심(watchlist.csv) > 전날 종목 선정 결과.
    반환: (감시 종목, 매수 대상 종목 = 급등주 + 관심 + 선정). 매수 대상이 아닌 종목은 보유·미체결이 끝나면 감시를 해제한다.
    급등주 선정이 켜져 있고 '관심·선정 종목도 매매'가 꺼져 있으면 관심·선정 종목은 빼고 급등주만 매수 대상."""
    only_gainers = bool(gp and gp.enabled and not gp.include_watchlist)
    if only_gainers:
        watchlist = []
    snap = account.snapshot
    held = [WatchItem(h.symbol, h.exchange, h.name) for h in (snap.holdings.values() if snap else [])
            if h.exchange in QUOTE_EXCHANGE]
    pending = [WatchItem(o.symbol, o.signal.exchange) for o in orders.open_orders()
               if o.signal.exchange in QUOTE_EXCHANGE]
    screened: list[WatchItem] = []
    try:
        screener = ScreenerParams.from_toml(_STRATEGY_PATH)
    except (OSError, ValueError) as e:
        log.warning("[screener] 설정 오류 → 선정 종목 제외: %s", e)
    else:
        if screener.add_to_watchlist and not only_gainers:
            day = clock.last_closed_trading_day()
            path = BASE_DIR / "data" / "screen" / f"{day.isoformat()}.csv"
            screened = load_screen_result(path)
            if not path.exists():
                log.info("%s 종목 선정 결과 없음 (--collect-daily → --screen 실행 시 추가됨)", day)
    merged = merge_watchlists([("보유", held), ("미체결", pending), ("급등", list(gainers)), ("관심", watchlist),
                               ("선정", screened)])
    return merged, {w.symbol for w in (*gainers, *watchlist, *screened)}


class HoldingWatcher:
    """잔고가 바뀌면 실시간 감시 목록을 맞춘다 (키움 강의: 잔고 0 → 잔고 딕셔너리 삭제 + SetRealRemove).

    - 새로 보유한 종목(HTS 직접 매수 등)이 감시 밖이면 실시간 등록 → 익절·손절 감시
      (한도 40종목이 차 있으면 보유·미체결이 아닌 가장 낮은 순위 종목을 빼고 넣는다)
    - 매수 대상(관심·선정)이 아닌데 보유·미체결 때문에 감시하던 종목은 다 팔리면 실시간 해제
    """

    def __init__(self, client: KisClient, ws: KisWebSocket, strategy: MovingAverageCrossStrategy,
                 account: AccountService, orders: OrderManager, items: list[WatchItem], targets: set[str]):
        self._c, self._ws, self._strategy = client, ws, strategy
        self._account, self._orders = account, orders
        self.subscribed: dict[str, WatchItem] = {w.symbol: w for w in items}   # 우선순위 순서 유지
        self._targets = targets
        self._last_snap = account.snapshot
        self._tasks: set[asyncio.Task] = set()
        self.keep: Callable[[str], bool] = lambda _sym: False   # 프리마켓 가상 매매 보유 종목도 감시 유지

    async def check(self) -> None:
        snap = self._account.snapshot
        if snap is None or snap is self._last_snap:
            return   # 잔고가 다시 조회됐을 때만 (체결 반영 시 refresh 가 새 스냅샷을 만든다)
        self._last_snap = snap
        held = {s: h for s, h in snap.holdings.items() if h.quantity > 0 and h.exchange in QUOTE_EXCHANGE}
        pending = {o.symbol for o in self._orders.open_orders()}

        for sym in [s for s in self.subscribed if s not in self._targets and s not in held and s not in pending
                    and not self.keep(s)]:
            await self._drop(sym, "보유 0주")
        for sym, h in held.items():
            if sym in self.subscribed:
                continue
            if len(self.subscribed) >= MAX_SYMBOLS:
                victim = next((s for s in reversed(self.subscribed) if s not in held and s not in pending), None)
                if victim is None:
                    log.warning("[감시 추가 실패] %s: 실시간 한도 %d종목이 모두 보유·미체결 종목", sym, MAX_SYMBOLS)
                    continue
                await self._drop(victim, f"한도 {MAX_SYMBOLS}종목 → 보유 종목 {sym} 우선")
            item = WatchItem(sym, h.exchange, h.name)
            self.subscribed[sym] = item
            self._strategy.add_item(item)
            await self._ws.subscribe_ticks(sym, item.quote_exchange)
            log.info("[감시 추가] 새 보유 종목 %s %g주 → 실시간 시세 등록, 익절·손절 감시 (%d종목)",
                     sym, h.quantity, len(self.subscribed))
            task = asyncio.create_task(warm_up(self._c, self._strategy, [item]), name=f"warm-{sym}")
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def add_target(self, item: WatchItem) -> bool:
        """매수 대상 추가 (급등주 새로 선정): 실시간 등록 + 과거 분봉 로드. 한도가 차 있으면 False."""
        self._targets.add(item.symbol)
        if item.symbol in self.subscribed:
            return True
        if len(self.subscribed) >= MAX_SYMBOLS:
            log.warning("[감시 추가 실패] %s: 실시간 한도 %d종목", item.symbol, MAX_SYMBOLS)
            self._targets.discard(item.symbol)
            return False
        self.subscribed[item.symbol] = item
        self._strategy.add_item(item)
        await self._ws.subscribe_ticks(item.symbol, item.quote_exchange)
        log.info("[감시 추가] 급등주 %s → 실시간 시세 등록 (%d종목)", item.symbol, len(self.subscribed))
        task = asyncio.create_task(warm_up(self._c, self._strategy, [item]), name=f"warm-{item.symbol}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def release_target(self, sym: str, why: str) -> None:
        """매수 대상에서 뺀다. 보유·미체결이면 감시는 계속 (다 팔리면 check() 가 해제)."""
        self._targets.discard(sym)
        snap = self._account.snapshot
        h = snap.holdings.get(sym) if snap else None
        if sym in self.subscribed and not (h and h.quantity > 0) and not self._orders.open_orders(sym) \
                and not self.keep(sym):
            await self._drop(sym, why)
        elif sym in self.subscribed:
            log.info("[급등주] %s 빠짐 → 신규 매수만 중지, 보유 중이라 매도 기준으로 계속 감시", sym)

    async def _drop(self, sym: str, why: str) -> None:
        item = self.subscribed.pop(sym)
        await self._ws.unsubscribe_ticks(sym, item.quote_exchange)
        self._strategy.remove_item(sym)
        log.info("[감시 해제] %s: %s → 실시간 시세 해제 (%d종목)", sym, why, len(self.subscribed))

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)


async def stream_quotes(client: KisClient, account: AccountService, clock: MarketClock,
                        watchlist: list[WatchItem], cfg: LiveConfig, store: SettlementStore, risk_store: RiskStore,
                        until: datetime | timedelta, dash: DashboardState | None = None,
                        presets: PresetStore | None = None, book: PositionBook | None = None) -> bool:
    """until 시각까지 (timedelta 면 준비가 끝난 시점부터 그 시간 동안) 실시간 시세를 받아 전략을 실행하고 주문한다.
    하루 목표 수익을 달성해 청산까지 끝나면 그 자리에서 거래를 마감하고 정산한다 (반환값 True).
    거래일마다 전략·주문 관리자를 새로 만들고, 끝날 때(오류·중단 포함) 미체결 취소 후 정산한다.
    도중에 설정이 바뀌면(대시보드·파일 수정) 봉 단위·이동평균 기간을 뺀 나머지를 바로 반영한다."""
    cfg.poll(force=True)
    trading_date = clock.now().astimezone(ET).date()
    start_snap = account.snapshot
    params = cfg.ma.with_budget_from(start_snap.orderable_usd if start_snap else None)
    order_params = cfg.order
    orders = OrderManager(client, account, order_params)
    try:
        await orders.load_open_orders()
    except KisError as e:
        log.warning("기존 미체결 주문 조회 실패 (계속 진행, 중복 주문 주의): %s", e)
    guard = RiskGuard(cfg.risk, risk_store, account, orders, trading_date, order_params.order_timeout_sec)
    await guard.start(guard.equity(), start_cash=start_snap.orderable_usd if start_snap else None)
    selector = GainerSelector(client, cfg, clock, trading_date)
    gp = selector.params
    if gp.enabled:
        if selector.active(clock.session()):
            try:
                await selector.select()
            except KisError as e:
                log.warning("[급등주] 첫 선정 실패 → 1분 뒤 다시 시도: %s", e)
        else:
            log.info("[급등주] %s 에는 고르지 않습니다 (프리마켓·정규장에만 선정)", clock.session().label)
    watchlist, targets = build_trading_list(clock, watchlist, account, orders, gp, [g.item for g in selector.current])
    strategy = MovingAverageCrossStrategy(params, watchlist)
    if presets:
        strategy.preset_info = presets.info        # 매수 신호에 지금 켜진 거래 옵션을 붙인다
    if book:
        strategy.exit_rule = book.get              # 보유 종목은 산 때 옵션 기준으로 매도
        book.sync(orders.orders, account.snapshot)
    ctx = AccountContext(account, orders, book)
    guard.target_pct = lambda: strategy.p.daily_profit_target_pct   # 하루 목표 수익 (거래 옵션 값)
    if book or presets:   # 청산 매도도 그 종목을 산 옵션으로 기록 (옵션별 성적)
        def _sell_meta(sym: str) -> dict:
            rule = book.get(sym) if book else None
            pid, name = (rule.preset_id, rule.preset_name) if rule else (presets.info() if presets else ("", ""))
            return {"preset_id": pid, "preset_name": name}
        guard.sell_meta = _sell_meta

    def on_config(c: LiveConfig, changes: list[Change]) -> None:
        nonlocal params
        # 봉 단위·이동평균 기간은 오늘 쌓은 봉과 맞물려 있어 그대로 두고 (다음 거래일부터), 나머지는 바로 적용
        new = replace(c.ma, bar_minutes=params.bar_minutes, short_window=params.short_window,
                      long_window=params.long_window)
        keys = {ch.field.key for ch in changes if ch.field.section == "ma_cross"}
        if keys & {"capital_usage_pct", "budget_per_trade_usd", "max_positions", "off"}:   # off: 사용 비율·종목 수 켬/끔
            new = new.with_budget_from(start_snap.orderable_usd if start_snap else None)   # 시작 시 주문가능 USD 기준
        else:
            new = replace(new, budget_per_trade_usd=params.budget_per_trade_usd)
        params = strategy.p = new
        orders.p = c.order          # 주문 미전송·실전 허용 여부(orders.dry_run)는 시작 때 정해진 값 유지
        guard.set_params(c.risk, c.order.order_timeout_sec)
        if dash:
            dash.ma_params = new

    unsubscribe = cfg.subscribe(on_config)
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
    watcher = HoldingWatcher(client, ws, strategy, account, orders, watchlist, targets)
    selector.watcher = watcher
    if gp.enabled:   # 급등주 모드: 지금 매수 대상(급등주 + 설정에 따라 관심·선정)인 종목만 새로 산다
        strategy.can_buy = lambda sym: sym in targets
        # 급등주는 이미 전일 대비 등락률로 골랐으므로 거래 옵션의 '등락률 범위'(예: 0~5%)로 다시 막지 않는다
        strategy.rate_exempt = lambda sym: sym in selector.symbols
    if dash:
        dash.attach_day(trading_date, orders, guard, strategy, ws, watcher, targets, params)
        dash.gainers = selector
    ws_task = asyncio.create_task(ws.run(), name="kis-websocket")
    order_task = asyncio.create_task(orders.run(), name="order-sync")
    reporter = TickReporter()
    book_snap = None
    session = clock.session()
    paper = not client.settings.is_real
    # 모의투자 프리마켓 가상 매매: 모의 서버가 프리마켓 주문을 받지 않으므로 신호를 가상 계좌에서 체결해 손익을 본다
    sim: PremarketSim | None = None
    if paper and cfg.params("premarket_sim").enabled:
        sim = PremarketSim(SimStore(BASE_DIR / "data" / "sim", client.settings.env), trading_date,
                           start_snap.orderable_usd if start_snap else 0.0,
                           fees=lambda: cfg.fees, params=lambda: cfg.params("premarket_sim"))
        if session is not Session.PRE:
            sim.close_all("프리마켓이 끝난 뒤 시작")   # 프리마켓 도중 꺼졌던 기록이 있으면 정리 (없으면 아무 일 없음)
        watcher.keep = sim.holds
    if dash:
        dash.sim = sim

    def sim_active(s: Session) -> bool:
        return sim is not None and s is Session.PRE and not sim.closed

    def order_session(s: Session) -> Session:
        """모의투자 서버는 정규장 주문만 받는다 (프리·애프터마켓 주문은 '40570000 모의투자 장시작전' 으로 거부).
        모의투자에서는 정규장 밖을 휴장처럼 다뤄 봉·지표만 쌓고 신호·주문은 내지 않는다."""
        return Session.CLOSED if paper and s is not Session.REGULAR else s

    def note_paper_session(s: Session) -> None:
        if sim_active(s) and s in strategy.p.sessions:
            log.info("[모의투자] 프리마켓은 KIS 모의 서버가 주문을 받지 않아 가상 계좌로 매매합니다 (시뮬레이션, 체결가 %g%% 불리하게) "
                     "→ 정규장 시작 때 가상 보유 종목은 모두 정리", cfg.params("premarket_sim").slippage_pct)
        elif paper and s in (Session.PRE, Session.AFTER) and s in strategy.p.sessions:
            log.info("[모의투자] %s 에는 주문하지 않습니다 (KIS 모의 서버는 정규장 주문만 받음) "
                     "→ 시세로 봉·지표만 쌓고 정규장부터 매매", s.label)

    note_paper_session(session)
    target_done = False
    next_save = time.monotonic() + _PROGRESS_SAVE_SEC
    log.info("실시간 시세 수신 시작: %d종목, %s 까지", len(watchlist), fmt_kst(until))
    try:
        while clock.now() < until:
            if ws_task.done():
                ws_task.result()   # 복구 불가 예외면 여기서 전파
                break
            await watcher.check()
            selector.check(session)
            if book and account.snapshot is not book_snap:   # 잔고가 새로 조회되면 산 옵션 기록을 맞춘다
                book_snap = account.snapshot
                book.sync(orders.orders, book_snap)
            current = clock.session()
            if current != session:
                log.info("세션 전환: %s → %s", session.label, current.label)
                if sim and session is Session.PRE:
                    sim.close_all("프리마켓 종료")
                session = current
                note_paper_session(session)
            if time.monotonic() >= next_save:   # 장중 중간 저장 → 도중에 꺼져도 정산이 남는다
                next_save = time.monotonic() + _PROGRESS_SAVE_SEC
                save_progress(store, account, orders.orders, trading_date, start_snap)
            await guard.check(tradable=order_session(session) is not Session.CLOSED)
            if guard.halt_kind is HaltKind.DAILY_TARGET and guard.liquidated:
                log.warning("[하루 목표 달성] 청산 완료 → 오늘 거래를 마감하고 바로 정산합니다", extra=NOTIFY)
                target_done = True
                break
            cfg.poll()
            try:
                tick = await asyncio.wait_for(ws.ticks.get(), timeout=1.0)
            except TimeoutError:
                continue
            reporter.on_tick(tick)
            guard.on_tick(tick.symbol, tick.price)
            if dash:
                dash.on_tick(tick)
            orders.on_tick(tick.symbol, tick.price)
            if sim_active(session) and clock.session() is Session.PRE:   # 가상 계좌로 전략 실행 (봉은 이 한 번만 쌓인다), 실제 주문은 내지 않음
                sim.on_tick(tick)
                keep_rule, strategy.exit_rule = strategy.exit_rule, (lambda _sym: None)   # 가상 보유는 현재 옵션 기준
                try:
                    for signal in strategy.on_tick(tick, session, sim):
                        log.info("[시뮬 신호] %s", signal)
                        sim.execute(signal, tick)
                except Exception:
                    log.exception("[%s] 프리마켓 가상 매매 처리 중 오류 → 이 틱은 건너뜀", tick.symbol)
                finally:
                    strategy.exit_rule = keep_rule
                continue
            try:
                signals = strategy.on_tick(tick, order_session(session), ctx)
            except Exception:
                log.exception("[%s] 전략 처리 중 오류 → 이 틱은 건너뜀", tick.symbol)
                continue
            for signal in signals:
                log.info("[신호] %s", signal)
                orders.submit(signal)
    finally:
        unsubscribe()
        if sim:
            if clock.session() is Session.PRE:
                sim.checkpoint()   # 프리마켓 도중 종료: 상태만 저장 (같은 거래일에 다시 켜면 이어서)
            else:
                sim.close_all("프리마켓 종료")
            if dash:
                dash.sim = None
        if book:
            book.sync(orders.orders, None)
        await selector.close()
        await watcher.close()
        await orders.close()
        order_task.cancel()
        await ws.stop()
        await asyncio.gather(ws_task, order_task, return_exceptions=True)
        log.info("실시간 시세 수신 종료 (수신 틱 %d건)", reporter.count)
        report = await settle_day(store, account, orders.orders, trading_date, start_snap,
                                  final=target_done or clock.now() >= until)
        guard.on_settled(report.end_equity if report else None)
        if dash:
            dash.detach_day()
    return target_done


async def wait_after_target(clock: MarketClock, risk_store: RiskStore, until: datetime) -> bool:
    """목표 달성 마감 후 거래일 끝까지 대기. 그 사이 재개(대시보드·--resume)되면 True 를 돌려 다시 매매한다.
    대기 중에는 손실 차단 장치가 없으므로 도착한 명령은 상태 파일에 직접 반영한다."""
    while clock.now() < until:
        for cmd in risk_store.take_commands():
            log.info("[명령] %s → %s", cmd, apply_command_offline(risk_store, cmd))
        if not risk_store.load().get("halt"):
            return True
        await asyncio.sleep(min(10.0, max(0.0, (until - clock.now()).total_seconds())))
    return False


async def sleep_until(clock: MarketClock, target: datetime) -> None:
    """target 까지 대기. 벽시계를 주기적으로 다시 확인해 절전 복귀/시간 보정에도 정확하다."""
    while (remaining := (target - clock.now()).total_seconds()) > 0:
        await asyncio.sleep(min(remaining, _MAX_IDLE_SLEEP))


async def run_forever(client: KisClient, account: AccountService, clock: MarketClock,
                      watchlist: list[WatchItem], cfg: LiveConfig, store: SettlementStore,
                      risk_store: RiskStore, once: bool = False, dash: DashboardState | None = None,
                      presets: PresetStore | None = None, book: PositionBook | None = None) -> None:
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
            if dash:
                dash.waiting(nxt)
            await sleep_until(clock, nxt.start)
            continue

        log.info("현재 세션: %s", window)
        try:
            log_snapshot(await account.refresh())
        except KisError as e:
            log.error("세션 시작 잔고 동기화 실패 (계속 진행): %s", e)
        await log_deposit(account, client.settings.is_real)

        day_end = clock.trading_day_end(now)
        assert day_end is not None
        try:
            target_done = await stream_quotes(client, account, clock, watchlist, cfg, store, risk_store,
                                              until=day_end, dash=dash, presets=presets, book=book)
        except Exception:
            log.exception("시세 수신 중 예기치 못한 오류 → 30초 후 재시작")
            await asyncio.sleep(30)
            continue
        if target_done and clock.now() < day_end:   # 목표 달성으로 일찍 마감 → 거래일이 끝날 때까지 쉰다
            log.info("오늘 매매 마감 (하루 목표 달성). 거래일 종료 %s 까지 대기 (재개하면 다시 매매)", fmt_kst(day_end))
            if dash and (nxt := clock.next_window(day_end)):
                dash.waiting(nxt)
            if await wait_after_target(clock, risk_store, day_end):
                log.warning("[재개] 하루 목표 달성 중지가 해제됨 → 오늘 매매를 다시 시작합니다", extra=NOTIFY)
                continue
        log.info("거래일 종료 (%s)", fmt_kst(day_end))
        await collect_after_close(client, clock, watchlist)
        if once:
            log.info("--once: 거래일 정산을 마치고 종료합니다")
            return


async def run_collect_daily(client: KisClient, clock: MarketClock, limit: int | None) -> int:
    """미국 전체 종목(주식, $1 이상)의 일봉을 모은다. 이미 받은 종목은 건너뛰므로 다시 실행하면 이어서 진행."""
    if clock.session() is not Session.CLOSED:
        log.warning("지금은 %s 시간입니다. 매매 프로그램과 동시에 돌리면 요청 한도를 나눠 쓰게 되니 장 마감 후 실행을 권장합니다",
                    clock.session().label)
    try:
        items = await load_universe(BASE_DIR / "data" / "universe")
    except Exception as e:   # 마스터 파일을 한 번도 못 받은 경우
        log.critical("종목 목록을 가져오지 못했습니다: %s", e)
        return 1
    if limit:
        items = items[:limit]
    last_day = clock.last_closed_trading_day()
    log.info("일봉 수집 시작: %d종목, %s 까지 %d일치", len(items), last_day, DEFAULT_DAILY_COUNT)
    stats = await collect_daily(client, items, BASE_DIR / "data" / "daily", last_day)
    return 0 if stats.failed < max(1, stats.total // 10) else 1


async def run_screener(clock: MarketClock) -> int:
    """수집된 일봉으로 종목 선정 (서버 요청 없음, 종목 목록만 필요)."""
    try:
        params = ScreenerParams.from_toml(_STRATEGY_PATH)
        items = await load_universe(BASE_DIR / "data" / "universe")
    except Exception as e:
        log.critical("종목 선정 준비 실패: %s", e)
        return 1
    run_screen(items, BASE_DIR / "data" / "daily", clock.last_closed_trading_day(), params,
               BASE_DIR / "data" / "screen")
    return 0


MINUTE_STORE = MinuteStore(BASE_DIR / "data" / "minute")
RUN_STORE = RunStore(BASE_DIR / "data" / "backtest")


def minute_targets(clock: MarketClock, watchlist: list[WatchItem]) -> list[WatchItem]:
    """분봉을 모을 종목: 관심 종목 + 최근 종목 선정 결과 + 이미 모으고 있는 종목 (계속 이어 붙이도록)."""
    day = clock.last_closed_trading_day()
    screened = load_screen_result(BASE_DIR / "data" / "screen" / f"{day.isoformat()}.csv")
    stored = [WatchItem(s, "NASD") for s in MINUTE_STORE.symbols()]
    known = {w.symbol: w for w in (*stored, *screened, *watchlist)}   # 거래소 정보는 관심·선정 쪽을 우선
    return [known[s] for s in sorted(known)]


async def collect_after_close(client: KisClient, clock: MarketClock, watchlist: list[WatchItem]) -> None:
    """거래일이 끝나면 백테스트용 분봉을 이어 받는다 ([backtest] collect_after_close). 실패해도 매매에는 영향 없음."""
    try:
        with _STRATEGY_PATH.open("rb") as f:
            conf = tomllib.load(f).get("backtest", {})
    except (OSError, ValueError):
        conf = {}
    if not conf.get("collect_after_close", True):
        return
    try:
        items = minute_targets(clock, watchlist)
        log.info("[분봉 수집] 장 마감 후 백테스트용 분봉 이어 받기: %d종목", len(items))
        res = await collect_minutes(client, MINUTE_STORE, items, int(conf.get("collect_days", 22)))
        log.info("[분봉 수집] 완료: +%d봉, 실패 %s", res["added"], res["failed"] or "없음")
    except Exception:
        log.exception("[분봉 수집] 오류 → 건너뜀 (매매에는 영향 없음)")


def cli_backtest(cfg: LiveConfig, days: int) -> int:
    from backtest.runs import data_coverage
    cov = data_coverage(MINUTE_STORE)
    if not cov["symbols"]:
        log.error("저장된 분봉이 없습니다. 먼저 python main.py --collect-minute 을 실행하세요")
        return 1
    end = date.fromisoformat(cov["last"])
    start, n = end, 1
    while n < days and start > date.fromisoformat(cov["first"]):
        start -= timedelta(days=1)
        n += MarketClock.is_trading_day(start)
    try:
        r = run_backtest(cfg, MINUTE_STORE, RUN_STORE, start=start, end=end)
    except ValueError as e:
        log.error("백테스트 실패: %s", e)
        return 1
    log.info(f"===== 백테스트 {r['period']['start']} ~ {r['period']['end']} ({r['period']['days']}거래일, {len(r['symbols'])}종목) =====")
    log.info(f"수익률 {r['return_pct']:+.2f}% (단순 보유 {r['benchmark_return_pct']:+.2f}%), 최대 낙폭 {r['max_drawdown_pct']:.2f}%")
    log.info(f"거래 {r['trades']}건, 승률 {r['win_rate'] if r['win_rate'] is not None else '-'}%, "
             f"손익비 {r['profit_factor'] or '-'}, 실현 ${r['realized_pnl']:+,.2f}, 수수료 ${r['fees']:,.2f}")
    for e in r["by_exit"]:
        log.info(f"  {e['exit']}: {e['count']}건 ${e['pnl']:+,.2f}")
    log.info("결과 저장: data/backtest/%s.json (대시보드 백테스트 탭에서 비교)", r["id"])
    return 0


def show_annual(store: SettlementStore, cfg: LiveConfig, which: str) -> int:
    if which == "latest":
        year = available_years(store)[0]
    else:
        try:
            year = int(which)
        except ValueError:
            log.error("연도 형식 오류 (예: --annual 2026): %s", which)
            return 2
    result = compute_annual(store, year, cfg.fees, cfg.tax)
    log_annual(result)
    log.info("매도 상세 저장: %s", save_annual(store, result))
    return 0


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
    p.add_argument("--annual", nargs="?", const="latest", metavar="YYYY",
                   help="연간 정산 (수수료·원화 환산·예상 양도소득세) 출력 후 data/reports/ 에 저장")
    p.add_argument("--collect-minute", nargs="?", const=22, type=int, metavar="DAYS",
                   help="백테스트용 1분봉 수집 (관심·선정·이미 저장된 종목, 기본 22거래일) 후 종료")
    p.add_argument("--backtest", nargs="?", const=20, type=int, metavar="DAYS",
                   help="저장된 분봉의 최근 DAYS 거래일로 현재 설정 백테스트 후 종료 (기본 20)")
    p.add_argument("--collect-daily", action="store_true", help="미국 전체 종목 일봉 수집 후 종료")
    p.add_argument("--limit", type=int, metavar="N", help="--collect-daily 를 앞쪽 N종목만 (테스트용)")
    p.add_argument("--screen", action="store_true",
                   help="수집한 일봉으로 종목 선정 후 종료 (--collect-daily 와 함께 쓰면 수집 → 선정 순서로 실행)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--kill", action="store_true",
                   help="긴급 정지: 미체결 주문 전부 취소 + 보유 종목 전부 매도 + 신규 매수 중지 (--resume 까지 유지)")
    g.add_argument("--pause", action="store_true", help="일시정지: 신규 매수만 중지 (매도는 계속, --resume 까지 유지)")
    g.add_argument("--resume", action="store_true", help="손실 한도·긴급 정지·일시정지로 멈춘 매매를 재개")
    g.add_argument("--risk-status", action="store_true", help="손실 차단 상태 출력")
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        settings = load_settings()
        watchlist = load_watchlist(_WATCHLIST_PATH)
        cfg = LiveConfig.load(_STRATEGY_PATH)
        params, order_params, risk_params = cfg.ma, cfg.order, cfg.risk
    except (ValueError, OSError) as e:
        print(f"[설정 오류] {e}", file=sys.stderr)
        return 2

    setup_logging(settings.log_dir, settings.log_level)
    store = SettlementStore(BASE_DIR / "data", settings.env)
    risk = (risk_params, RiskStore(BASE_DIR / "data" / "risk", settings.env))
    if args.report:
        return show_report(store, args.report)
    if args.annual:
        return show_annual(store, cfg, args.annual)
    if args.backtest:
        return cli_backtest(cfg, args.backtest)
    if args.risk_status:
        for line in describe_state(risk[1].load()):
            log.info(line)
        return 0
    setup_slack(settings.slack_webhook_url, prefix="[모의] " if not settings.is_real else "[실전] ")
    if args.kill or args.resume or args.pause:
        command = "kill" if args.kill else "pause" if args.pause else "resume"
        return await run_risk_command(command, settings, MarketClock(), order_params, store, risk)
    job_label = "분봉 수집" if args.collect_minute else "일봉 수집·종목 선정" if (args.collect_daily or args.screen) else "매매"
    log.info("=== vibe-stock 시작: %s (환경: %s, 계좌: %s-**) ===", job_label, settings.env.upper(),
             settings.cano[:4] + "****", extra=NOTIFY)
    if not watchlist:
        log.critical("매매 대상 종목이 없습니다: %s", _WATCHLIST_PATH)
        return 2
    log.info("매매 대상 %d종목: %s", len(watchlist), ", ".join(w.symbol for w in watchlist))
    log.info("전략: 이동평균 돌파 (%d분봉 MA%d/MA%d, %s, %s %s, 세션 %s)",
             params.bar_minutes, params.short_window, params.long_window,
             f"등락률 {params.min_change_rate:.1f}~{params.max_change_rate:.1f}%" if params.uses("change_rate") else "등락률 제한 끔",
             f"익절 {params.take_profit_pct:.1f}%" if params.take_profit_pct else "익절 끔",
             f"손절 {params.stop_loss_pct:.1f}%" if params.stop_loss_pct else "손절 끔",
             "/".join(s.label for s in params.sessions))
    if params.off:
        log.info("꺼 둔 기능: %s", ", ".join(MA_TOGGLES[n] for n in params.off))
    if order_params.dry_run or (settings.is_real and not order_params.allow_real_trading):
        log.warning("주문 모드: 미전송 (신호만 기록) — config/strategy.toml [order] 확인")
    else:
        log.info("주문 모드: %s 계좌로 실제 주문 전송", "실전" if settings.is_real else "모의투자")

    clock = MarketClock()
    # 같은 작업이 겹쳐 실행되지 않게 (작업 스케줄러·더블클릭 중복 등). 매매와 일봉 수집은 서로 다른 잠금
    job = "daily" if (args.collect_daily or args.screen or args.collect_minute) else "trader"
    try:
        with InstanceLock(BASE_DIR / "data" / f"{job}.{settings.env}.lock"):
            return await run_job(args, settings, clock, watchlist, cfg, store, risk[1])
    except AlreadyRunning as e:
        log.error("%s → 이번 실행은 종료합니다", e)
        return 3


async def run_risk_command(command: str, settings, clock: MarketClock, order_params: OrderParams,
                           store: SettlementStore, risk: tuple[RiskParams, RiskStore]) -> int:
    """--kill / --resume. 매매 프로그램이 실행 중이면 명령 파일로 전달하고, 꺼져 있으면 직접 처리한다
    (긴급 정지는 장중이면 이 실행에서 바로 청산까지)."""
    label = {"kill": "긴급 정지", "pause": "일시정지", "resume": "매매 재개"}[command]
    lock = InstanceLock(BASE_DIR / "data" / f"trader.{settings.env}.lock")
    try:
        lock.acquire(wait_sec=0.5)
    except AlreadyRunning:
        risk[1].send(command)
        log.warning("[%s] 실행 중인 매매 프로그램에 명령 전달 → 장중이면 몇 초 안에, 장 외 시간이면 다음 세션 시작 시 처리",
                    label, extra=NOTIFY)
        return 0
    try:
        log.warning("[%s] 매매 프로그램이 꺼져 있음 → %s", label, apply_command_offline(risk[1], command),
                    extra=NOTIFY)
        if command != "kill":
            return 0
        if clock.session() is Session.CLOSED:
            log.warning("[긴급 정지] 지금은 휴장 → 매매 프로그램을 다음에 실행하면 장이 열릴 때 청산합니다")
            return 0
        return await liquidate_now(settings, clock, order_params, store, risk)
    finally:
        lock.release()


async def liquidate_now(settings, clock: MarketClock, order_params: OrderParams, store: SettlementStore,
                        risk: tuple[RiskParams, RiskStore], max_sec: float = 600.0) -> int:
    """매매 프로그램 없이 긴급 정지 청산: 미체결 전부 취소 → 보유 전부 매도 → 체결 확인 (최대 10분)."""
    async with KisClient(settings) as client:
        account = AccountService(client)
        try:
            start_snap = await bootstrap(client, account)
        except KisError as e:
            log.critical("[긴급 정지] 계좌 조회 실패 → 청산하지 못했습니다. HTS/앱에서 직접 정리하세요: %s", e,
                         extra=NOTIFY)
            return 1
        log_snapshot(start_snap)
        trading_date = clock.now().astimezone(ET).date()
        orders = OrderManager(client, account, order_params)
        try:
            await orders.load_open_orders()
        except KisError as e:
            log.warning("기존 미체결 주문 조회 실패: %s", e)
        guard = RiskGuard(*risk, account, orders, trading_date, order_params.order_timeout_sec)
        await guard.start(guard.equity())
        order_task = asyncio.create_task(orders.run(), name="order-sync")
        deadline = time.monotonic() + max_sec
        try:
            await orders.cancel_all_open(include_adopted=True)
            while time.monotonic() < deadline and clock.session() is not Session.CLOSED:
                await guard.check(tradable=True)
                if guard.liquidated:
                    break
                if orders.dry_run and orders.orders:
                    log.warning("[긴급 정지] 주문 미전송 모드 → 청산 주문은 기록만 하고 종료")
                    break
                await asyncio.sleep(1)
            else:
                log.error("[긴급 정지] 시간 안에 청산을 끝내지 못했습니다. 남은 종목은 HTS/앱에서 확인하세요",
                          extra=NOTIFY)
        finally:
            await orders.close()
            order_task.cancel()
            await asyncio.gather(order_task, return_exceptions=True)
            report = await settle_day(store, account, orders.orders, trading_date, start_snap)
            guard.on_settled(report.end_equity if report else None)
    return 0


async def run_job(args: argparse.Namespace, settings, clock: MarketClock, watchlist: list[WatchItem],
                  cfg: LiveConfig, store: SettlementStore, risk_store: RiskStore) -> int:
    if args.collect_minute:
        async with KisClient(settings) as client:
            await client.tokens.get_token()
            res = await collect_minutes(client, MINUTE_STORE, minute_targets(clock, watchlist), args.collect_minute)
        log.info("분봉 수집 완료: %d종목, +%d봉, 실패 %s", res["symbols"], res["added"], res["failed"] or "없음")
        return 0 if not res["failed"] else 1
    if args.collect_daily:
        async with KisClient(settings) as client:
            rc = await run_collect_daily(client, clock, args.limit)
        if not args.screen:
            return rc
        # 수집이 일부 실패해도 받은 종목으로 선정은 진행 (최신 일봉이 없는 종목은 선정에서 자동 제외)
        return await run_screener(clock) or rc
    if args.screen:
        return await run_screener(clock)
    async with KisClient(settings) as client:
        account = AccountService(client)
        try:
            snap = await bootstrap(client, account)
        except KisError as e:
            log.critical("초기화 실패로 종료: %s", e)
            return 1
        log.info("토큰 만료 예정: %s", client.tokens.expires_at)
        log_snapshot(snap)
        await log_deposit(account, client.settings.is_real)
        log.info("현재 시장 상태: %s", clock.session().label)

        presets = PresetStore(BASE_DIR / "config" / "presets.toml", cfg)
        book = PositionBook(BASE_DIR / "data" / "positions", settings.env)
        log.info("거래 옵션: '%s' 켜짐 (%s), 전체 %d개", presets.active_name, presets.summary(presets.active_id),
                 len(presets.presets))
        dash, dash_server = start_dashboard(settings, clock, cfg, risk_store, watchlist, store, account, presets, book)
        try:
            if args.stream_test:
                await stream_quotes(client, account, clock, watchlist, cfg, store, risk_store,
                                    until=timedelta(seconds=args.stream_test), dash=dash, presets=presets, book=book)
                return 0
            await run_forever(client, account, clock, watchlist, cfg, store, risk_store, once=args.once, dash=dash,
                              presets=presets, book=book)
        finally:
            if dash_server:
                await dash_server.stop()
                logging.getLogger().removeHandler(dash.events)
    return 0


def start_dashboard(settings, clock: MarketClock, cfg: LiveConfig, risk_store: RiskStore,
                    watchlist: list[WatchItem], store: SettlementStore, account: AccountService,
                    presets: PresetStore | None = None, book: PositionBook | None = None) -> tuple[DashboardState | None, DashboardServer | None]:
    """[dashboard] enabled 면 웹 화면 서버를 매매와 같은 이벤트 루프에서 띄운다."""
    try:
        dparams = DashboardParams.from_toml(_STRATEGY_PATH)
    except (OSError, ValueError) as e:
        log.warning("[dashboard] 설정 오류 → 대시보드 끔: %s", e)
        return None, None
    if not dparams.enabled:
        return None, None
    dash = DashboardState(settings.env, clock, cfg.ma, cfg.risk, {w.symbol for w in watchlist}, risk_store)
    dash.account = account
    dash.config = cfg
    dash.presets, dash.book = presets, book
    cfg.subscribe(lambda c, _changes: dash.on_config(c))
    logging.getLogger().addHandler(dash.events)
    server = DashboardServer(dash, store, dparams, settings.dashboard_password)
    server.start()
    return dash, server


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        logging.getLogger("main").info("사용자 중단")

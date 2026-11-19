"""매매 전략: 진입 방식 4가지(이평 교차 · 박스 돌파 · VWAP 눌림 · 시가 범위 돌파) + 공통 매수 조건 + 청산 규칙.

진입 (봉 마감 시점, entry_mode)
  ma_cross : 단기 이평이 장기 이평을 상향 돌파(골든크로스)
  breakout : 마감 봉 종가가 직전 breakout_bars 개 봉의 최고가를 돌파 (+ 거래량 volume_mult 배 이상)
  vwap     : 단기 이평 > 장기 이평(상승 추세)인데 봉이 VWAP 까지 눌렸다가(저가 ≤ VWAP) VWAP 위 양봉으로 마감
  orb      : 정규장 시작 후 orb_minutes 분의 고가(시가 범위)를 처음 돌파 (+ 거래량 volume_mult 배 이상)
공통 매수 조건: 세션 · 전일 대비 등락률 범위 · 최대 보유 종목 · 하루 최대 매수 · 매도 후 재매수 대기 · 1회 예산,
  (실시간에서만) 체결강도 하한 · 호가 스프레드 상한 · 장 마감 정리 시간대에는 사지 않음
청산 (보유 종목은 산 때 거래 옵션 기준, strategy/positions.py)
  틱마다: 손절 → 익절 → 추적 손절(고점이 +활성% 이상 오른 뒤 고점 대비 −N%) → 시간 청산 → 장 마감 전 정리
  봉 마감: 데드크로스 (이평 교차로 산 종목만)
"""
from __future__ import annotations

import logging
import time
import tomllib
from dataclasses import dataclass, fields, replace
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable

from core.ws_client import Tick
from market.bars import MAX_HISTORY_BARS, Bar, BarBuilder
from market.clock import ET, KST, MarketClock, Session
from market.watchlist import WatchItem
from strategy.base import Side, Signal, Strategy, StrategyContext
from strategy.positions import ExitRule

log = logging.getLogger(__name__)

ENTRY_MODES = {"ma_cross": "이평 교차", "breakout": "박스 돌파", "vwap": "VWAP 눌림 반등", "orb": "시가 범위 돌파"}
_REGULAR_OPEN = dtime(9, 30)


@dataclass(frozen=True)
class MaCrossParams:
    bar_minutes: int = 1
    short_window: int = 5
    long_window: int = 20
    min_change_rate: float = 0.0
    max_change_rate: float = 5.0
    take_profit_pct: float = 3.0
    stop_loss_pct: float = 2.0
    budget_per_trade_usd: float = 1000.0
    capital_usage_pct: float = 0.0   # > 0 이면 1회 예산 = 주문가능 USD × 이 비율 ÷ max_positions (고정 예산 대신)
    max_positions: int = 5
    cooldown_sec: float = 300.0
    sessions: tuple[Session, ...] = (Session.REGULAR,)
    daily_profit_target_pct: float = 0.0   # 오늘 손익이 하루 시작 예수금의 이 비율 이상 → 전부 매도, 그날 종료 (0 = 끔)
    max_buys_per_day: int = 0              # 하루 매수 횟수 한도 (0 = 제한 없음, 매도는 막지 않음)
    rebuy_wait_sec: float = 0.0            # 같은 종목 매도 체결 후 다시 살 때까지 기다릴 시간(초) (0 = 바로 가능)
    # ── 스캘핑 ──
    entry_mode: str = "ma_cross"           # ma_cross / breakout / vwap / orb
    breakout_bars: int = 20                # 박스 돌파: 직전 몇 개 봉의 최고가를 넘으면 매수
    orb_minutes: int = 15                  # 시가 범위 돌파: 정규장 시작 후 몇 분을 범위로 볼지
    volume_mult: float = 0.0               # 돌파 봉 거래량이 평균의 이 배 이상일 때만 (0 = 끔)
    trailing_activate_pct: float = 0.0     # 추적 손절: 고점이 매입가 대비 이만큼 오르면 켜짐 (0 = 끔)
    trailing_pct: float = 0.5              # 추적 손절: 고점 대비 이만큼 밀리면 매도
    max_hold_min: float = 0.0              # 시간 청산: 산 뒤 이 시간(분)이 지나면 매도 (0 = 끔)
    flatten_before_close_min: float = 0.0  # 마지막 선택 세션이 끝나기 이 시간(분) 전에 전부 매도, 그 뒤로는 매수 안 함 (0 = 끔)
    min_strength: float = 0.0              # (실시간만) 체결강도가 이 값 이상일 때만 매수 (0 = 끔, 100 = 매수·매도 균형)
    max_spread_pct: float = 0.0            # (실시간만) 매수·매도 호가 차이가 이 % 이하일 때만 매수 (0 = 끔)

    @classmethod
    def from_toml(cls, path: Path) -> MaCrossParams:
        with path.open("rb") as f:
            return cls.from_dict(tomllib.load(f).get("ma_cross", {}))

    @classmethod
    def from_dict(cls, raw: dict) -> MaCrossParams:
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"[ma_cross]: 알 수 없는 설정 {sorted(unknown)}")
        raw = dict(raw)
        if "sessions" in raw:
            try:
                raw["sessions"] = tuple(s if isinstance(s, Session) else Session(str(s).upper()) for s in raw["sessions"])
            except ValueError:
                raise ValueError("sessions 는 PRE / REGULAR / AFTER 중에서 골라야 합니다") from None
        params = cls(**raw)
        params.validate()
        return params

    def validate(self) -> None:
        if not 0 < self.short_window < self.long_window:
            raise ValueError("short_window < long_window 이어야 합니다")
        if self.long_window + 1 > MAX_HISTORY_BARS:
            raise ValueError(f"long_window 는 {MAX_HISTORY_BARS - 1} 이하여야 합니다 (과거 분봉 조회 한도)")
        if self.min_change_rate > self.max_change_rate:
            raise ValueError("min_change_rate <= max_change_rate 이어야 합니다")
        if self.bar_minutes not in (1, 2, 3, 5, 10, 15, 30, 60):
            raise ValueError("bar_minutes 는 1/2/3/5/10/15/30/60 중 하나여야 합니다")
        if not 0 <= self.capital_usage_pct <= 100:
            raise ValueError("capital_usage_pct 는 0~100 이어야 합니다")
        if self.max_positions < 1:
            raise ValueError("max_positions 는 1 이상이어야 합니다")
        if not self.sessions:
            raise ValueError("sessions 는 하나 이상 골라야 합니다")
        if self.take_profit_pct <= 0 or self.stop_loss_pct <= 0:
            raise ValueError("take_profit_pct, stop_loss_pct 는 0 보다 커야 합니다")
        if self.budget_per_trade_usd <= 0 or self.cooldown_sec < 0:
            raise ValueError("budget_per_trade_usd > 0, cooldown_sec >= 0 이어야 합니다")
        if not 0 <= self.daily_profit_target_pct <= 100 or self.max_buys_per_day < 0 or self.rebuy_wait_sec < 0:
            raise ValueError("daily_profit_target_pct 는 0~100, max_buys_per_day 는 0 이상이어야 합니다")
        if self.entry_mode not in ENTRY_MODES:
            raise ValueError(f"entry_mode 는 {' / '.join(ENTRY_MODES)} 중 하나여야 합니다")
        if not 2 <= self.breakout_bars < MAX_HISTORY_BARS or not 1 <= self.orb_minutes <= 120:
            raise ValueError(f"breakout_bars 는 2~{MAX_HISTORY_BARS - 1}, orb_minutes 는 1~120 이어야 합니다")
        if min(self.volume_mult, self.trailing_activate_pct, self.max_hold_min, self.flatten_before_close_min,
               self.min_strength, self.max_spread_pct) < 0:
            raise ValueError("스캘핑 값은 0 이상이어야 합니다")
        if self.trailing_activate_pct and self.trailing_pct <= 0:
            raise ValueError("추적 손절을 켜면 trailing_pct 는 0 보다 커야 합니다")

    @property
    def is_scalp(self) -> bool:
        return self.entry_mode != "ma_cross"

    def with_budget_from(self, orderable_usd: float | None) -> MaCrossParams:
        """capital_usage_pct 가 켜져 있으면 주문가능 USD 로 종목당 1회 예산을 정한다.
        거래일 시작 시 한 번만 계산 → 매수할수록 예산이 줄어드는 일이 없다."""
        if self.capital_usage_pct <= 0:
            return self
        if not orderable_usd or orderable_usd <= 0:
            log.warning("주문가능 USD 를 알 수 없어 고정 예산 $%s 사용", f"{self.budget_per_trade_usd:,.0f}")
            return self
        usable = orderable_usd * self.capital_usage_pct / 100
        budget = usable / self.max_positions
        log.info(f"매수 자금: 주문가능 ${orderable_usd:,.2f} × {self.capital_usage_pct:g}% = ${usable:,.2f} "
                 f"→ 종목당 ${budget:,.2f} (최대 {self.max_positions}종목)")
        return replace(self, budget_per_trade_usd=budget)


def _sma(values: list[float], n: int) -> float:
    return sum(values[-n:]) / n


def _et(b: Bar) -> datetime:
    return b.start.astimezone(ET)


def vwap_of(bars: list[Bar]) -> float | None:
    """오늘(마지막 봉의 미국 날짜) 정규장 봉의 VWAP. 정규장 전이면 그날 봉 전체 (거래량이 없으면 None)."""
    if not bars:
        return None
    last = _et(bars[-1])
    day = [b for b in bars if _et(b).date() == last.date()]
    if last.time() >= _REGULAR_OPEN:
        day = [b for b in day if _et(b).time() >= _REGULAR_OPEN]
    vol = sum(b.volume for b in day)
    if vol <= 0:
        return None
    return sum((b.high + b.low + b.close) / 3 * b.volume for b in day) / vol


def opening_range(bars: list[Bar], minutes: int) -> tuple[float, list[Bar]] | None:
    """오늘 정규장 시작 후 minutes 분 동안의 (최고가, 그 봉들). 범위가 아직 다 안 찼으면 None."""
    if not bars:
        return None
    last = _et(bars[-1])
    start = datetime.combine(last.date(), _REGULAR_OPEN, ET)
    end = start + timedelta(minutes=minutes)
    rng = [b for b in bars if start <= _et(b) < end]
    if not rng or last < end:   # 마지막 마감 봉이 범위 이후여야 범위가 확정
        return None
    return max(b.high for b in rng), rng


class MovingAverageCrossStrategy(Strategy):
    name = "ma_cross"

    def __init__(self, params: MaCrossParams, watchlist: list[WatchItem]):
        self.p = params
        self._items = {w.symbol: w for w in watchlist}
        self._bars = {w.symbol: BarBuilder(params.bar_minutes) for w in watchlist}
        self._exit_bars: dict[str, BarBuilder] = {}   # 산 옵션의 봉 단위가 지금과 다른 보유 종목의 데드크로스용 봉
        self._cooldown_until: dict[str, float] = {}
        self._peak: dict[str, float] = {}             # 추적 손절용: 보유 중 최고가
        self._last_tick: dict[str, Tick] = {}
        self._clock = MarketClock()
        self._session_end: dict[tuple[date, tuple], datetime | None] = {}
        self.now = time.monotonic   # 신호 간격 계산용 시계 (백테스트는 가상 시각으로 바꿔 끼운다)
        # 거래 옵션: 매수 신호에 붙일 현재 옵션 (id, 이름) / 보유 종목의 산 때 매도 기준 (없으면 현재 값)
        self.preset_info: Callable[[], tuple[str, str]] = lambda: ("", "")
        self.exit_rule: Callable[[str], ExitRule | None] = lambda _symbol: None

    def add_item(self, item: WatchItem) -> None:
        """거래일 중간에 감시 종목 추가 (예: HTS 에서 새로 산 종목의 익절·손절 감시)."""
        if item.symbol not in self._bars:
            self._items[item.symbol] = item
            self._bars[item.symbol] = BarBuilder(self.p.bar_minutes)

    def remove_item(self, symbol: str) -> None:
        for d in (self._items, self._bars, self._exit_bars, self._cooldown_until, self._peak, self._last_tick):
            d.pop(symbol, None)

    def seed(self, symbol: str, bars: list[Bar]) -> None:
        if symbol in self._bars:
            self._bars[symbol].seed(bars)

    def exit_minutes(self, symbol: str) -> int | None:
        """산 옵션의 봉 단위가 지금 봉 단위와 다르면 그 값 (데드크로스용 과거 봉을 따로 채워야 함)."""
        rule = self.exit_rule(symbol)
        if not rule or rule.entry_mode != "ma_cross":
            return None
        return rule.bar_minutes if rule.bar_minutes != self.p.bar_minutes else None

    def seed_exit(self, symbol: str, bars: list[Bar]) -> None:
        minutes = self.exit_minutes(symbol)
        if minutes and symbol in self._items:
            self._exit_builder(symbol, minutes).seed(bars)

    def _need_bars(self) -> int:
        if self.p.entry_mode == "breakout":
            return self.p.breakout_bars
        if self.p.entry_mode == "orb":
            return 1
        return self.p.long_window

    def ready(self, symbol: str) -> bool:
        return len(self._bars[symbol].closed) > self._need_bars()

    def indicators(self, symbol: str) -> dict | None:
        """화면 표시용: 마감된 봉 기준 단기·장기 이동평균과 추세(단기가 위면 up)."""
        builder = self._bars.get(symbol)
        if builder is None:
            return None
        closes = builder.closes()
        s, l = self.p.short_window, self.p.long_window
        ma_s = _sma(closes, s) if len(closes) >= s else None
        ma_l = _sma(closes, l) if len(closes) >= l else None
        trend = None if ma_s is None or ma_l is None else ("up" if ma_s > ma_l else "down")
        return {"ma_short": ma_s, "ma_long": ma_l, "trend": trend, "bars": len(closes),
                "ready": len(closes) > self._need_bars()}

    def scalp_metrics(self, symbol: str) -> dict | None:
        """스캘핑 화면용: VWAP·돌파 기준가·시가 범위 고가·거래량 배수·체결강도·스프레드."""
        builder = self._bars.get(symbol)
        if builder is None:
            return None
        bars = list(builder.closed)
        tick = self._last_tick.get(symbol)
        n = self.p.breakout_bars
        prev = bars[-1 - n:-1] if len(bars) > n else []
        avg_vol = sum(b.volume for b in prev) / len(prev) if prev else 0
        orb = opening_range(bars, self.p.orb_minutes)
        return {
            "vwap": vwap_of(bars),
            "breakout_level": max(b.high for b in bars[-n:]) if len(bars) >= n else None,
            "orb_high": orb[0] if orb else None,
            "volume_ratio": (bars[-1].volume / avg_vol) if bars and avg_vol > 0 else None,
            "strength": tick.strength if tick and tick.strength else None,
            "spread_pct": _spread(tick),
            "bars": len(bars),
        }

    # ── 틱 처리 ─────────────────────────────────────────────
    def on_tick(self, tick: Tick, session: Session, ctx: StrategyContext) -> list[Signal]:
        builder = self._bars.get(tick.symbol)
        if builder is None:
            return []
        self._last_tick[tick.symbol] = tick
        closed_bar = builder.update(tick)   # 세션과 무관하게 봉은 계속 쌓는다
        pos = ctx.position(tick.symbol)
        rule = self.exit_rule(tick.symbol) if pos else None
        if not pos:
            self._peak.pop(tick.symbol, None)
        alt_closed = None
        if rule and rule.entry_mode == "ma_cross" and rule.bar_minutes != self.p.bar_minutes:   # 산 옵션의 봉 단위로 따로 쌓는다
            alt_closed = self._exit_builder(tick.symbol, rule.bar_minutes).update(tick)

        if session not in self.p.sessions:
            return []
        cooling = self._in_cooldown(tick.symbol)
        # 스캘핑 방식으로 산 종목의 청산은 신호 간격에 막히지 않는다 (손절이 늦어지지 않게)
        scalp_pos = bool(pos) and ((rule.entry_mode if rule else self.p.entry_mode) != "ma_cross")
        signal = None
        if pos and (not cooling or scalp_pos):
            signal = self._check_exit(tick, pos, rule, ctx)
            if signal is None and closed_bar is not None and self.ready(tick.symbol) and not cooling:
                if (rule.entry_mode if rule else self.p.entry_mode) == "ma_cross" and not alt_closed \
                        and not (rule and rule.bar_minutes != self.p.bar_minutes):
                    signal = self._check_dead(tick, builder.closes(), pos, rule)
            if signal is None and alt_closed is not None and not cooling:
                signal = self._check_dead(tick, self._exit_bars[tick.symbol].closes(), pos, rule)
        elif not pos and not cooling and closed_bar is not None and self.ready(tick.symbol):
            signal = self._check_entry(tick, builder, ctx)

        if signal is None:
            return []
        self._cooldown_until[tick.symbol] = self.now() + self.p.cooldown_sec
        return [signal]

    # ── 청산 ────────────────────────────────────────────────
    def _check_exit(self, tick: Tick, pos, rule: ExitRule | None, ctx: StrategyContext) -> Signal | None:
        if pos.avg_price <= 0 or pos.orderable_qty <= 0:
            return None
        r = rule or ExitRule.from_params(self.p)
        price = tick.price
        pnl_pct = (price / pos.avg_price - 1) * 100
        if pnl_pct <= -r.stop_loss_pct:
            return self._sell(tick, pos, rule, f"손절 {pnl_pct:+.2f}%")
        if pnl_pct >= r.take_profit_pct:
            return self._sell(tick, pos, rule, f"익절 {pnl_pct:+.2f}%")
        peak = self._peak[tick.symbol] = max(self._peak.get(tick.symbol, pos.avg_price), price)
        if r.trailing_activate_pct and (peak / pos.avg_price - 1) * 100 >= r.trailing_activate_pct \
                and price <= peak * (1 - r.trailing_pct / 100):
            return self._sell(tick, pos, rule, f"추적손절 {pnl_pct:+.2f}% (고점 ${peak:,.2f} 대비 −{r.trailing_pct:g}%)")
        now = tick.kst_time or datetime.now(KST)
        if r.max_hold_min and (opened := _entry_time(ctx, tick.symbol)) and \
                (now - opened).total_seconds() >= r.max_hold_min * 60:
            return self._sell(tick, pos, rule, f"시간청산 {pnl_pct:+.2f}% ({r.max_hold_min:g}분 경과)")
        if r.flatten_before_close_min and self._near_close(now, r.flatten_before_close_min):
            return self._sell(tick, pos, rule, f"마감청산 {pnl_pct:+.2f}% (세션 종료 {r.flatten_before_close_min:g}분 전)")
        return None

    def _check_dead(self, tick: Tick, closes: list[float], pos, rule: ExitRule | None) -> Signal | None:
        s = rule.short_window if rule else self.p.short_window
        l = rule.long_window if rule else self.p.long_window
        if len(closes) <= l or pos.orderable_qty <= 0:
            return None
        prev_short, prev_long = _sma(closes[:-1], s), _sma(closes[:-1], l)
        cur_short, cur_long = _sma(closes, s), _sma(closes, l)
        if prev_short >= prev_long and cur_short < cur_long:
            return self._sell(tick, pos, rule, f"데드크로스 MA{s} {cur_short:,.2f} / MA{l} {cur_long:,.2f}")
        return None

    # ── 진입 ────────────────────────────────────────────────
    def _check_entry(self, tick: Tick, builder: BarBuilder, ctx: StrategyContext) -> Signal | None:
        reason = self._entry_reason(builder)
        if reason is None:
            return None
        sym, p = tick.symbol, self.p
        now = tick.kst_time or datetime.now(KST)
        if p.flatten_before_close_min and self._near_close(now, p.flatten_before_close_min):
            log.info("[%s] %s 신호지만 세션 종료 %g분 전 정리 시간 → 매수 안 함", sym, ENTRY_MODES[p.entry_mode],
                     p.flatten_before_close_min)
            return None
        if p.rebuy_wait_sec and (sold := ctx.last_sell_at(sym)):
            left = p.rebuy_wait_sec - (now - sold).total_seconds()
            if left > 0:
                log.info("[%s] %s 신호지만 매도 후 재매수 대기 중 (%.0f초 남음, 대기 %g초)", sym, ENTRY_MODES[p.entry_mode],
                         left, p.rebuy_wait_sec)
                return None
        rate = tick.change_rate
        if not p.min_change_rate <= rate <= p.max_change_rate:
            log.info("[%s] %s 신호지만 등락률 %.2f%% 가 범위 밖 → 매수 보류", sym, ENTRY_MODES[p.entry_mode], rate)
            return None
        if p.min_strength and tick.strength and tick.strength < p.min_strength:   # 실시간만 (백테스트는 값 없음)
            log.info("[%s] %s 신호지만 체결강도 %.1f < %g → 매수 보류", sym, ENTRY_MODES[p.entry_mode], tick.strength,
                     p.min_strength)
            return None
        if p.max_spread_pct and (spread := _spread(tick)) is not None and spread > p.max_spread_pct:
            log.info("[%s] %s 신호지만 호가 스프레드 %.3f%% > %g%% → 매수 보류", sym, ENTRY_MODES[p.entry_mode], spread,
                     p.max_spread_pct)
            return None
        if ctx.position_count() >= p.max_positions:
            log.info("[%s] %s 신호지만 최대 보유 종목 수(%d) 도달", sym, ENTRY_MODES[p.entry_mode], p.max_positions)
            return None
        if p.max_buys_per_day and (n := ctx.buys_today()) >= p.max_buys_per_day:
            log.info("[%s] %s 신호지만 오늘 매수 %d회로 하루 한도(%d회) 도달", sym, ENTRY_MODES[p.entry_mode], n,
                     p.max_buys_per_day)
            return None
        qty = int(p.budget_per_trade_usd // tick.price)
        if qty < 1:
            log.info("[%s] 1주 가격($%.2f)이 1회 예산보다 큼 → 매수 보류", sym, tick.price)
            return None
        preset_id, preset_name = self.preset_info()
        meta = {"preset_id": preset_id, "preset_name": preset_name,
                "exit_rule": ExitRule.from_params(p, preset_id, preset_name).to_dict()}
        return self._signal(tick, Side.BUY, qty, f"{reason}, 등락률 {rate:+.2f}%", meta)

    def _entry_reason(self, builder: BarBuilder) -> str | None:
        """방금 마감된 봉이 진입 조건을 만족하면 매수 사유 문구, 아니면 None."""
        p = self.p
        closes = builder.closes()
        if p.entry_mode == "ma_cross":
            s, l = p.short_window, p.long_window
            prev_short, prev_long = _sma(closes[:-1], s), _sma(closes[:-1], l)
            cur_short, cur_long = _sma(closes, s), _sma(closes, l)
            if prev_short <= prev_long and cur_short > cur_long:
                return f"골든크로스 MA{s} {cur_short:,.2f} / MA{l} {cur_long:,.2f}"
            return None
        bars = list(builder.closed)
        b = bars[-1]
        if p.entry_mode == "breakout":
            prev = bars[-1 - p.breakout_bars:-1]
            level = max(x.high for x in prev)
            if b.close > level and self._volume_ok(b, prev):
                return f"박스돌파 {p.breakout_bars}봉 고가 ${level:,.2f} → 종가 ${b.close:,.2f}{self._vol_text(b, prev)}"
            return None
        if p.entry_mode == "orb":
            orb = opening_range(bars[:-1], p.orb_minutes)
            if orb is None or _et(b).date() != _et(bars[-2]).date():
                return None
            level, rng = orb
            if b.close > level and bars[-2].close <= level and self._volume_ok(b, rng):
                return f"시가범위돌파 {p.orb_minutes}분 고가 ${level:,.2f} → 종가 ${b.close:,.2f}{self._vol_text(b, rng)}"
            return None
        # vwap: 상승 추세에서 VWAP 까지 눌렸다가 VWAP 위 양봉으로 마감
        s, l = p.short_window, p.long_window
        if len(closes) < l:
            return None
        ma_s, ma_l = _sma(closes, s), _sma(closes, l)
        vw = vwap_of(bars)
        if vw and ma_s > ma_l and b.low <= vw * 1.001 and b.close > vw and b.close > b.open:
            return f"VWAP눌림반등 VWAP ${vw:,.2f}, 저가 ${b.low:,.2f} → 종가 ${b.close:,.2f} (MA{s} > MA{l})"
        return None

    def _volume_ok(self, b: Bar, ref: list[Bar]) -> bool:
        if not self.p.volume_mult:
            return True
        avg = sum(x.volume for x in ref) / len(ref) if ref else 0
        return avg > 0 and b.volume >= avg * self.p.volume_mult

    def _vol_text(self, b: Bar, ref: list[Bar]) -> str:
        avg = sum(x.volume for x in ref) / len(ref) if ref else 0
        return f", 거래량 {b.volume / avg:.1f}배" if avg > 0 else ""

    # ── 보조 ────────────────────────────────────────────────
    def _near_close(self, now: datetime, minutes: float) -> bool:
        """선택한 세션 중 오늘 마지막 세션이 끝나기 minutes 분 전부터 True."""
        et = now.astimezone(ET)
        key = (et.date(), self.p.sessions)
        if key not in self._session_end:
            ws = [w for w in self._clock.windows(et.date()) if w.session in self.p.sessions]
            self._session_end[key] = ws[-1].end if ws else None
        end = self._session_end[key]
        return end is not None and end - timedelta(minutes=minutes) <= et < end

    def _exit_builder(self, symbol: str, minutes: int) -> BarBuilder:
        b = self._exit_bars.get(symbol)
        if b is None or b.minutes != minutes:
            b = self._exit_bars[symbol] = BarBuilder(minutes)
        return b

    def _in_cooldown(self, symbol: str) -> bool:
        return self.now() < self._cooldown_until.get(symbol, 0.0)

    def _sell(self, tick: Tick, pos, rule: ExitRule | None, reason: str) -> Signal:
        preset_id, preset_name = (rule.preset_id, rule.preset_name) if rule else self.preset_info()
        if rule and rule.preset_name:
            reason += f" [{rule.preset_name} 기준]"
        return self._signal(tick, Side.SELL, int(pos.orderable_qty), reason,
                            {"preset_id": preset_id, "preset_name": preset_name})

    def _signal(self, tick: Tick, side: Side, qty: int, reason: str, meta: dict | None = None) -> Signal:
        return Signal(tick.symbol, self._items[tick.symbol].exchange, side, qty, tick.price, reason, meta=meta)


def _spread(tick: Tick | None) -> float | None:
    if tick is None or tick.bid <= 0 or tick.ask <= 0 or tick.ask < tick.bid:
        return None
    return (tick.ask - tick.bid) / ((tick.ask + tick.bid) / 2) * 100


def _entry_time(ctx: StrategyContext, symbol: str) -> datetime | None:
    fn = getattr(ctx, "entry_time", None)
    return fn(symbol) if fn else None

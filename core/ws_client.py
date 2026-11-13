"""KIS 실시간 웹소켓 클라이언트 (해외주식 실시간 체결가 HDFSCNT0).

- 수신 전담 태스크가 프레임을 읽어 Tick 으로 변환 후 asyncio.Queue 에 넣는다.
  소비자(전략 루프)가 느려도 수신이 막히지 않아 PINGPONG 응답이 밀리지 않는다.
- 연결이 끊기면 지수 백오프로 재접속하고, 원하던 구독 목록을 자동으로 다시 등록한다.
- KIS 는 앱키당 웹소켓 세션 1개만 허용한다 (같은 키로 두 프로그램 동시 실행 금지).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI

from core.api_client import KisClient
from core.exceptions import KisError
from market.clock import KST

log = logging.getLogger(__name__)

TR_TICK = "HDFSCNT0"          # 해외주식 실시간 체결가
MAX_SUBSCRIPTIONS = 41
_IDLE_TIMEOUT = 60.0          # 서버 PINGPONG 이 약 10초 주기 → 60초 무수신이면 끊긴 것으로 간주
_MAX_BACKOFF = 60.0
_MAX_HANDSHAKE_BACKOFF = 5.0

_TICK_FIELDS = (
    "rsym", "symb", "zdiv", "tymd", "xymd", "xhms", "kymd", "khms",
    "open", "high", "low", "last", "sign", "diff", "rate",
    "pbid", "pask", "vbid", "vask", "evol", "tvol", "tamt",
    "bivl", "asvl", "strn", "mtyp",
)
_MARKET_TYPE = {"1": "장중", "2": "장전", "3": "장후"}


def tick_key(symbol: str, quote_exchange: str) -> str:
    """실시간 구독 키. 예: ('AAPL', 'NAS') -> 'DNASAAPL'."""
    return f"D{quote_exchange}{symbol}"


def _f(v: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Tick:
    symbol: str
    key: str
    price: float
    open: float
    high: float
    low: float
    change: float
    change_rate: float
    bid: float
    ask: float
    volume: float          # 이번 체결량
    cum_volume: float      # 누적 거래량
    strength: float        # 체결강도
    market_type: str       # 장중/장전/장후
    kst_time: datetime | None
    received_at: float     # time.monotonic()

    @classmethod
    def parse(cls, values: list[str]) -> Tick:
        r = dict(zip(_TICK_FIELDS, values))
        sign = r.get("sign", "")
        diff, rate = _f(r.get("diff", "")), _f(r.get("rate", ""))
        if sign in ("4", "5"):   # 4: 하한, 5: 하락 → 값에 부호가 빠져 와도 음수로 (등락률은 매수 필터에 쓰임)
            diff, rate = -abs(diff), -abs(rate)
        try:
            kst_time = datetime.strptime(r["kymd"] + r["khms"], "%Y%m%d%H%M%S").replace(tzinfo=KST)
        except (KeyError, ValueError):
            kst_time = None
        return cls(
            symbol=r.get("symb", ""),
            key=r.get("rsym", ""),
            price=_f(r.get("last", "")),
            open=_f(r.get("open", "")),
            high=_f(r.get("high", "")),
            low=_f(r.get("low", "")),
            change=diff,
            change_rate=rate,
            bid=_f(r.get("pbid", "")),
            ask=_f(r.get("pask", "")),
            volume=_f(r.get("evol", "")),
            cum_volume=_f(r.get("tvol", "")),
            strength=_f(r.get("strn", "")),
            market_type=_MARKET_TYPE.get(r.get("mtyp", ""), r.get("mtyp", "")),
            kst_time=kst_time,
            received_at=time.monotonic(),
        )


class KisWebSocket:
    def __init__(self, client: KisClient, queue_size: int = 10_000):
        self._c = client
        self._url = client.settings.ws_url
        self.ticks: asyncio.Queue[Tick] = asyncio.Queue(maxsize=queue_size)
        self.last: dict[str, Tick] = {}            # 종목별 최신 체결
        self.connected = asyncio.Event()
        self._subs: set[tuple[str, str]] = set()   # 유지하려는 구독 (tr_id, tr_key)
        self._ws: ClientConnection | None = None
        self._approval_key: str = ""
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._dropped = 0
        self._notice_tr = client.settings.tr_id("overseas_notice")
        self.on_notice: Callable[[], None] | None = None   # 체결통보 수신 시 호출

    # ── 구독 관리 ───────────────────────────────────────────
    async def subscribe_ticks(self, symbol: str, quote_exchange: str) -> None:
        await self._subscribe(TR_TICK, tick_key(symbol, quote_exchange))

    async def subscribe_notices(self, hts_id: str) -> None:
        """내 계좌 체결통보 구독. 내용은 암호화되어 오지만, 수신 자체를 체결 확인 트리거로 쓴다."""
        await self._subscribe(self._notice_tr, hts_id)

    async def unsubscribe_ticks(self, symbol: str, quote_exchange: str) -> None:
        sub = (TR_TICK, tick_key(symbol, quote_exchange))
        if sub in self._subs:
            self._subs.discard(sub)
            await self._send_sub(*sub, register=False)

    async def _subscribe(self, tr_id: str, tr_key: str) -> None:
        sub = (tr_id, tr_key)
        if sub in self._subs:
            return
        if len(self._subs) >= MAX_SUBSCRIPTIONS:
            raise ValueError(f"웹소켓 구독 한도({MAX_SUBSCRIPTIONS}) 초과: {tr_key}")
        self._subs.add(sub)
        await self._send_sub(tr_id, tr_key, register=True)

    async def _send_sub(self, tr_id: str, tr_key: str, register: bool) -> None:
        ws = self._ws
        if ws is None:
            return  # 아직 미접속: 접속 직후 일괄 등록됨
        msg = {
            "header": {
                "approval_key": self._approval_key,
                "custtype": "P",
                "tr_type": "1" if register else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}},
        }
        async with self._send_lock:
            await ws.send(json.dumps(msg))

    # ── 실행/종료 ───────────────────────────────────────────
    async def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            await self._ws.close()

    async def run(self) -> None:
        """stop() 이 호출될 때까지 접속 유지 + 자동 재접속.

        - 핸드셰이크 실패: KIS 서버가 간헐적으로 업그레이드 요청을 끊는 현상이 있어 짧은 간격으로 재시도
        - 접속 후 끊김: 지수 백오프로 재접속 (구독 목록은 자동 재등록)
        """
        attempt = 0
        handshake_failures = 0
        while not self._stop.is_set():
            handshake_failed = False
            try:
                self._approval_key = await self._c.tokens.get_approval_key(force=attempt >= 3)
                # KIS 서버는 permessage-deflate 협상 헤더가 있으면 핸드셰이크를 끊어버린다 → 압축 비활성화
                # 연결 유지는 서버 PINGPONG 으로 하므로 라이브러리 ping 도 끈다
                async with connect(self._url, compression=None, ping_interval=None,
                                   open_timeout=10, close_timeout=5, user_agent_header=None) as ws:
                    self._ws = ws
                    if handshake_failures:
                        log.info("핸드셰이크 %d회 실패 후 접속 성공", handshake_failures)
                    handshake_failures = 0
                    log.info("웹소켓 접속 (%s), 구독 %d건 등록", self._url, len(self._subs))
                    for tr_id, tr_key in sorted(self._subs):
                        await self._send_sub(tr_id, tr_key, register=True)
                    self.connected.set()
                    attempt = 0
                    await self._read_loop(ws)
            except InvalidURI:
                raise
            except InvalidHandshake as e:
                handshake_failed = True
                handshake_failures += 1
                if handshake_failures % 10 == 1:
                    log.warning("웹소켓 핸드셰이크 실패 (누적 %d회, 재시도 중): %s", handshake_failures, e)
            except (ConnectionClosed, OSError, TimeoutError, KisError) as e:
                if not self._stop.is_set():
                    log.warning("웹소켓 연결 끊김: %s: %s", type(e).__name__, e)
            finally:
                self._ws = None
                self.connected.clear()

            if self._stop.is_set():
                break
            if handshake_failed:
                delay = min(1.0 + 0.5 * handshake_failures, _MAX_HANDSHAKE_BACKOFF)
            else:
                attempt += 1
                delay = min(2 ** attempt, _MAX_BACKOFF)
                log.info("%.0f초 후 웹소켓 재접속 시도 (%d회째)", delay, attempt)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
        log.info("웹소켓 종료")

    async def _read_loop(self, ws: ClientConnection) -> None:
        while not self._stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=_IDLE_TIMEOUT)
            except TimeoutError:
                raise TimeoutError(f"{_IDLE_TIMEOUT:.0f}초간 수신 없음") from None
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if raw[:1] in ("0", "1"):
                self._on_data(raw)
            else:
                await self._on_control(ws, raw)

    # ── 메시지 처리 ─────────────────────────────────────────
    def _on_data(self, raw: str) -> None:
        # 형식: 암호화여부|TR_ID|건수|필드^필드^...
        try:
            encrypted, tr_id, count, payload = raw.split("|", 3)
        except ValueError:
            log.warning("알 수 없는 데이터 프레임: %.100s", raw)
            return
        if tr_id == self._notice_tr:
            log.info("체결통보 수신 → 체결내역 확인")
            if self.on_notice is not None:
                self.on_notice()
            return
        if encrypted == "1":
            return
        if tr_id != TR_TICK:
            return
        values = payload.split("^")
        n = len(_TICK_FIELDS)
        for i in range(int(count or 1)):
            chunk = values[i * n:(i + 1) * n]
            if len(chunk) < n:
                break
            tick = Tick.parse(chunk)
            self.last[tick.symbol] = tick
            self._enqueue(tick)

    def _enqueue(self, tick: Tick) -> None:
        try:
            self.ticks.put_nowait(tick)
        except asyncio.QueueFull:
            self.ticks.get_nowait()   # 가장 오래된 틱을 버리고 최신 틱 유지
            self.ticks.put_nowait(tick)
            self._dropped += 1
            if self._dropped % 1000 == 1:
                log.warning("틱 큐 포화: 소비가 수신을 못 따라감 (누적 %d건 폐기)", self._dropped)

    async def _on_control(self, ws: ClientConnection, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except ValueError:
            log.warning("JSON 파싱 실패: %.100s", raw)
            return
        header = msg.get("header", {})
        tr_id = header.get("tr_id", "")
        if tr_id == "PINGPONG":
            async with self._send_lock:
                await ws.send(raw)
            return

        body = msg.get("body", {})
        tr_key = header.get("tr_key", "")
        msg_cd, text = body.get("msg_cd", ""), body.get("msg1", "")
        if body.get("rt_cd") == "0":
            log.info("구독 응답 [%s %s] %s", tr_id, tr_key, text)
        elif msg_cd == "OPSP0002":   # ALREADY IN SUBSCRIBE
            log.debug("이미 구독 중 [%s %s]", tr_id, tr_key)
        else:
            log.error("구독 실패 [%s %s] %s %s", tr_id, tr_key, msg_cd, text)
            if msg_cd == "OPSP8996":   # ALREADY IN USE appkey
                log.error("같은 앱키로 다른 웹소켓 세션이 접속 중입니다. 다른 실행 중인 프로그램을 종료하세요.")
            else:
                self._subs.discard((tr_id, tr_key))   # 재접속 때 반복 실패하지 않도록 제외

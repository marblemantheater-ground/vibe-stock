"""KIS REST 공통 클라이언트.

모든 요청은 `await` 로 응답을 받은 뒤에야 다음 코드가 진행되므로 순서가 보장된다.
여기에 더해:
- RateLimiter: 초당 요청 수를 제한해 EGW00201(초당 거래건수 초과)을 예방
- 토큰 만료 응답 시 자동 재발급 후 재시도
- 네트워크 오류/5xx 는 지수 백오프로 재시도. 단 주문(POST)은 서버 도달 여부가 불확실한
  오류(읽기 타임아웃 등)에서는 재시도하지 않는다 → 중복 주문 방지
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from config.settings import Settings
from core.exceptions import KisApiError, KisError, KisNetworkError
from core.token_manager import TokenManager

log = logging.getLogger(__name__)

_TOKEN_ERROR_CODES = {"EGW00121", "EGW00123"}   # 유효하지 않은 토큰 / 기간 만료 토큰
_RATE_LIMIT_CODES = {"EGW00201"}                # 초당 거래건수 초과
# 요청이 서버에 도달하지 않았음이 확실한 오류 → POST 도 재시도 안전
_SAFE_TO_RETRY = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


@dataclass
class KisResponse:
    body: dict[str, Any]
    headers: httpx.Headers

    @property
    def has_next(self) -> bool:
        """연속조회 여부 (응답 헤더 tr_cont 가 F/M 이면 다음 페이지 존재)."""
        return self.headers.get("tr_cont", "") in ("F", "M")


class RateLimiter:
    def __init__(self, rps: float):
        self._interval = 1.0 / rps
        self._lock = asyncio.Lock()
        self._next_at = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._next_at > now:
                await asyncio.sleep(self._next_at - now)
            self._next_at = max(time.monotonic(), self._next_at) + self._interval


class KisClient:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None):
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.rest_url, timeout=settings.request_timeout, transport=transport
        )
        self.tokens = TokenManager(settings, self._http)
        self._limiter = RateLimiter(settings.max_rps)

    async def __aenter__(self) -> KisClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, tr_id: str, params: dict[str, str], tr_cont: str = "") -> KisResponse:
        return await self.request("GET", path, tr_id, params=params, tr_cont=tr_cont)

    async def post(self, path: str, tr_id: str, body: dict[str, str]) -> KisResponse:
        return await self.request("POST", path, tr_id, body=body)

    async def request(
        self,
        method: str,
        path: str,
        tr_id: str,
        *,
        params: dict[str, str] | None = None,
        body: dict[str, str] | None = None,
        tr_cont: str = "",
    ) -> KisResponse:
        last_error: KisError | None = None

        for attempt in range(1, self.settings.max_retries + 1):
            token = await self.tokens.get_token()
            await self._limiter.acquire()
            headers = {
                "content-type": "application/json; charset=utf-8",
                "authorization": f"Bearer {token}",
                "appkey": self.settings.app_key,
                "appsecret": self.settings.app_secret,
                "tr_id": tr_id,
                "tr_cont": tr_cont,
                "custtype": "P",
            }

            try:
                resp = await self._http.request(method, path, params=params, json=body, headers=headers)
            except httpx.TransportError as e:
                last_error = KisNetworkError(f"[{tr_id}] {type(e).__name__}: {e}")
                if method != "GET" and not isinstance(e, _SAFE_TO_RETRY):
                    log.error("주문성 요청 전송 결과 불확실, 재시도하지 않음: %s", last_error)
                    raise last_error from e
                log.warning("네트워크 오류 (%d/%d): %s", attempt, self.settings.max_retries, last_error)
                await self._backoff(attempt)
                continue

            try:
                data = resp.json()
            except ValueError:
                data = {}

            msg_cd = data.get("msg_cd", "")
            if msg_cd in _TOKEN_ERROR_CODES:
                self.tokens.invalidate(token)
                last_error = KisApiError(msg_cd, data.get("msg1", ""), tr_id, resp.status_code)
                continue
            if msg_cd in _RATE_LIMIT_CODES:
                log.debug("초당 요청 한도 초과, 잠시 대기 후 재시도 [%s]", tr_id)
                last_error = KisApiError(msg_cd, data.get("msg1", ""), tr_id, resp.status_code)
                await asyncio.sleep(0.5 * attempt)
                continue

            if data.get("rt_cd") == "0":
                return KisResponse(body=data, headers=resp.headers)

            if "rt_cd" in data:
                # 업무 오류 (잔고 부족, 주문 불가 시간 등): 재시도 무의미
                raise KisApiError(msg_cd, data.get("msg1", "").strip(), tr_id, resp.status_code)

            # JSON 이 아니거나 rt_cd 가 없는 응답 = 게이트웨이/서버 장애
            last_error = KisNetworkError(f"[{tr_id}] HTTP {resp.status_code}: {resp.text[:200]}")
            if method != "GET":
                raise last_error
            log.warning("비정상 응답 (%d/%d): %s", attempt, self.settings.max_retries, last_error)
            await self._backoff(attempt)

        raise last_error or KisError(f"[{tr_id}] 재시도 횟수 초과")

    @staticmethod
    async def _backoff(attempt: int) -> None:
        await asyncio.sleep(min(2 ** (attempt - 1), 30))

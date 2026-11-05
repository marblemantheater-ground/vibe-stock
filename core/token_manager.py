"""접근토큰 발급·캐시·자동 갱신.

- 토큰은 data/token_{env}.json 에 캐시해 재시작 시 재사용한다 (KIS 는 발급 1분당 1회 제한).
- 만료 margin 초 전에 미리 재발급한다.
- 여러 코루틴이 동시에 만료를 감지해도 asyncio.Lock 으로 발급은 한 번만 일어난다 (single-flight).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from config.settings import Settings
from core.exceptions import KisAuthError, KisNetworkError

log = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
_EXPIRED_FMT = "%Y-%m-%d %H:%M:%S"
_ISSUE_PATH = "/oauth2/tokenP"
_APPROVAL_PATH = "/oauth2/Approval"
_ISSUE_RATE_LIMIT_CODE = "EGW00133"   # 접근토큰 발급 1분당 1회 제한
_ISSUE_MIN_INTERVAL = 61.0
_ISSUE_MAX_ATTEMPTS = 5
_SEED_DEFAULT_TTL = 3600              # 만료시각을 모르는 시드 토큰은 1시간만 신뢰


def _parse_kst(text: str) -> float:
    return datetime.strptime(text, _EXPIRED_FMT).replace(tzinfo=KST).timestamp()


class TokenManager:
    def __init__(self, settings: Settings, http: httpx.AsyncClient):
        self._s = settings
        self._http = http
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._last_issue_at: float = 0.0
        self._approval_lock = asyncio.Lock()
        self._approval_key: str | None = None
        # 캐시 파일이 다른 앱키의 토큰을 가리키지 않도록 키 지문을 함께 저장
        self._key_hint = hashlib.sha256(settings.app_key.encode()).hexdigest()[:12]
        self._load_initial()

    # ── public ──────────────────────────────────────────────
    async def get_token(self) -> str:
        if self._is_fresh():
            return self._token  # type: ignore[return-value]
        async with self._lock:
            if self._is_fresh():  # 대기 중 다른 코루틴이 이미 갱신했을 수 있음
                return self._token  # type: ignore[return-value]
            await self._issue()
            return self._token  # type: ignore[return-value]

    async def get_approval_key(self, force: bool = False) -> str:
        """웹소켓 접속키. 접속(재접속)할 때마다 쓰며 프로세스 메모리에만 보관한다."""
        async with self._approval_lock:
            if self._approval_key and not force:
                return self._approval_key
            body = {
                "grant_type": "client_credentials",
                "appkey": self._s.app_key,
                "secretkey": self._s.app_secret,
            }
            try:
                resp = await self._http.post(_APPROVAL_PATH, json=body)
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                raise KisNetworkError(f"웹소켓 접속키 발급 요청 실패: {e!r}") from e
            key = data.get("approval_key")
            if not key:
                raise KisAuthError(f"웹소켓 접속키 발급 실패: {data}")
            self._approval_key = key
            log.info("웹소켓 접속키 발급 완료")
            return key

    def invalidate(self, stale_token: str) -> None:
        """서버가 토큰 만료/무효를 알려왔을 때 호출. 이미 교체된 토큰이면 무시."""
        if self._token == stale_token:
            log.warning("서버가 토큰을 거부함 → 다음 요청 시 재발급")
            self._token = None
            self._expires_at = 0.0

    @property
    def expires_at(self) -> datetime | None:
        if not self._expires_at:
            return None
        return datetime.fromtimestamp(self._expires_at, KST)

    # ── internal ────────────────────────────────────────────
    def _is_fresh(self) -> bool:
        return bool(self._token) and time.time() < self._expires_at - self._s.token_refresh_margin_sec

    def _load_initial(self) -> None:
        path = self._s.token_cache_path
        if path.exists():
            try:
                cached = json.loads(path.read_text(encoding="utf-8"))
                if cached.get("key_hint") == self._key_hint:
                    self._token = cached["access_token"]
                    self._expires_at = float(cached["expires_at"])
                    if self._is_fresh():
                        log.info("캐시된 토큰 사용 (만료 %s)", self.expires_at)
                        return
            except (OSError, ValueError, KeyError) as e:
                log.warning("토큰 캐시 읽기 실패, 무시함: %s", e)

        if self._s.seed_access_token:
            self._token = self._s.seed_access_token
            if self._s.seed_access_token_expires:
                self._expires_at = _parse_kst(self._s.seed_access_token_expires)
            else:
                # margin 과 무관하게 지금부터 _SEED_DEFAULT_TTL 동안은 사용
                self._expires_at = time.time() + self._s.token_refresh_margin_sec + _SEED_DEFAULT_TTL
            if self._is_fresh():
                log.info(".env 의 KIS_ACCESS_TOKEN 사용 (만료 추정 %s)", self.expires_at)
                return

        self._token, self._expires_at = None, 0.0

    async def _issue(self) -> None:
        body = {
            "grant_type": "client_credentials",
            "appkey": self._s.app_key,
            "appsecret": self._s.app_secret,
        }
        last_error = ""
        for attempt in range(1, _ISSUE_MAX_ATTEMPTS + 1):
            wait = self._last_issue_at + _ISSUE_MIN_INTERVAL - time.monotonic()
            if self._last_issue_at and wait > 0:
                log.info("토큰 발급 1분 제한 대기 %.0f초", wait)
                await asyncio.sleep(wait)

            try:
                resp = await self._http.post(_ISSUE_PATH, json=body)
                data = resp.json()
            except (httpx.HTTPError, ValueError) as e:
                last_error = repr(e)
                log.warning("토큰 발급 요청 실패 (%d/%d): %s", attempt, _ISSUE_MAX_ATTEMPTS, e)
                await asyncio.sleep(min(2 ** attempt, 30))
                continue
            finally:
                self._last_issue_at = time.monotonic()

            if "access_token" in data:
                self._token = data["access_token"]
                if "access_token_token_expired" in data:
                    self._expires_at = _parse_kst(data["access_token_token_expired"])
                else:
                    self._expires_at = time.time() + int(data.get("expires_in", 86400))
                self._save_cache()
                log.info("접근토큰 발급 완료 (만료 %s)", self.expires_at)
                return

            code = data.get("error_code", "")
            last_error = f"{code} {data.get('error_description', data)}"
            log.warning("토큰 발급 거부 (%d/%d): %s", attempt, _ISSUE_MAX_ATTEMPTS, last_error)
            if code != _ISSUE_RATE_LIMIT_CODE and resp.status_code < 500:
                break  # 앱키 오류 등은 재시도해도 소용없음

        raise KisAuthError(f"접근토큰 발급 실패: {last_error}")

    def _save_cache(self) -> None:
        path = self._s.token_cache_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps({
                    "key_hint": self._key_hint,
                    "access_token": self._token,
                    "expires_at": self._expires_at,
                }),
                encoding="utf-8",
            )
            tmp.replace(path)  # 원자적 교체: 쓰다 죽어도 캐시가 깨지지 않음
        except OSError as e:
            log.warning("토큰 캐시 저장 실패 (동작에는 영향 없음): %s", e)

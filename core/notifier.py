"""슬랙 알림: 체결·오류·정산 같은 중요한 로그를 슬랙 채널로 보낸다 (무인 실행 중 휴대폰으로 확인).

키움 강의 교재의 봇 토큰(xoxb) 방식은 슬랙 정책 변경으로 발급 절차가 바뀌었다. 여기서는 그 영향을 받지 않는
Incoming Webhook URL 방식을 쓴다 — .env 에 SLACK_WEBHOOK_URL 하나만 넣으면 되고, 없으면 알림은 꺼진다.

어떤 로그를 보내나:
  - ERROR 이상 로그 전부 (예기치 못한 오류, 초기화 실패, 중복 실행 등)
  - log.info(..., extra=NOTIFY) 처럼 notify 표시가 붙은 로그 (체결, 주문 거부, 외부 주문, 시작, 정산)
전송은 별도 스레드에서 하므로 매매 루프를 막지 않고, 슬랙이 응답하지 않아도 매매에는 영향이 없다.
"""
from __future__ import annotations

import atexit
import logging
import queue
import threading
import time

import httpx

NOTIFY = {"notify": True}     # log.info("...", extra=NOTIFY)

_DEDUP_SEC = 600.0            # 같은 문구는 10분에 한 번만 (오류가 반복될 때 알림 폭주 방지)
_MIN_INTERVAL_SEC = 1.0       # 슬랙 웹훅 권장 한도: 초당 1건
_MAX_QUEUE = 200
_MAX_TEXT = 3000

log = logging.getLogger(__name__)


class SlackHandler(logging.Handler):
    def __init__(self, webhook_url: str, prefix: str = ""):
        super().__init__(level=logging.INFO)
        self._url = webhook_url
        self._prefix = prefix
        self._queue: queue.Queue[str | None] = queue.Queue(maxsize=_MAX_QUEUE)
        self._recent: dict[str, float] = {}
        self._thread = threading.Thread(target=self._worker, name="slack-notifier", daemon=True)
        self._thread.start()

    # ── logging.Handler ────────────────────────────────────
    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == __name__ or record.name.startswith(("httpx", "httpcore")):
            return False   # 알림 전송 자체의 로그가 다시 알림이 되지 않게
        return record.levelno >= logging.ERROR or bool(getattr(record, "notify", False))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            now = time.monotonic()
            if now - self._recent.get(msg, -_DEDUP_SEC) < _DEDUP_SEC:
                return
            self._recent[msg] = now
            if len(self._recent) > 500:
                self._recent = {k: v for k, v in self._recent.items() if now - v < _DEDUP_SEC}
            icon = ":rotating_light:" if record.levelno >= logging.ERROR else (
                ":warning:" if record.levelno >= logging.WARNING else ":white_check_mark:")
            text = f"{icon} {self._prefix}{msg}"
            if record.exc_info and record.levelno >= logging.ERROR:
                exc = record.exc_info[1]
                text += f"\n`{type(exc).__name__}: {exc}`"
            self._queue.put_nowait(text[:_MAX_TEXT])
        except queue.Full:
            pass   # 슬랙이 막혀 쌓였으면 버린다 (로그 파일에는 남아 있음)
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        """남은 알림을 최대 5초 동안 보내고 종료."""
        if self._thread.is_alive():
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            self._thread.join(timeout=5)
        super().close()

    # ── 전송 스레드 ────────────────────────────────────────
    def _worker(self) -> None:
        with httpx.Client(timeout=10) as http:
            while (text := self._queue.get()) is not None:
                try:
                    resp = http.post(self._url, json={"text": text})
                    if resp.status_code == 429:   # 한도 초과 → 안내된 시간만큼 쉬고 한 번 더
                        time.sleep(float(resp.headers.get("Retry-After") or 5))
                        resp = http.post(self._url, json={"text": text})
                    if resp.status_code >= 400:
                        log.warning("슬랙 알림 실패: HTTP %d %s", resp.status_code, resp.text[:100])
                except httpx.HTTPError as e:
                    log.warning("슬랙 알림 실패: %s", type(e).__name__)
                time.sleep(_MIN_INTERVAL_SEC)


def setup_slack(webhook_url: str | None, prefix: str = "") -> SlackHandler | None:
    """웹훅 URL 이 있으면 루트 로거에 슬랙 핸들러를 붙인다."""
    if not webhook_url:
        return None
    if not webhook_url.startswith("https://hooks.slack.com/"):
        log.warning("SLACK_WEBHOOK_URL 형식이 아닙니다 (https://hooks.slack.com/... 이어야 함) → 슬랙 알림 꺼짐")
        return None
    handler = SlackHandler(webhook_url, prefix)
    logging.getLogger().addHandler(handler)
    atexit.register(handler.close)
    log.info("슬랙 알림 켜짐")
    return handler

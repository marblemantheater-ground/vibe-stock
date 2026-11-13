"""중복 실행 방지: 같은 환경(모의/실전)의 매매 프로그램이 이미 돌고 있으면 새로 시작하지 않는다.

KIS 는 앱키당 웹소켓 1개만 허용하고, 두 프로그램이 같은 신호로 주문을 이중으로 낼 수 있다.
작업 스케줄러·더블클릭이 겹쳐 실행되는 경우를 막는다. OS 파일 잠금을 쓰므로 프로그램이 비정상 종료돼도
잠금이 자동으로 풀린다 (남은 잠금 파일을 손으로 지울 필요 없음).
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path


class AlreadyRunning(RuntimeError):
    pass


class InstanceLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def acquire(self, wait_sec: float = 3.0) -> None:
        """잠금을 얻는다. wait_sec 동안 재시도 (Windows 는 강제 종료된 프로세스의 잠금을 조금 늦게 푼다)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        deadline = time.monotonic() + wait_sec
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    fh.close()
                    raise AlreadyRunning(f"이미 실행 중인 프로그램이 있습니다 (잠금 파일: {self.path})") from None
                time.sleep(0.2)
        fh.seek(0)
        fh.truncate()
        fh.write(str(os.getpid()).encode())
        fh.flush()
        self._fh = fh

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()

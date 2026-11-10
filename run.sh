#!/usr/bin/env bash
# macOS / Linux 실행 스크립트: 가상환경 준비 → 패키지 설치 → main.py 실행
# 사용법: ./run.sh [--stream-test 60]
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
    echo "[setup] 가상환경 생성 (.venv)"
    "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install -q --disable-pip-version-check -r requirements.txt
exec .venv/bin/python main.py "$@"

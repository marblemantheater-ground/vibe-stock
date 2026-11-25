"""대시보드 웹 서버 (FastAPI): 매매 프로그램 안에서 같이 돌며 상태를 브라우저 화면에 보낸다.

- 이 PC(127.0.0.1)에서만 접속된다. 로그인 비밀번호는 .env 의 DASHBOARD_PASSWORD
  (비어 있으면 실행할 때마다 임시 비밀번호를 만들어 콘솔 창에만 보여 준다)
- 화면(React)은 web/dist 의 빌드 결과물을 그대로 제공한다 → 실행하는 PC 에는 Node.js 가 필요 없다
- 현황·보유·주문·감시 종목·이벤트(실시간 스트림), 정산 보고서
- 일시정지·재개·긴급 정지, 매매 설정 변경(즉시 적용) → 로그인 + 전용 헤더가 있어야 한다
- 웹 서버에 문제가 생겨도 매매는 계속된다 (포트 사용 중이면 대시보드만 끄고 진행)
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import secrets
import socket
import sys
import tomllib
from dataclasses import dataclass, fields
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse

from backtest.runs import RunStore, data_coverage, run_backtest
from config.settings import BASE_DIR
from market.minute import MinuteStore
from report.annual import FeeParams, TaxParams, available_years, compute_annual, sells_csv
from report.settlement import SettlementStore, preset_performance
from sim.premarket import PremarketSim, SimStore, summary
from web.scalp import scalp_view, template_values
from web.state import DashboardState

log = logging.getLogger(__name__)

DIST_DIR = BASE_DIR / "web" / "dist"
_COOKIE = "vibe_session"
_STREAM_INTERVAL_SEC = 1.0


@dataclass(frozen=True)
class DashboardParams:
    enabled: bool = True
    port: int = 8765

    @classmethod
    def from_toml(cls, path: Path) -> DashboardParams:
        with path.open("rb") as f:
            raw = tomllib.load(f).get("dashboard", {})
        unknown = set(raw) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError(f"{path.name} [dashboard]: 알 수 없는 설정 {sorted(unknown)}")
        p = cls(**raw)
        if not 1024 <= p.port <= 65535:
            raise ValueError("[dashboard] port 는 1024~65535 사이여야 합니다")
        return p


def create_app(state: DashboardState, store: SettlementStore, password: str,
               stopping: Callable[[], bool] = lambda: False, data_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="vibe-stock", docs_url=None, redoc_url=None, openapi_url=None)
    sessions: set[str] = set()
    data_dir = data_dir or BASE_DIR / "data"
    minutes, runs = MinuteStore(data_dir / "minute"), RunStore(data_dir / "backtest")
    bt = {"running": False, "progress": 0.0, "started_at": None, "error": None, "last_id": None}

    def authed(request: Request) -> None:
        if request.cookies.get(_COOKIE) not in sessions:
            raise HTTPException(401, "로그인이 필요합니다")

    @app.post("/api/login")
    async def login(request: Request, response: Response):
        try:
            body = await request.json()
        except ValueError:
            body = {}
        given = str(body.get("password", "")) if isinstance(body, dict) else ""
        if not hmac.compare_digest(given.encode(), password.encode()):
            await asyncio.sleep(1.0)   # 비밀번호 대입 속도 제한
            log.warning("대시보드 로그인 실패")
            raise HTTPException(401, "비밀번호가 맞지 않습니다")
        token = secrets.token_urlsafe(32)
        sessions.add(token)
        response.set_cookie(_COOKIE, token, httponly=True, samesite="strict")
        return {"ok": True}

    @app.post("/api/logout")
    async def logout(request: Request, response: Response):
        sessions.discard(request.cookies.get(_COOKIE, ""))
        response.delete_cookie(_COOKIE)
        return {"ok": True}

    @app.get("/api/snapshot")
    async def snapshot(request: Request):
        authed(request)
        return state.snapshot()

    @app.get("/api/stream")
    async def stream(request: Request):
        """Server-Sent Events: 1초마다 전체 상태를 보낸다 (브라우저가 끊기면 자동 재연결)."""
        authed(request)

        async def gen():
            while not stopping() and not await request.is_disconnected():   # 종료 시작하면 스트림도 끝냄
                if request.cookies.get(_COOKIE) not in sessions:
                    break
                yield f"data: {json.dumps(state.snapshot(), ensure_ascii=False)}\n\n"
                await asyncio.sleep(_STREAM_INTERVAL_SEC)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    def guarded(request: Request) -> None:
        """상태를 바꾸는 요청: 로그인 + 사용자 정의 헤더 (다른 사이트가 몰래 보내는 요청 차단)."""
        authed(request)
        if request.headers.get("x-vibe-control") != "1":
            raise HTTPException(403, "허용되지 않은 요청입니다")

    @app.post("/api/control/{command}")
    async def control(command: str, request: Request):
        """일시정지·재개·긴급 정지."""
        guarded(request)
        if command not in ("pause", "resume", "kill"):
            raise HTTPException(404, "알 수 없는 명령입니다")
        if command == "kill":
            try:
                body = await request.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict) or body.get("confirm") != "긴급정지":
                raise HTTPException(400, "확인 문구가 맞지 않습니다")
        log.warning("[대시보드] %s 요청", {"pause": "일시정지", "resume": "재개", "kill": "긴급 정지"}[command])
        return await state.control(command)

    @app.get("/api/settings")
    async def get_settings(request: Request):
        authed(request)
        return state.settings_view()

    @app.put("/api/settings")
    async def put_settings(request: Request):
        """{"values": {"ma_cross.take_profit_pct": 4.0, ...}} → 검증 → strategy.toml 기록 → 실행 중인 매매에 적용."""
        guarded(request)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "요청 형식 오류") from None
        values = body.get("values") if isinstance(body, dict) else None
        if not isinstance(values, dict):
            raise HTTPException(400, "요청 형식 오류")
        result = state.update_settings(values)
        if not result["ok"]:
            raise HTTPException(400, result["message"])
        return result

    def annual(year: int) -> dict:
        cfg = state.config
        fees, tax = (cfg.fees, cfg.tax) if cfg else (FeeParams(), TaxParams())
        snap = state.account.snapshot if state.account else None
        return compute_annual(store, year, fees, tax, current_rate=snap.exchange_rate if snap else None)

    @app.get("/api/annual")
    async def get_annual(request: Request, year: int | None = None):
        """연간 정산: 수수료·원화 환산·예상 세금 (설정 탭의 수수료·세금 값으로 계산)."""
        authed(request)
        years = available_years(store)
        y = year or years[0]
        if not 2000 <= y <= 2100:
            raise HTTPException(400, "연도 범위 오류")
        return {"years": years, **annual(y)}

    @app.get("/api/annual/{year}/sells.csv")
    async def get_annual_csv(year: int, request: Request):
        authed(request)
        if not 2000 <= year <= 2100:
            raise HTTPException(400, "연도 범위 오류")
        return Response(sells_csv(annual(year)).encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="vibe-stock-{store.env}-{year}-sells.csv"'})

    # ── 백테스트 ───────────────────────────────────────────
    @app.get("/api/backtest/data")
    async def backtest_data(request: Request):
        authed(request)
        return {"coverage": await asyncio.to_thread(data_coverage, minutes), "status": bt}

    @app.get("/api/backtest/status")
    async def backtest_status(request: Request):
        authed(request)
        return bt

    @app.post("/api/backtest")
    async def backtest_run(request: Request):
        """현재 설정 + 바꿔 볼 값(파일에는 쓰지 않음)으로 백테스트. 계산은 별도 스레드 (매매 루프를 막지 않게), 한 번에 하나."""
        guarded(request)
        if state.config is None:
            raise HTTPException(400, "설정을 읽을 수 없어 백테스트를 할 수 없습니다")
        if bt["running"]:
            raise HTTPException(409, "이미 백테스트가 실행 중입니다")
        try:
            body = await request.json()
            start, end = date.fromisoformat(body["start"]), date.fromisoformat(body["end"])
            symbols = body.get("symbols") or None
            overrides = body.get("overrides") or {}
            if not isinstance(overrides, dict) or (symbols is not None and not isinstance(symbols, list)):
                raise ValueError
            cash, slip = float(body.get("start_cash", 100_000)), float(body.get("fill_slippage_pct", 0.05))
            preset_id = body.get("preset")
        except (ValueError, KeyError, TypeError):
            raise HTTPException(400, "요청 형식 오류") from None
        preset = None
        if preset_id:   # 거래 옵션으로 실행: 그 옵션의 전략 값으로 덮어쓴다
            if not state.presets or preset_id not in state.presets.presets:
                raise HTTPException(400, "없는 거래 옵션입니다")
            p = state.presets.presets[preset_id]
            overrides = {**overrides, **{f"ma_cross.{k}": v for k, v in state.presets._values(preset_id).items()}}
            preset = {"id": preset_id, "name": p["name"]}
        bt.update(running=True, progress=0.0, started_at=datetime.now().isoformat(timespec="seconds"), error=None)
        log.info("[대시보드] 백테스트 요청: %s ~ %s, 바꾼 값 %d개", start, end, len(overrides))
        try:
            result = await asyncio.to_thread(
                run_backtest, state.config, minutes, runs, start=start, end=end, symbols=symbols, overrides=overrides,
                start_cash=cash, fill_slippage_pct=slip, progress=lambda x: bt.__setitem__("progress", round(x, 3)),
                preset=preset)
        except ValueError as e:
            bt["error"] = str(e)
            raise HTTPException(400, str(e)) from None
        except Exception as e:
            log.exception("백테스트 오류")
            bt["error"] = f"백테스트 중 오류: {e!r}"
            raise HTTPException(500, bt["error"]) from None
        finally:
            bt["running"] = False
        bt["last_id"] = result["id"]
        return result

    @app.get("/api/backtest/runs")
    async def backtest_runs(request: Request):
        authed(request)
        return {"runs": await asyncio.to_thread(runs.list)}

    @app.get("/api/backtest/runs/{run_id}")
    async def backtest_one(run_id: str, request: Request):
        authed(request)
        r = await asyncio.to_thread(runs.load, run_id)
        if r is None:
            raise HTTPException(404, "기록이 없습니다")
        return r

    # ── 거래 옵션 ───────────────────────────────────────────
    def presets_or_400():
        if state.presets is None:
            raise HTTPException(400, "거래 옵션을 쓸 수 없습니다")
        return state.presets

    def presets_view() -> dict:
        ps = presets_or_400()
        perf = preset_performance(store)
        last_bt: dict[str, dict] = {}
        for r in runs.list(100):
            pid = (r.get("preset") or {}).get("id")
            if pid and pid not in last_bt:
                last_bt[pid] = r
        items = []
        for p in ps.list():
            items.append({**p, "summary": ps.summary(p["id"]), "live": perf.get(p["name"]), "backtest": last_bt.get(p["id"])})
        fields = [f for g in (state.config.describe()["groups"] if state.config else []) for f in g["fields"]
                  if f["section"] == "ma_cross"]
        snap = state.account.snapshot if state.account else None
        holdings = []
        for sym, h in sorted((snap.holdings if snap else {}).items()):
            rule = state.book.get(sym) if state.book else None
            holdings.append({"symbol": sym, "qty": h.quantity, "preset": rule.preset_name if rule else None,
                             "rule": rule.summary() if rule else None})
        return {"active": ps.active_id, "presets": items, "fields": fields, "holdings": holdings,
                "trading": state.phase == "trading", "env": state.env}

    async def preset_body(request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise HTTPException(400, "요청 형식 오류")
        return body

    @app.get("/api/presets")
    async def get_presets(request: Request):
        authed(request)
        return await asyncio.to_thread(presets_view)

    @app.post("/api/presets")
    async def create_preset(request: Request):
        guarded(request)
        body = await preset_body(request)
        try:
            pid = presets_or_400().create(body.get("name", ""), body.get("description", ""), body.get("values"),
                                          body.get("copy_from"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True, "id": pid, "message": f"'{state.presets.presets[pid]['name']}' 을(를) 만들었습니다", **presets_view()}

    @app.put("/api/presets/{pid}")
    async def update_preset(pid: str, request: Request):
        guarded(request)
        body = await preset_body(request)
        try:
            changes = presets_or_400().update(pid, body.get("name"), body.get("description"), body.get("values"))
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        active = pid == state.presets.active_id
        msg = "저장했습니다" + (f". 켜져 있는 옵션이라 바뀐 값 {len(changes)}개를 매매에 적용했습니다" if active and changes else "")
        return {"ok": True, "message": msg, **presets_view()}

    @app.delete("/api/presets/{pid}")
    async def delete_preset(pid: str, request: Request):
        guarded(request)
        try:
            name = presets_or_400().presets.get(pid, {}).get("name", pid)
            state.presets.delete(pid)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True, "message": f"'{name}' 을(를) 지웠습니다", **presets_view()}

    @app.post("/api/presets/{pid}/activate")
    async def activate_preset(pid: str, request: Request):
        guarded(request)
        try:
            changes = presets_or_400().activate(pid)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        later = [c for c in changes if c.field.apply != "now"]
        msg = f"'{state.presets.active_name}' 을(를) 켰습니다. 새 매수부터 적용되고, 보유 종목은 산 때 옵션 기준으로 매도합니다"
        if later and state.phase == "trading":
            msg += f". {', '.join(c.field.label for c in later)}은(는) 다음 거래일부터"
        return {"ok": True, "message": msg, **presets_view()}

    # ── 스캘핑 ───────────────────────────────────────────────
    @app.get("/api/scalp")
    async def get_scalp(request: Request):
        authed(request)
        items = (await asyncio.to_thread(presets_view))["presets"] if state.presets else []
        return scalp_view(state, items)

    @app.post("/api/scalp/templates/{key}")
    async def scalp_from_template(key: str, request: Request):
        """템플릿으로 거래 옵션 만들기 (켜지는 않는다 → 백테스트 후 거래 옵션 탭이나 여기서 켜기)."""
        guarded(request)
        try:
            ps = presets_or_400()
            name, desc, values = template_values(key, ps.presets[ps.active_id].get("off") or [])
            names = {p["name"] for p in ps.presets.values()}
            final, n = name, 2
            while final in names:
                final, n = f"{name} {n}", n + 1
            pid = ps.create(final, desc, values)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return {"ok": True, "id": pid, "message": f"'{final}' 거래 옵션을 만들었습니다. 백테스트로 먼저 확인해 보세요"}

    # ── 프리마켓 시뮬레이션 (모의투자 가상 매매) ─────────────────
    sim_store = SimStore(data_dir / "sim", store.env)

    @app.get("/api/sim")
    async def sim_days(request: Request):
        authed(request)
        days = await asyncio.to_thread(sim_store.days)
        live = state.sim
        if live is not None and live.started:   # 진행 중인 날은 메모리의 최신 값으로
            days = [d for d in days if d["date"] != live.date.isoformat()] + [summary(live.data)]
        sp = state.config.params("premarket_sim") if state.config else None
        return {"env": store.env, "days": sorted(days, key=lambda d: d["date"]),
                "enabled": sp.enabled if sp else None, "slippage_pct": sp.slippage_pct if sp else None,
                "active": live is not None and live.started and not live.closed}

    @app.get("/api/sim/{day}")
    async def sim_day(day: str, request: Request):
        authed(request)
        try:
            d = date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "날짜 형식 오류 (예: 2026-11-25)") from None
        live = state.sim
        if live is not None and live.date == d and live.started:
            return live.view()
        data = await asyncio.to_thread(sim_store.load, d)
        if data is None:
            raise HTTPException(404, f"{day} 프리마켓 시뮬레이션 기록이 없습니다")
        return PremarketSim.view_of(data)

    @app.get("/api/reports")
    async def reports(request: Request):
        authed(request)
        return {"env": store.env, "days": store.load_summary()}

    @app.get("/api/reports/{day}")
    async def report(day: str, request: Request):
        authed(request)
        try:
            d = date.fromisoformat(day)
        except ValueError:
            raise HTTPException(400, "날짜 형식 오류 (예: 2026-11-10)") from None
        data = store.load_report_json(d)
        if data is None:
            raise HTTPException(404, f"{day} 정산 보고서가 없습니다")
        return data

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    # ── 화면 파일 (web/dist) ───────────────────────────────
    @app.get("/{path:path}", include_in_schema=False)
    async def spa(path: str):
        target = (DIST_DIR / path).resolve()
        if path and target.is_file() and DIST_DIR.resolve() in target.parents:
            return FileResponse(target)
        index = DIST_DIR / "index.html"
        if index.exists():
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return HTMLResponse("<h3>화면 파일(web/dist)이 없습니다.</h3>"
                            "<p>web/frontend 에서 <code>npm install</code> → <code>npm run build</code> 를 실행하세요.</p>",
                            status_code=503)

    return app


class _QuietServer(uvicorn.Server):
    """매매 프로그램의 Ctrl+C 처리를 가로채지 않도록 uvicorn 의 시그널 처리를 끈다."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self) -> None:   # 구버전 uvicorn 호환
        pass


def resolve_password(configured: str | None) -> str:
    if configured:
        return configured
    temp = secrets.token_urlsafe(9)
    # 로그 파일·슬랙에 남지 않도록 콘솔 창에만 출력
    print(f"\n[대시보드] .env 에 DASHBOARD_PASSWORD 가 없어 임시 비밀번호를 만들었습니다: {temp}\n"
          f"           (프로그램을 다시 시작하면 바뀝니다. 고정하려면 .env 에 DASHBOARD_PASSWORD=... 추가)\n",
          file=sys.stderr, flush=True)
    return temp


class DashboardServer:
    """대시보드 실행·종료. 종료는 uvicorn 정상 종료 절차로 (강제 취소하면 Windows 에서 소켓 오류 로그가 남는다)."""

    def __init__(self, state: DashboardState, store: SettlementStore, params: DashboardParams, password: str | None):
        self._state, self._store, self._params, self._password = state, store, params, password
        self._server: _QuietServer | None = None
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="dashboard")

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._server is not None:
            self._server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=5)
        except (TimeoutError, Exception):
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        """실패해도 예외를 내보내지 않는다 (매매는 계속)."""
        port = self._params.port
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if sys.platform != "win32":
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("127.0.0.1", port))
        except OSError as e:
            sock.close()
            log.warning("대시보드를 켜지 못했습니다 (포트 %d 사용 중?): %s → 매매는 계속합니다", port, e)
            return
        app = create_app(self._state, self._store, resolve_password(self._password),
                         stopping=lambda: bool(self._server and self._server.should_exit))
        # log_config=None: uvicorn 이 로깅 설정을 바꾸지 않고 프로그램 로그(파일·슬랙)로 남긴다
        self._server = _QuietServer(uvicorn.Config(app, log_level="warning", log_config=None, access_log=False,
                                                   lifespan="off", timeout_graceful_shutdown=3))
        log.info("대시보드: http://127.0.0.1:%d (이 PC 에서만 접속)", port)
        if not (DIST_DIR / "index.html").exists():
            log.warning("대시보드 화면 파일(web/dist)이 없습니다 → web/frontend 에서 npm run build 필요")
        try:
            await self._server.serve(sockets=[sock])
        except asyncio.CancelledError:
            raise
        except BaseException as e:   # uvicorn 은 시작 실패 시 SystemExit 를 던진다
            if isinstance(e, KeyboardInterrupt):
                raise
            log.error("대시보드 서버 오류 → 대시보드만 중지 (매매는 계속): %r", e)
        finally:
            sock.close()

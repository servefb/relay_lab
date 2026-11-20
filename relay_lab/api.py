"""ASGI surface of the lab: typed endpoints, one static console.

The contracts in :mod:`relay_lab.config` double as request/response models, so
FastAPI validates bodies before the controller ever sees them.
"""

from __future__ import annotations

import asyncio
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import contract, store
from .ai_api import ai_router
from .auth import (
    LOGIN_GATE,
    PUBLIC_API_PATHS,
    LoginRateLimit,
    LoginRequest,
    clear_session_cookie,
    client_address,
    configured_username,
    issue_token,
    request_is_https,
    set_session_cookie,
    user_from_cookies,
    user_from_request,
    verify_credentials,
)
from .config import (
    CONTRACT_CHECKS,
    DATASET_PRESETS,
    FAST_CONTRACT_CHECKS,
    ContractRequest,
    DemoRequest,
    LabRun,
    LoadRequest,
    ModelsRequest,
    ModelsResponse,
    ParallelRequest,
    QualityRequest,
    RunsResponse,
    Schedule,
    ScheduleKey,
    ScheduleRequest,
    SchedulesResponse,
    ScheduleToggle,
    SpeedRequest,
    StartRequest,
)
from .demo import DemoServer
from .discovery import DiscoveryError, fetch_models
from .scheduler import Scheduler
from .search import CapacityLab

PROJECT_ROOT = Path(__file__).resolve().parents[1]
"""Deployment root: the folder holding this package, wherever it was copied to."""

WEB_CANDIDATES = ('web/dist', 'web')
"""Built assets first, then a plain static directory for hand-written pages."""


def resolve_web(web_dir: str | Path | None = None) -> Path | None:
    """Find the console: explicit path, env override, then a local web/ folder."""
    if web_dir:
        candidate = Path(web_dir).resolve()
        return candidate if (candidate / 'index.html').is_file() else None
    override = os.environ.get('RELAY_LAB_WEB')
    if override:
        candidate = Path(override).resolve()
        return candidate if (candidate / 'index.html').is_file() else None
    for suffix in WEB_CANDIDATES:
        candidate = PROJECT_ROOT / suffix
        if (candidate / 'index.html').is_file():
            return candidate
    return None


def _message(error: RequestValidationError) -> str:
    """Render validator output as one readable Chinese line."""
    parts = []
    for item in error.errors():
        location = '.'.join(str(part) for part in item['loc'] if part != 'body')
        parts.append(f'{location}: {item["msg"]}' if location else item['msg'])
    return '; '.join(parts) or '请求内容不合法'


class RunHub:
    """Fan run mutations out to connected consoles, replacing HTTP polling.

    Writes happen on worker threads, so publishing hops back to the event loop that
    owns the sockets. A slow client loses intermediate updates (its queue is bounded)
    but always receives a fresh one on its next event, which is the right trade for a
    progress view.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any]]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self) -> None:
        self._loop = asyncio.get_running_loop()

    def publish(self, run: LabRun) -> None:
        loop = self._loop
        if loop is None or not self._queues:
            return
        payload = {'type': 'run', 'run': run.model_dump(mode='json')}
        for queue in list(self._queues):
            try:
                loop.call_soon_threadsafe(queue.put_nowait, payload)
            except (RuntimeError, asyncio.QueueFull):
                # Loop closed during shutdown, or this client is too far behind to care.
                continue

    def register(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._queues.add(queue)
        return queue

    def unregister(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._queues.discard(queue)


def create_app(
    data_dir: str | Path,
    *,
    web_dir: str | Path | None = None,
    schedule_loop: bool = True,
) -> FastAPI:
    """Build the app around one lab instance rooted at *data_dir*."""
    root = Path(data_dir)
    lab = CapacityLab(root)
    scheduler = Scheduler(root, lab)
    if schedule_loop:
        scheduler.start()

    console = resolve_web(web_dir)
    hub = RunHub()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        """Bind the push hub to this event loop and stop listening on shutdown."""
        hub.bind_loop()
        store.subscribe(hub.publish)
        try:
            yield
        finally:
            store.unsubscribe(hub.publish)

    app = FastAPI(
        title='Relay Lab',
        version='0.2.0',
        docs_url='/api/docs',
        openapi_url='/api/openapi.json',
        lifespan=lifespan,
    )
    app.state.lab = lab
    app.state.scheduler = scheduler

    @app.middleware('http')
    async def same_origin(request: Request, call_next):
        """Do not let another website trigger authenticated benchmark traffic."""
        if request.url.path.startswith('/api/'):
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                return JSONResponse({'error': '只允许从本机同源实验页面访问'}, status_code=403)
        return await call_next(request)

    @app.middleware('http')
    async def require_session(request: Request, call_next):
        """JWT cookie gate for every /api route except login/health/me/logout."""
        path = request.url.path
        if path.startswith('/api/') and path not in PUBLIC_API_PATHS:
            if user_from_request(request) is None:
                return JSONResponse({'error': '未登录或会话已过期'}, status_code=401)
        return await call_next(request)

    @app.exception_handler(RequestValidationError)
    async def invalid_body(request: Request, error: RequestValidationError) -> JSONResponse:
        return JSONResponse({'error': _message(error)}, status_code=400)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        return JSONResponse({'error': str(error.detail)}, status_code=error.status_code)

    def busy(error: RuntimeError) -> HTTPException:
        return HTTPException(status_code=409, detail=str(error))

    @app.get('/api/health')
    def health() -> dict[str, Any]:
        return {'ok': True, 'schedules': len(scheduler.list())}

    @app.post('/api/auth/login')
    def login(body: LoginRequest, request: Request) -> JSONResponse:
        """Issue an HttpOnly JWT cookie; the JSON body never contains the token."""
        client = client_address(request)
        try:
            LOGIN_GATE.check(client)
        except LoginRateLimit as error:
            raise HTTPException(status_code=429, detail=str(error)) from error
        if not verify_credentials(body.username, body.password):
            LOGIN_GATE.fail(client)
            raise HTTPException(status_code=401, detail='用户名或密码错误')
        LOGIN_GATE.succeed(client)
        username = configured_username()
        response = JSONResponse({'ok': True, 'user': username})
        set_session_cookie(
            response,
            issue_token(username),
            secure=request_is_https(request),
        )
        return response

    @app.post('/api/auth/logout')
    def logout(request: Request) -> JSONResponse:
        response = JSONResponse({'ok': True})
        clear_session_cookie(response, secure=request_is_https(request))
        return response

    @app.get('/api/auth/me')
    def me(request: Request) -> dict[str, Any]:
        username = user_from_request(request)
        if username is None:
            raise HTTPException(status_code=401, detail='未登录或会话已过期')
        return {'user': username}

    @app.websocket('/api/ws')
    async def live(socket: WebSocket) -> None:
        """Push run changes as they happen: one snapshot, then one message per write.

        The console used to poll /api/runs every two seconds; every quality item and
        every stage write now reaches it immediately instead of on the next tick.
        """
        # Accept first so the browser receives the close code. Rejecting before
        # accept collapses to 1006 and the console reconnects forever on a dead JWT.
        await socket.accept()
        if user_from_cookies(dict(socket.cookies)) is None:
            await socket.close(code=4401)
            return
        queue = hub.register()
        try:
            await socket.send_json(
                {'type': 'snapshot', 'runs': [run.model_dump(mode='json') for run in lab.history()]}
            )
            while True:
                await socket.send_json(await queue.get())
        except (WebSocketDisconnect, RuntimeError):
            # Client closed the tab mid-write; nothing else to do.
            pass
        finally:
            hub.unregister(queue)

    @app.get('/api/presets')
    def presets() -> dict[str, Any]:
        """Named workloads the console offers, with the trade-off each one encodes.

        Served rather than hard-coded in the UI so the two can never disagree about what
        a preset means or which token length it uses.
        """
        from .model_profiles import GENERIC, GLM, KIMI_K3, resolve_profile

        def _profile_payload(profile: Any) -> dict[str, Any]:
            return {
                'family': profile.family,
                'label': profile.label,
                'default_checks': list(profile.default_checks),
                'supports_dynamic_tools': profile.supports_dynamic_tools,
                'supports_preserved_thinking': profile.supports_preserved_thinking,
                'default_temperature': profile.default_temperature,
                'allows_thinking_disable': profile.allows_thinking_disable,
            }

        return {
            'datasets': [
                {'key': key, **value} for key, value in DATASET_PRESETS.items()
            ],
            'contract_modes': {
                'fast': {
                    'label': '快速检查',
                    'description': '仅运行核心高信号协议项与主协议，跳过 15s 缓存等待',
                    'checks': list(FAST_CONTRACT_CHECKS),
                },
                'full': {
                    'label': '全量检查',
                    'description': '覆盖全部 KVV 专项、多协议矩阵与缓存 TTL 闭环',
                    'checks': list(CONTRACT_CHECKS),
                },
            },
            # Keep the 'kimi' key for frontend compatibility; expose K3 flagship defaults.
            'contract_profiles': {
                'kimi': _profile_payload(KIMI_K3),
                'glm': _profile_payload(GLM),
                'generic': _profile_payload(GENERIC),
            },
            # Tiny resolver hint so the console can preview without re-encoding rules.
            'contract_profile_examples': {
                'kimi-k3.1-pro': resolve_profile('kimi-k3.1-pro').family,
                'kimi-k2.5': resolve_profile('kimi-k2.5').family,
                'glm-5.3': resolve_profile('glm-5.3').family,
                'gpt-test': resolve_profile('gpt-test').family,
            },
        }

    @app.get('/api/runs', response_model=RunsResponse)
    def list_runs() -> RunsResponse:
        return RunsResponse(runs=lab.history())

    @app.post('/api/runs', response_model=LabRun, status_code=202)
    def start_run(config: StartRequest) -> LabRun:
        try:
            return lab.start(config)
        except RuntimeError as error:
            raise busy(error) from error

    @app.post('/api/speed', response_model=LabRun, status_code=202)
    def start_speed(config: SpeedRequest) -> LabRun:
        try:
            return lab.start_speed(config)
        except RuntimeError as error:
            raise busy(error) from error

    @app.post('/api/parallel', response_model=LabRun, status_code=202)
    def start_parallel(config: ParallelRequest) -> LabRun:
        try:
            return lab.start_parallel(config)
        except RuntimeError as error:
            raise busy(error) from error

    @app.post('/api/load', response_model=LabRun, status_code=202)
    def start_load(config: LoadRequest) -> LabRun:
        try:
            return lab.start_load(config)
        except RuntimeError as error:
            raise busy(error) from error

    @app.post('/api/quality', response_model=LabRun, status_code=202)
    def start_quality(config: QualityRequest) -> LabRun:
        try:
            return lab.start_quality(config)
        except RuntimeError as error:
            raise busy(error) from error

    @app.post('/api/contract', response_model=LabRun, status_code=202)
    def start_contract(config: ContractRequest) -> LabRun:
        """Probe what the gateway forwards: tools, stop, limits, stream framing."""
        try:
            return lab.start_contract(config)
        except RuntimeError as error:
            raise busy(error) from error

    @app.get('/api/runs/{run_id}', response_model=LabRun)
    def get_run(run_id: str) -> LabRun:
        found = lab.get_run(run_id)
        if found is None:
            raise HTTPException(status_code=404, detail='实验不存在')
        return found

    @app.get('/api/runs/{run_id}/report')
    def export_run_report(run_id: str) -> Any:
        """导出实验报告：协议检查返回结构化 Markdown 报告，其他实验返回 JSON。"""
        found = lab.get_run(run_id)
        if found is None:
            raise HTTPException(status_code=404, detail='实验不存在')
        if found.kind == 'contract':
            return PlainTextResponse(
                contract.render_report(found),
                media_type='text/markdown; charset=utf-8',
                headers={'Content-Disposition': f'attachment; filename="{found.run_id}.md"'},
            )
        return JSONResponse(
            found.model_dump(mode='json'),
            headers={'Content-Disposition': f'attachment; filename="{found.run_id}.json"'},
        )

    @app.post('/api/runs/{run_id}/stop', response_model=LabRun)
    def stop_run(run_id: str) -> LabRun:
        try:
            return lab.stop(run_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail='没有正在运行的对应实验') from error

    @app.post('/api/models', response_model=ModelsResponse)
    def list_models(connection: ModelsRequest) -> ModelsResponse:
        try:
            return fetch_models(connection)
        except DiscoveryError as error:
            raise HTTPException(status_code=error.status, detail=str(error)) from error

    @app.post('/api/demo', response_model=LabRun, status_code=202)
    def start_demo(body: DemoRequest | None = None) -> LabRun:
        """Exercise the real engine against an owned loopback fixture."""
        protocol = (body or DemoRequest()).protocol
        server = DemoServer()
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            run = lab.start(
                StartRequest(
                    url=f'http://127.0.0.1:{server.server_address[1]}/v1',
                    model='local-simulator',
                    label=f'本地模拟验证 · {protocol} · 非真实站点',
                    protocol=protocol,
                    authorized=True,
                    goal='both',
                    stage_seconds=10,
                    max_steps=2,
                    refine_steps=0,
                    start_rpm=60,
                    max_rpm=120,
                    max_parallel=8,
                    max_requests=80,
                    token_reserve=300000,
                    max_wall_seconds=300,
                    cooldown_seconds=0,
                    request_timeout=10,
                )
            )
        except RuntimeError as error:
            server.shutdown()
            server.server_close()
            raise busy(error) from error

        def cleanup() -> None:
            lab.jobs[run.run_id].done.wait()
            server.shutdown()
            server.server_close()

        threading.Thread(target=cleanup, daemon=True).start()
        return run

    # ---------- schedules ----------

    @app.get('/api/schedules', response_model=SchedulesResponse)
    def list_schedules() -> SchedulesResponse:
        return SchedulesResponse(schedules=scheduler.list())

    @app.post('/api/schedules', response_model=Schedule, status_code=201)
    def create_schedule(config: ScheduleRequest) -> Schedule:
        return scheduler.create(config)

    @app.delete('/api/schedules/{schedule_id}', status_code=204)
    def delete_schedule(schedule_id: str) -> None:
        try:
            scheduler.remove(schedule_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail='定时任务不存在') from error

    @app.post('/api/schedules/{schedule_id}/toggle', response_model=Schedule)
    def toggle_schedule(schedule_id: str, body: ScheduleToggle) -> Schedule:
        try:
            return scheduler.toggle(schedule_id, body.enabled)
        except KeyError as error:
            raise HTTPException(status_code=404, detail='定时任务不存在') from error

    @app.post('/api/schedules/{schedule_id}/key', response_model=Schedule)
    def schedule_key(schedule_id: str, body: ScheduleKey) -> Schedule:
        value = body.api_key.get_secret_value().strip() if body.api_key else ''
        if not value:
            raise HTTPException(status_code=400, detail='请输入 API Key')
        try:
            return scheduler.set_key(schedule_id, value)
        except KeyError as error:
            raise HTTPException(status_code=404, detail='定时任务不存在') from error

    @app.post('/api/schedules/{schedule_id}/run', response_model=Schedule)
    def run_schedule_now(schedule_id: str, body: ScheduleKey | None = None) -> Schedule:
        value = body.api_key.get_secret_value().strip() if body and body.api_key else None
        try:
            return scheduler.run_now(schedule_id, value)
        except KeyError as error:
            raise HTTPException(status_code=404, detail='定时任务不存在') from error

    app.include_router(ai_router(lab, presets))

    if console is not None:
        app.mount('/', StaticFiles(directory=console, html=True), name='console')
    else:

        @app.get('/')
        def missing_console() -> JSONResponse:
            """The API works on its own; the console is a separate deliverable."""
            return JSONResponse(
                {
                    'service': 'relay_lab backend',
                    'console': 'not built',
                    'hint': '在 web/ 执行 npm install && npm run build，或用 --web 指定静态目录；接口见 /api/docs',
                },
                status_code=503,
            )

    return app

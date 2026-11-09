"""ASGI surface of the lab: typed endpoints, one static console.

The contracts in :mod:`relay_lab.config` double as request/response models, so
FastAPI validates bodies before the controller ever sees them.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import (
    ContractRequest,
    DemoRequest,
    LabRun,
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

    app = FastAPI(title='Relay Lab', version='0.2.0', docs_url='/api/docs', openapi_url='/api/openapi.json')
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
        found = next((item for item in lab.history() if item.run_id == run_id), None)
        if found is None:
            raise HTTPException(status_code=404, detail='实验不存在')
        return found

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
                    'hint': '在 relay_lab/web 执行 npm run build，或用 --web 指定静态目录；接口见 /api/docs',
                },
                status_code=503,
            )

    return app

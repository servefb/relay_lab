"""Authenticated AI routes; failures before and after opening SSE are distinct."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import aclosing
from datetime import datetime, timezone
from typing import Any

import anyio
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import Field
from starlette.types import Receive, Scope, Send

from .ai_assistant import AIChatRequest, Emit, chat
from .ai_client import AIError, redact_key, stream_text, test_connection
from .ai_config import AIConfig, AIConfigResponse, AIConfigState, AIConfigUpdate
from .ai_diagnose import DIAGNOSIS_SYSTEM, summarize_run_for_ai
from .config import AIAnalysis, Contract
from .search import CapacityLab

LOGGER = logging.getLogger(__name__)
MAX_CONCURRENT = 4


class AIAnalyzeRequest(Contract):
    force: bool = Field(default=False, strict=True)
    focus: str = Field(default='', max_length=1000)


class AIStream(StreamingResponse):
    media_type = 'text/event-stream'

    def __init__(self, producer: Callable[[Emit], Awaitable[None]]) -> None:
        super().__init__((), media_type='text/event-stream', headers={
            'Cache-Control': 'no-cache, no-store', 'X-Accel-Buffering': 'no',
        })
        self.producer = producer

    async def stream_response(self, send: Send) -> None:
        await send({'type': 'http.response.start', 'status': self.status_code, 'headers': self.raw_headers})

        async def emit(event: str, data: dict[str, Any]) -> None:
            payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
            await send({'type': 'http.response.body', 'body': f'event: {event}\ndata: {payload}\n\n'.encode(),
                        'more_body': True})

        try:
            await self.producer(emit)
        except AIError as error:
            await emit('error', {'code': error.code, 'message': str(error)})
        except OSError:
            raise
        except Exception as error:
            LOGGER.warning('AI stream failed (%s)', type(error).__name__)
            await emit('error', {'code': 'internal_error', 'message': 'AI 请求未完成'})
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Listen even when ASGI 2.4+ would ordinarily wait until the next write fails.
        async with anyio.create_task_group() as tasks:
            async def produce() -> None:
                try:
                    await self.stream_response(send)
                except OSError:
                    pass  # A closed peer can race the disconnect signal.
                finally:
                    tasks.cancel_scope.cancel()

            tasks.start_soon(produce)
            await self.listen_for_disconnect(receive)
            tasks.cancel_scope.cancel()


def ai_router(lab: CapacityLab, presets: Callable[[], dict[str, Any]]) -> APIRouter:
    router = APIRouter(prefix='/api', tags=['AI'])
    state = AIConfigState()
    running = 0
    active_reports: set[str] = set()

    def acquire() -> None:
        nonlocal running
        if running >= MAX_CONCURRENT:
            raise AIError('ai_busy', 'AI 并发请求已满，请稍后重试', 409)
        running += 1

    def release() -> None:
        nonlocal running
        running -= 1

    def configured() -> AIConfig:
        config = state.snapshot()
        if not config.configured:
            raise HTTPException(409, '请先配置 AI 模型与 Key')
        return config

    @router.get('/ai/config', response_model=AIConfigResponse)
    async def get_config() -> AIConfigResponse:
        return state.public()

    @router.put('/ai/config', response_model=AIConfigResponse)
    async def update_config(patch: AIConfigUpdate) -> AIConfigResponse:
        try:
            return state.update(patch)
        except ValueError as error:
            # Prefer the fixed Chinese messages from ai_config; never echo raw parser text.
            message = str(error)
            if not message.startswith('AI '):
                message = 'AI 配置格式不合法'
            raise HTTPException(400, message) from error

    @router.post('/ai/test')
    async def probe() -> Any:
        config = configured()
        try:
            acquire()
            try:
                await test_connection(config)
            finally:
                release()
        except AIError as error:
            return JSONResponse({'error': str(error), 'code': error.code}, status_code=error.status)
        return {'ok': True, 'model': config.model, 'protocol': config.protocol}

    @router.post('/runs/{run_id}/analyze', response_class=AIStream, status_code=200)
    async def analyze(run_id: str, body: AIAnalyzeRequest | None = None) -> AIStream:
        options = body or AIAnalyzeRequest()
        focus = options.focus.strip()
        run = await anyio.to_thread.run_sync(lab.get_run, run_id)
        if run is None:
            raise HTTPException(404, '实验不存在')
        if run.status in ('running', 'stopping'):
            raise HTTPException(409, '实验尚未结束')

        async def cached(emit: Emit) -> None:
            assert run.ai_analysis is not None
            await emit('done', {'cached': True, 'persisted': True, 'analysis': run.ai_analysis.model_dump()})

        if run.ai_analysis and not options.force and not focus:
            return AIStream(cached)
        if run_id in active_reports:
            raise HTTPException(409, '这份报告正在生成 AI 解读')
        config = configured()

        async def generate(emit: Emit) -> None:
            if run_id in active_reports:
                raise AIError('analysis_busy', '这份报告正在生成 AI 解读', 409)
            acquire()
            active_reports.add(run_id)
            try:
                assert config.api_key is not None
                key = config.api_key.get_secret_value()
                prompt = json.dumps(redact_key({'report': summarize_run_for_ai(run), 'focus': focus}, key),
                                    ensure_ascii=False, allow_nan=False)
                parts = []
                async with aclosing(stream_text(config, prompt, DIAGNOSIS_SYSTEM)) as stream:
                    async for delta in stream:
                        parts.append(delta)
                        await emit('delta', {'text': delta})
                analysis = AIAnalysis(summary_md=''.join(parts), model=config.model, protocol=config.protocol,
                                      created_at=datetime.now(timezone.utc).isoformat())
                if not focus:
                    await anyio.lowlevel.checkpoint_if_cancelled()
                    try:
                        await anyio.to_thread.run_sync(lab.save_analysis, run_id, analysis)
                    except (OSError, RuntimeError) as error:
                        raise AIError('save_failed', '解读完成，但报告写入失败') from error
                await emit('done', {'cached': False, 'persisted': not bool(focus), 'analysis': analysis.model_dump()})
            finally:
                active_reports.discard(run_id)
                release()

        return AIStream(generate)

    @router.post('/ai/chat', response_class=AIStream, status_code=200)
    async def assistant(body: AIChatRequest) -> AIStream:
        if body.messages[-1].role != 'user' or not body.messages[-1].content.strip():
            raise HTTPException(400, '最后一条消息必须是非空用户消息')
        config = configured()

        async def generate(emit: Emit) -> None:
            acquire()
            try:
                await chat(config, body, lab, presets, emit)
            finally:
                release()

        return AIStream(generate)

    return router

"""Run one probe in a child process so a run can be cancelled or time-boxed.

This replaces the upstream service helper on purpose: the lab owns its process
handling, so stopping a stage never depends on another package's task registry.
"""

from __future__ import annotations

import multiprocessing
import queue
import threading
import time
from typing import Any, Callable


class ProbeStopped(Exception):
    """The caller asked to stop; no partial statistics may be treated as a result."""


class ProbeFailed(Exception):
    """The child process died or reported an error."""


_active: dict[str, multiprocessing.Process] = {}
_stopped: set[str] = set()
"""Task ids the caller asked to stop, so a killed child reads as "stopped", not "failed"."""
_lock = threading.Lock()


def _plain(value: Any) -> Any:
    """Convert pydantic models and containers into queue-safe primitives."""
    dump = getattr(value, 'model_dump', None)
    if callable(dump):
        return dump(mode='json')
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return value


def _worker(func: Callable[..., Any], channel: Any, args: tuple) -> None:
    try:
        channel.put(('ok', _plain(func(*args))))
    except BaseException as error:  # noqa: BLE001 - the parent needs the type, not the traceback
        channel.put(('error', f'{type(error).__name__}: {error}'))


def stop(task_id: str) -> bool:
    """Terminate the child process registered for this stage, if any."""
    with _lock:
        process = _active.get(task_id)
    if process is None or not process.is_alive():
        return False
    with _lock:
        _stopped.add(task_id)
    process.terminate()
    process.join(5)
    if process.is_alive():
        process.kill()
        process.join(5)
    return True


def run(func: Callable[..., Any], *args: Any, task_id: str, timeout: float) -> Any:
    """Block until *func* finishes in a child process, or fail closed."""
    context = multiprocessing.get_context('spawn')
    channel = context.Queue()
    process = context.Process(target=_worker, args=(func, channel, args), daemon=True)
    process.start()
    with _lock:
        _active[task_id] = process
    deadline = time.monotonic() + timeout
    result: tuple[str, Any] | None = None
    try:
        while process.is_alive():
            try:
                # Draining the pipe while the child runs avoids the classic
                # queue.put() deadlock on payloads above the OS pipe buffer.
                result = channel.get(timeout=0.1)
                break
            except queue.Empty:
                if time.monotonic() >= deadline:
                    process.terminate()
                    process.join(5)
                    raise TimeoutError(f'压测进程超过 {timeout:g} 秒未结束') from None
        process.join(10)
        if result is None:
            try:
                result = channel.get(timeout=1)
            except queue.Empty:
                with _lock:
                    stopped = task_id in _stopped
                if stopped:
                    raise ProbeStopped(f'{task_id} 已按请求终止') from None
                raise ProbeFailed('压测进程没有返回结果') from None
    finally:
        with _lock:
            _active.pop(task_id, None)
            _stopped.discard(task_id)
        channel.close()
        if process.is_alive():
            process.kill()
            process.join(5)
    kind, payload = result
    if kind == 'error':
        raise ProbeFailed(payload)
    return payload

"""Persist runs as one small JSON file per experiment directory."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from pathlib import Path

from .config import LabRun

LOGGER = logging.getLogger(__name__)

_LISTENERS: list[Callable[[LabRun], None]] = []
"""Called after every successful write; the console's WebSocket push hangs off this."""


def subscribe(listener: Callable[[LabRun], None]) -> None:
    """Register a change listener; writes keep working even if the listener breaks."""
    _LISTENERS.append(listener)


def unsubscribe(listener: Callable[[LabRun], None]) -> None:
    if listener in _LISTENERS:
        _LISTENERS.remove(listener)

HISTORY_LIMIT = 50

DONE_MESSAGES = {
    'capacity': '实验完成。请结合结论标签阅读，不将下界或短测当成绝对极限。',
    'speed': '测速完成。分位数样本量等于请求数，P99 在样本少时约等于最大值。',
    'parallel': '并行测试完成。吞吐下降不等于站点上限，请结合客户端与网络条件看。',
    'quality': '质量抽查完成。默认题包为 MMLU 精选（四选一字母判分）或内置冒烟题，不是完整基准成绩。',
    'contract': '协议检查完成。“无法判定”不等于“没问题”，请按证据逐项阅读。',
    'load': '吞吐测试完成。这是并发条件下的实测吞吐，不是站点上限。',
}

STALE_PREFIXES = ('正在测试', '正在运行', '正在准备')


def save(root: Path, run: LabRun) -> None:
    """Write atomically so a crash cannot leave a half-written report."""
    directory = root / run.run_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'experiment.json'
    temporary = directory / 'experiment.json.tmp'
    temporary.write_text(run.model_dump_json(indent=2), encoding='utf-8')
    temporary.replace(path)
    # Observers are UI plumbing: a broken listener must never lose an experiment.
    for listener in list(_LISTENERS):
        try:
            listener(run)
        except Exception:  # noqa: BLE001 - see comment above
            LOGGER.warning('run change listener failed', exc_info=True)


def load(root: Path, limit: int = HISTORY_LIMIT) -> list[LabRun]:
    """Read recent reports; unfinished runs from a previous process are marked."""
    if not root.exists():
        return []
    runs: list[LabRun] = []
    for path in sorted(root.glob('relay-*/experiment.json'), reverse=True)[:limit]:
        run = load_run(root, path.parent.name)
        if run is not None:
            runs.append(run)
    return runs


def load_run(root: Path, run_id: str) -> LabRun | None:
    if not re.fullmatch(r'relay-[a-zA-Z0-9_-]{1,100}', run_id):
        return None
    try:
        path = root / run_id / 'experiment.json'
        if not path.resolve().is_relative_to(root.resolve()):
            return None
        run = LabRun.model_validate_json(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        return None
    if run.run_id != run_id:
        return None
    if run.status in ('running', 'stopping'):
        run.status = 'interrupted'
        run.message = '服务重启，实验已中断；未自动恢复流量'
    elif run.status == 'completed' and run.message.startswith(STALE_PREFIXES):
        run.message = DONE_MESSAGES.get(run.kind, '已完成')
    return run

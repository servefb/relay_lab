"""Persist runs as one small JSON file per experiment directory."""

from __future__ import annotations

from pathlib import Path

from .config import LabRun

HISTORY_LIMIT = 50

DONE_MESSAGES = {
    'capacity': '实验完成。请结合结论标签阅读，不将下界或短测当成绝对极限。',
    'speed': '测速完成。分位数样本量等于请求数，P99 在样本少时约等于最大值。',
    'parallel': '并行测试完成。吞吐下降不等于站点上限，请结合客户端与网络条件看。',
    'quality': '质量抽查完成。这是冒烟检查，不是基准成绩。',
    'contract': '协议检查完成。“无法判定”不等于“没问题”，请按证据逐项阅读。',
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


def load(root: Path, limit: int = HISTORY_LIMIT) -> list[LabRun]:
    """Read recent reports; unfinished runs from a previous process are marked."""
    if not root.exists():
        return []
    runs: list[LabRun] = []
    for path in sorted(root.glob('relay-*/experiment.json'), reverse=True)[:limit]:
        try:
            run = LabRun.model_validate_json(path.read_text(encoding='utf-8'))
        except (ValueError, OSError):
            continue
        if run.status in ('running', 'stopping'):
            run.status = 'interrupted'
            run.message = '服务重启，实验已中断；未自动恢复流量'
        elif run.status == 'completed' and run.message.startswith(STALE_PREFIXES):
            # Older builds left the last in-flight stage message on finished runs.
            run.message = DONE_MESSAGES.get(run.kind, '已完成')
        runs.append(run)
    return runs

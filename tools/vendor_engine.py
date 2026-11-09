"""Copy the EvalScope perf runtime into relay_lab.vendor, renaming imports.

We only vendor the code path this lab actually executes: single-turn
chat-completions load generation, the closed/open-loop dispatchers, the SSE
client and the metric accumulation. SLA tuning, AgentX scenarios, hub datasets,
HTML reports and the semantic layer are dropped on purpose; the shims in
vendor/support/ cover the few framework symbols the remaining code touches.

Run from this directory:  python tools/vendor_engine.py --source /path/to/evalscope
"""

from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / 'relay_lab' / 'vendor'
"""Where the copy lands: always this deployment, no matter where it sits."""


def find_source(explicit: str | None = None) -> Path:
    """Locate the upstream package: --source, env var, cwd, then nearby folders."""
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    env = os.environ.get('EVALSCOPE_SRC')
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve().parents[1]
    candidates += [
        Path.cwd() / 'evalscope',
        here.parent / 'evalscope',
        here.parent / 'evalscope-relay-lab' / 'evalscope',
    ]
    for candidate in candidates:
        if (candidate / 'perf' / 'arguments.py').is_file():
            return candidate.resolve()
    looked = '\n'.join(f'  {c}' for c in candidates)
    raise SystemExit(f'找不到 EvalScope 源码，请用 --source 指定（查找过）：\n{looked}')


PERF_FILES = [
    'perf/__init__.py',
    'perf/arguments.py',
    'perf/benchmark.py',
    'perf/multi_turn_args.py',
    'perf/core/__init__.py',
    'perf/core/http_client.py',
    'perf/core/metrics_consumer.py',
    'perf/core/pipeline.py',
    'perf/core/strategies/__init__.py',
    'perf/core/strategies/base.py',
    'perf/core/strategies/closed_loop.py',
    'perf/core/strategies/open_loop.py',
    'perf/core/strategies/multi_turn.py',
    'perf/plugin/__init__.py',
    'perf/plugin/registry.py',
    'perf/plugin/api/__init__.py',
    'perf/plugin/api/base.py',
    'perf/plugin/api/default_api.py',
    'perf/plugin/api/openai_api.py',
    'perf/plugin/datasets/__init__.py',
    'perf/plugin/datasets/base.py',
    'perf/plugin/datasets/custom.py',
    'perf/plugin/datasets/dataset_args.py',
    'perf/plugin/datasets/utils.py',
    'perf/utils/__init__.py',
    'perf/utils/benchmark_util.py',
    'perf/utils/body_meta.py',
    'perf/utils/db_util.py',
    'perf/utils/handler.py',
    'perf/utils/log_utils.py',
    'perf/utils/perf_constants.py',
    'perf/utils/perf_models.py',
    'perf/utils/trace_metrics.py',
    'perf/utils/worker_util.py',
    'perf/utils/workload_timeline.py',
]

SUPPORT_FILES = {
    'constants.py': 'support/constants.py',
    'utils/logger.py': 'support/logger.py',
    'utils/argument_utils.py': 'support/argument_utils.py',
    'utils/tqdm_utils/__init__.py': 'support/tqdm_utils.py',
    'utils/tqdm_utils/progress_tracker.py': 'support/progress_tracker.py',
    'utils/tqdm_utils/tqdm_logging.py': 'support/tqdm_logging.py',
    'utils/model_utils.py': 'support/model_utils.py',
    'utils/asyncio_runtime.py': 'support/asyncio_runtime.py',
    'utils/import_utils.py': 'support/import_utils.py',
    'utils/uri_utils.py': 'support/uri_utils.py',
    'utils/io_utils.py': 'support/io_utils.py',
}

RENAMES = [
    ('from evalscope.perf', 'from relay_lab.vendor.perf'),
    ('import evalscope.perf', 'import relay_lab.vendor.perf'),
    ('from evalscope.utils.logger', 'from relay_lab.vendor.support.logger'),
    ('from evalscope.utils.tqdm_utils', 'from relay_lab.vendor.support.tqdm_utils'),
    ('from evalscope.utils.model_utils', 'from relay_lab.vendor.support.model_utils'),
    ('from evalscope.utils.asyncio_runtime', 'from relay_lab.vendor.support.asyncio_runtime'),
    ('from evalscope.utils.import_utils', 'from relay_lab.vendor.support.import_utils'),
    ('from evalscope.utils.uri_utils', 'from relay_lab.vendor.support.uri_utils'),
    ('from evalscope.utils.io_utils', 'from relay_lab.vendor.support.io_utils'),
    ('from evalscope.utils.argument_utils', 'from relay_lab.vendor.support.argument_utils'),
    ('from evalscope.utils import', 'from relay_lab.vendor.support import'),
    ('from evalscope.constants', 'from relay_lab.vendor.support.constants'),
    ('from evalscope.api.dataset.hub', 'from relay_lab.vendor.support.dataset'),
    ('from evalscope.api.dataset import', 'from relay_lab.vendor.support.dataset import'),
    ('from evalscope.metrics.semantics import', 'from relay_lab.vendor.support.semantics import'),
]

BANNER = """# Vendored from EvalScope (Apache-2.0). See vendor/NOTICE.md.
# Local edits: import paths only, plus the AgentX scenario hook removed.
"""

# AgentX is an external agent scenario this lab never runs; dropping it removes
# scenarios/ and its dependencies from the vendored tree.
ARGUMENT_PATCHES = [
    'from .scenarios.agentx import AgentXScenario, parse_agentx_scenario, validate_agentx_arguments\n',
    '    scenario: Optional[AgentXScenario] = None\n'
    '    """Optional external Perf scenario. Currently supports AgentX."""\n\n',
    "    @field_validator('scenario', mode='before')\n"
    '    @classmethod\n'
    '    def _parse_scenario(cls, value: Any) -> Optional[AgentXScenario]:\n'
    '        if value is None:\n'
    '            return None\n'
    '        return parse_agentx_scenario(value)\n\n',
    '        if self.scenario is not None:\n            validate_agentx_arguments(self)\n            return\n',
    '    parser.add_argument(\n'
    "        '--scenario',\n"
    '        type=str,\n'
    '        default=None,\n'
    '        help=\'External performance scenario: "agentx" or an AgentX JSON object.\',\n'
    '    )\n',
]

API_INIT = """from .base import ApiPluginBase
from .default_api import DefaultApiPlugin
from .openai_api import OpenaiPlugin
"""

DATASET_INIT = """from .base import DatasetPluginBase
from .custom import CustomDatasetPlugin
"""

SUPPORT_INIT = '''"""Stand-ins for the framework helpers the vendored perf code imports."""

from .argument_utils import BaseArgument, get_secret_value, secretize_auth_headers
from .logger import get_logger

__all__ = ['BaseArgument', 'get_secret_value', 'secretize_auth_headers', 'get_logger']
'''

DATASET_SHIM = '''"""Hub dataset helpers are not vendored: this lab only runs local prompt files."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class DatasetNotVendored(RuntimeError):
    """Raised when upstream code asks for a hub dataset in the trimmed build."""


@dataclass(frozen=True)
class DatasetSource:
    """Same shape as upstream, only the fields the trimmed code touches."""

    dataset_id: str
    hub: Any = None
    subset: str = 'default'
    split: str = 'test'


def download_dataset_file(*args: Any, **kwargs: Any) -> Any:
    raise DatasetNotVendored('hub 数据集未随精简引擎提供，请使用本地 prompts 文件')


def load_dataset_from_hub(*args: Any, **kwargs: Any) -> Any:
    raise DatasetNotVendored('hub 数据集未随精简引擎提供，请使用本地 prompts 文件')


def download_dataset_snapshot(*args: Any, **kwargs: Any) -> Any:
    raise DatasetNotVendored('hub 数据集未随精简引擎提供，请使用本地 prompts 文件')
'''


SEMANTICS_SHIM = '''"""Display-only metric formatting; the semantic registry itself is not vendored.

Upstream uses this to render tables. Numbers we read come straight from the
metric models, so formatting precision here cannot change any measurement.
"""

from __future__ import annotations

from typing import Any, Optional


def format_perf_value(
    value: Optional[float],
    field_key: str,
    include_unit: bool = True,
    resolver: Any = None,
) -> str:
    """Render one metric cell; units are omitted because our UI adds them."""
    if value is None:
        return '-'
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        if float(value).is_integer() and abs(value) < 1_000_000:
            return f'{value:,.0f}'
        return f'{value:,.2f}'
    return str(value)


def resolve_perf_semantics(field_key: str) -> Any:
    """Kept for signature compatibility with the vendored callers."""
    return None


def get_semantics_resolver() -> Any:
    """The semantic registry is not vendored; only SLA code used it."""
    raise RuntimeError('指标语义层未随精简引擎提供')
'''


def copy(source_root: Path, relative: str, destination: str) -> None:
    source = source_root / relative
    target = TARGET / destination
    target.parent.mkdir(parents=True, exist_ok=True)
    text = source.read_text(encoding='utf-8')
    for old, new in RENAMES:
        text = text.replace(old, new)
    target.write_text(BANNER + text, encoding='utf-8')


def patch_arguments() -> None:
    path = TARGET / 'perf' / 'arguments.py'
    text = path.read_text(encoding='utf-8')
    for block in ARGUMENT_PATCHES:
        replacement = '' if block in text else block
        if block not in text:
            raise SystemExit(f'arguments.py patch not found (upstream changed?): {block[:48]!r}')
        text = text.replace(block, replacement)
    path.write_text(text, encoding='utf-8')


def patch_logger_name() -> None:
    """Log lines should not claim to come from the upstream package."""
    path = TARGET / 'support' / 'logger.py'
    text = path.read_text(encoding='utf-8')
    path.write_text(text.replace("'evalscope'", "'relay_lab'"), encoding='utf-8')


def main() -> None:
    parser = argparse.ArgumentParser(description='Copy the EvalScope perf runtime into relay_lab.vendor')
    parser.add_argument('--source', default=None, help='path to the EvalScope source tree (contains perf/)')
    options = parser.parse_args()
    source = find_source(options.source)

    for sub in ('perf', 'support'):
        if (TARGET / sub).exists():
            shutil.rmtree(TARGET / sub)
    (TARGET / 'perf').mkdir(parents=True, exist_ok=True)
    (TARGET / 'support').mkdir(parents=True, exist_ok=True)
    (TARGET / '__init__.py').write_text('"""Vendored third-party code; see NOTICE.md."""\n', encoding='utf-8')

    for relative in PERF_FILES:
        copy(source, relative, relative)
    for relative, destination in SUPPORT_FILES.items():
        copy(source, relative, destination)

    patch_arguments()
    patch_logger_name()
    (TARGET / 'perf' / 'plugin' / 'api' / '__init__.py').write_text(API_INIT, encoding='utf-8')
    (TARGET / 'perf' / 'plugin' / 'datasets' / '__init__.py').write_text(DATASET_INIT, encoding='utf-8')
    (TARGET / 'support' / '__init__.py').write_text(SUPPORT_INIT, encoding='utf-8')
    (TARGET / 'support' / 'dataset.py').write_text(DATASET_SHIM, encoding='utf-8')
    (TARGET / 'support' / 'semantics.py').write_text(SEMANTICS_SHIM, encoding='utf-8')
    print(f'vendored {len(PERF_FILES)} perf files + {len(SUPPORT_FILES)} support files into {TARGET}')


if __name__ == '__main__':
    main()

"""Hub dataset helpers are not vendored: this lab only runs local prompt files."""

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

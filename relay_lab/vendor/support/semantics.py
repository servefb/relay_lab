"""Display-only metric formatting; the semantic registry itself is not vendored.

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

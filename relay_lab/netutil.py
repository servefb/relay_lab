"""Outbound-HTTP environment hygiene.

``httpx`` derives one URL pattern per ``NO_PROXY`` entry, and a bracketed IPv6 literal
such as ``[::1]`` becomes the malformed pattern ``all://*[::1]`` whose ``:1]`` is then
read as a port. The consequence is severe: constructing *any* client with
``trust_env=True`` raises ``InvalidURL`` before a single request is sent, and more than
one common developer tool writes exactly that entry.

Rather than fork httpx or reach into its private helpers, entries it cannot parse are
dropped from this process's environment before a client is built, and every drop is
reported, so a run says what happened instead of degrading into an unexplained failure.
"""

from __future__ import annotations

import os
from functools import lru_cache

import httpx

PROXY_KEYS = ('NO_PROXY', 'no_proxy')


def _unparsable(value: str) -> list[str]:
    """Return the NO_PROXY entries httpx would turn into an invalid URL pattern."""
    return [entry for entry in value.split(',') if '[' in entry or ']' in entry]


def sanitize_proxy_environment() -> list[str]:
    """Drop NO_PROXY entries httpx cannot parse; return every entry removed.

    Unbracketed ``::1`` is deliberately kept: aiohttp (the load generator) understands
    it, so loopback still bypasses the proxy for the engine.
    """
    dropped: list[str] = []
    for key in PROXY_KEYS:
        value = os.environ.get(key)
        if not value:
            continue
        bad = _unparsable(value)
        if not bad:
            continue
        kept = [entry for entry in value.split(',') if entry not in bad]
        dropped.extend(f'{key}={entry}' for entry in bad)
        if kept:
            os.environ[key] = ','.join(kept)
        else:
            os.environ.pop(key, None)
    if dropped:
        # The probe result is a function of the environment we just changed.
        trust_env_usable.cache_clear()
    return dropped


@lru_cache(maxsize=1)
def trust_env_usable() -> bool:
    """Whether an environment-aware client can be constructed at all on this machine."""
    try:
        probe = httpx.Client(trust_env=True)
    except httpx.InvalidURL:
        return False
    probe.close()
    return True


def prepare(run) -> None:
    """Repair the proxy environment and record on *run* whatever had to be changed."""
    dropped = sanitize_proxy_environment()
    if dropped:
        run.warnings.append(
            '环境变量 ' + '、'.join(dropped) + ' 无法被 HTTP 客户端解析（带方括号的 IPv6 写法会生成'
            '非法代理规则），本次运行已忽略它；若本机需要经代理访问目标站点，请从 NO_PROXY 中移除该写法。'
        )
    if not trust_env_usable():
        run.warnings.append(
            '本机的代理配置无法被 HTTP 客户端使用，质量与契约检查将直连，不读取系统代理；'
            '若目标站点只能经代理访问，这些检查会连接失败。'
        )

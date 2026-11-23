"""Regenerate Relay Lab contract Postman collection from live probes."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from relay_lab import contract
from relay_lab.config import CONTRACT_CHECKS, ContractSettings
from relay_lab.protocols import Unsupported

OUT = Path(r'd:\work_space\evalscope-relay-lab\relay_lab\postman\Relay-Lab-Contract.postman_collection.json')

GROUPS = [
    ('01-基础与输出', ['system', 'json_schema', 'schema_errors', 'max_tokens', 'stop', 'multi_turn', 'response_shape', 'usage']),
    ('02-工具链路', ['tools', 'dynamic_tools', 'dynamic_tools_both', 'dynamic_tools_mutex', 'tool_roundtrip']),
    ('03-思考链路', ['thinking', 'preserved_thinking', 'thinking_roundtrip']),
    ('04-传输与错误', ['errors', 'stream']),
    (
        '05-缓存与路由',
        ['cache', 'cache_headers', 'cache_headers_stream', 'cache_roundtrip', 'cache_roundtrip_stream', 'routing'],
    ),
]


def make_request(settings: ContractSettings, check_id: str, order: int) -> dict | None:
    probe = contract.PROBES[check_id]
    active = contract.Probe(probe.check, probe.build, probe.judge, wire_protocol=probe.wire_protocol)
    wire = settings.model_copy(update={'protocol': active.wire_protocol}) if active.wire_protocol else settings
    try:
        body = active.build(wire, active)
    except Unsupported:
        return None
    if body.get('model') == 'kimi-k2.5':
        body['model'] = '{{model}}'
    protocol = wire.protocol
    title = contract.TITLES[check_id]
    expected = active.expected
    if protocol == 'anthropic':
        url = '{{base_url}}/messages'
        headers = [
            {'key': 'Content-Type', 'value': 'application/json'},
            {'key': 'x-api-key', 'value': '{{api_key}}'},
            {'key': 'anthropic-version', 'value': '{{anthropic_version}}'},
            {'key': 'Authorization', 'value': '', 'disabled': True},
        ]
        auth = {'type': 'noauth'}
    else:
        url = '{{base_url}}/chat/completions'
        headers = [{'key': 'Content-Type', 'value': 'application/json'}]
        auth = {'type': 'inherit'}
    desc = (
        f'检查 ID: `{check_id}`\n\n**期望**: {expected}\n\n'
        '与 Relay Lab 协议检查探针对齐（Kimi 族默认包口径）。'
    )
    if check_id in {'tool_roundtrip', 'thinking_roundtrip', 'cache_roundtrip', 'cache_roundtrip_stream'}:
        desc += '\n\n> 本请求只覆盖闭环的**第一轮**；完整判定需按响应手工续传第二轮。'
    if check_id == 'routing':
        desc += '\n\n> 建议连续发送 3 次，对比响应头 `x-new-api-version` 是否一致。'
    if check_id == 'cache':
        desc += '\n\n> 建议连发两次同一请求，对比 `usage` 中的缓存命中字段。'
    return {
        'name': f'{order:02d} · {title}',
        'request': {
            'auth': auth,
            'method': 'POST',
            'header': headers,
            'body': {
                'mode': 'raw',
                'raw': json.dumps(body, ensure_ascii=False, indent=2),
                'options': {'raw': {'language': 'json'}},
            },
            'url': url,
            'description': desc,
        },
        'response': [],
    }


def main() -> None:
    settings = ContractSettings(
        url='https://example.com/v1',
        model='kimi-k2.5',
        protocol='openai',
        checks=list(CONTRACT_CHECKS),
        cache_prefix_tokens=512,
    )
    folders = []
    for folder_name, ids in GROUPS:
        items = []
        for index, check_id in enumerate(ids, start=1):
            item = make_request(settings, check_id, index)
            if item is not None:
                items.append(item)
        folders.append({'name': folder_name, 'item': items, 'description': f'Relay Lab 协议检查 · {folder_name}'})

    collection = {
        'info': {
            '_postman_id': str(uuid4()),
            'name': 'Relay Lab · 协议检查',
            'description': (
                '与 Relay Lab「协议检查」页面对齐的探针集合（按 Kimi 族默认包生成）。\n\n'
                '## 用法\n'
                '1. 导入本 Collection 与同目录 `Relay-Lab.environment.json`\n'
                '2. 在 Environment 里填 `base_url`（如 `https://your-gateway.com/v1`，不要带 `/chat/completions`）\n'
                '3. 填 `api_key`、`model`；思考探针可改 `anthropic_version`（`2023-06-01` / `2026-10-21` / `2023-01-01`）\n'
                '4. 选中该 Environment 后逐个发送，或用 Collection Runner 批量跑\n\n'
                '所有请求复用同一套 `base_url` / `api_key` / `model` 变量。'
            ),
            'schema': 'https://schema.getpostman.com/json/collection/v2.1.0/collection.json',
        },
        'auth': {
            'type': 'bearer',
            'bearer': [{'key': 'token', 'value': '{{api_key}}', 'type': 'string'}],
        },
        'variable': [
            {'key': 'base_url', 'value': 'https://api.example.com/v1'},
            {'key': 'api_key', 'value': ''},
            {'key': 'model', 'value': 'kimi-k2.5'},
            {
                'key': 'anthropic_version',
                'value': '2023-06-01',
                'description': '可选：2023-06-01（默认/多数中继）、2026-10-21（官方最新）、2023-01-01（初版）',
            },
        ],
        'item': folders,
    }
    OUT.write_text(json.dumps(collection, ensure_ascii=False, indent=2), encoding='utf-8')
    print('wrote', OUT)
    print('requests', sum(len(folder['item']) for folder in folders))


if __name__ == '__main__':
    main()

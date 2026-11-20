"""Small, protocol-specific validators for the explicit shape and usage probes."""

from __future__ import annotations

from typing import Any


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def reasoning_leaked_into_text(reasoning: str, text: str) -> bool:
    """判断 Responses 的 output_text 是否与 reasoning 思考文本重合或发生泄漏。

    注意：不能仅因 ``clean_t in clean_r`` 就判为泄漏——推理模型常在思考草稿中预演
    合规的短正文（如单行 JSON、随机工单 status 或 60 字散文），此时 ``output_text`` 本身是干净的。
    """
    clean_r = (reasoning or '').strip()
    clean_t = (text or '').strip()
    if not clean_r or not clean_t:
        return False
    if clean_t == clean_r and len(clean_t) >= 10:
        return True
    if len(clean_r) >= 24 and clean_r in clean_t:
        return True
    if len(clean_t) >= 40 and len(clean_r) >= 40 and clean_t[:40] == clean_r[:40]:
        return True
    return False


def responses_fixed_attrs_errors(payload: Any) -> list[str]:
    """强校验 Kimi Responses 200 响应的 4 个固定属性（键必须存在，且类型与值严格对齐）。"""
    if not isinstance(payload, dict):
        return ['响应必须为 JSON 对象']
    errors: list[str] = []
    if 'store' not in payload or payload['store'] is not False:
        errors.append(f'Responses 固定属性 store 必须存在且为 false（实际：{payload.get("store", "<缺失>")!r}）')
    if 'background' not in payload or payload['background'] is not False:
        errors.append(f'Responses 固定属性 background 必须存在且为 false（实际：{payload.get("background", "<缺失>")!r}）')
    if 'previous_response_id' not in payload or payload['previous_response_id'] is not None:
        errors.append(
            f'Responses 固定属性 previous_response_id 必须存在且为 null（实际：{payload.get("previous_response_id", "<缺失>")!r}）'
        )
    if 'conversation' not in payload or payload['conversation'] is not None:
        errors.append(
            f'Responses 固定属性 conversation 必须存在且为 null（实际：{payload.get("conversation", "<缺失>")!r}）'
        )
    output = payload.get('output')
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict):
                item_id = item.get('id')
                if isinstance(item_id, str) and item_id.startswith('resp_chat_'):
                    errors.append(f'检测到 Responses 转 Chat 协议转译：output[].id={item_id!r} 带有 resp_chat_ 前缀')
                    break
    return errors


def kimi_billing_usage_errors(protocol: str, payload: Any, *, usage_obj: Any = None) -> list[str]:
    """强校验 Kimi 官方三协议 200 响应的必填计费细分字段（缺失直接报错，不得补 0）。

    - Chat Completions: ``usage.prompt_tokens_details.cache_write_tokens``
    - Responses: ``usage.input_tokens_details.cache_write_tokens``
    - Anthropic Messages: ``usage.output_tokens_details.thinking_tokens``
    """
    spec: dict[str, tuple[str, str, str]] = {
        'openai': ('prompt_tokens_details', 'cache_write_tokens', 'cache_write_tokens'),
        'openai_responses': ('input_tokens_details', 'cache_write_tokens', 'cache_write_tokens'),
        'anthropic': ('output_tokens_details', 'thinking_tokens', 'thinking_tokens'),
    }
    target = spec.get(protocol)
    if target is None:
        return []
    details_key, field_key, usage_attr = target
    field_path = f'usage.{details_key}.{field_key}'
    if isinstance(payload, dict):
        usage = payload.get('usage')
        if not isinstance(usage, dict):
            return [f'缺少必填计费字段 {field_path}（响应缺少 usage 对象）']
        details = usage.get(details_key)
        if not isinstance(details, dict) or field_key not in details or not _integer(details.get(field_key)):
            actual = details.get(field_key, '<缺失>') if isinstance(details, dict) else '<details 缺失>'
            return [f'缺少必填计费字段 {field_path}（须为非负整数，实际：{actual!r}）']
        return []
    if usage_obj is not None:
        value = getattr(usage_obj, usage_attr, None)
        if not _integer(value):
            return [f'缺少必填计费字段 {field_path}（流式响应须回传非负整数，实际：{value!r}）']
    return []


def shape_errors(protocol: str, payload: Any, content_type: str, *, strict_kimi: bool = False) -> list[str]:
    errors: list[str] = []
    media = content_type.split(';', 1)[0].strip().lower()
    if media != 'application/json' and not media.endswith('+json'):
        errors.append('Content-Type 必须为 JSON')
    if not isinstance(payload, dict):
        return errors + ['响应必须为 JSON 对象']
    if payload.get('error'):
        errors.append('成功响应中包含 error')

    def string(obj, key):
        if not isinstance(obj.get(key), str) or not obj[key].strip():
            errors.append(f'{key} 必须为非空字符串')

    def blocks(content, allowed):
        if not isinstance(content, list) or not content:
            errors.append('content 必须为非空数组')
            return
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get('type'), str) or block['type'] not in allowed:
                errors.append('content 包含不符合协议的内容块')
                continue
            kind = block['type']
            if kind in {'text', 'output_text', 'thinking'}:
                key = 'thinking' if kind == 'thinking' else 'text'
                if not isinstance(block.get(key), str):
                    errors.append(f'{kind} 块的 {key} 必须为字符串')
            if kind == 'tool_use':
                string(block, 'id')
                string(block, 'name')
                if not isinstance(block.get('input'), dict):
                    errors.append('tool_use.input 必须为对象')
            if kind == 'thinking':
                # Compatibility gateways may omit signature; only enforce when present.
                if 'signature' in block and (
                    not isinstance(block.get('signature'), str) or not str(block.get('signature') or '').strip()
                ):
                    errors.append('thinking 块的 signature 必须为非空字符串')
            if kind == 'redacted_thinking':
                string(block, 'data')
            if kind == 'refusal':
                string(block, 'refusal')

    if protocol == 'openai':
        for key in ('id', 'model'):
            string(payload, key)
        if payload.get('object') != 'chat.completion':
            errors.append('object 必须为 chat.completion')
        if not _integer(payload.get('created')):
            errors.append('created 必须为非负整数')
        choices = payload.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            return errors + ['choices 必须包含一个对象']
        choice = choices[0]
        string(choice, 'finish_reason')
        if choice.get('index') != 0 or not _integer(choice.get('index')):
            errors.append('单候选的 choice.index 必须为 0')
        if choice.get('finish_reason') not in ('stop', 'length', 'tool_calls', 'content_filter', 'function_call'):
            errors.append('finish_reason 不符合 Chat 协议')
        message = choice.get('message')
        if not isinstance(message, dict):
            return errors + ['message 必须为对象']
        if message.get('role') != 'assistant':
            errors.append('message.role 必须为 assistant')
        if not isinstance(message.get('content'), (str, list)) and not (
            message.get('content') is None and isinstance(message.get('tool_calls'), list) and message['tool_calls']
        ):
            errors.append('message.content 类型无效')
        if isinstance(message.get('content'), list):
            blocks(message['content'], {'text', 'refusal'})
        # Gateways often echo "tool_calls": null on plain text replies — only
        # validate when the field is present and not null.
        if message.get('tool_calls') is not None:
            calls = message['tool_calls']
            if not isinstance(calls, list) or not calls:
                errors.append('tool_calls 必须为非空数组')
            else:
                for call in calls:
                    if not isinstance(call, dict) or call.get('type') != 'function' or not isinstance(call.get('function'), dict):
                        errors.append('tool_calls 必须包含 function 对象')
                        continue
                    string(call, 'id')
                    string(call['function'], 'name')
                    string(call['function'], 'arguments')
    elif protocol == 'anthropic':
        for key in ('id', 'model', 'stop_reason'):
            string(payload, key)
        if payload.get('type') != 'message' or payload.get('role') != 'assistant':
            errors.append('Messages 必须返回 type=message、role=assistant')
        blocks(payload.get('content'), {'text', 'thinking', 'redacted_thinking', 'tool_use'})
    elif protocol == 'openai_responses':
        string(payload, 'id')
        string(payload, 'model')
        if payload.get('object') != 'response':
            errors.append('object 必须为 response')
        if payload.get('status') not in ('completed', 'incomplete'):
            errors.append('Responses 缺少有效终态')
        output = payload.get('output')
        if not isinstance(output, list) or not output:
            return errors + ['output 必须为非空数组']
        reasoning_texts: list[str] = []
        output_texts: list[str] = []
        for item in output:
            if not isinstance(item, dict):
                errors.append('output 条目必须为对象')
            elif item.get('type') == 'message':
                if item.get('role') != 'assistant':
                    errors.append('output message.role 必须为 assistant')
                blocks(item.get('content'), {'output_text', 'refusal'})
                for part in item.get('content') or []:
                    if isinstance(part, dict) and part.get('type') in ('output_text', 'text') and isinstance(part.get('text'), str):
                        output_texts.append(part['text'])
            elif item.get('type') == 'function_call':
                for key in ('call_id', 'name', 'arguments'):
                    string(item, key)
            elif item.get('type') == 'reasoning':
                for part in item.get('summary') or item.get('content') or []:
                    if isinstance(part, dict) and isinstance(part.get('text'), str):
                        reasoning_texts.append(part['text'])
            else:
                errors.append('output 条目类型不符合本检查的文本请求')
        if reasoning_leaked_into_text(''.join(reasoning_texts), ''.join(output_texts)):
            errors.append('Responses 转换异常：output_text 正文与 reasoning 思考内容重合/泄漏')
        if strict_kimi:
            errors.extend(responses_fixed_attrs_errors(payload))
    else:
        candidates = payload.get('candidates')
        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
            return errors + ['candidates 必须为非空对象数组']
        candidate = candidates[0]
        string(candidate, 'finishReason')
        content = candidate.get('content')
        if isinstance(content, dict) and content.get('role') != 'model':
            errors.append('candidate.content.role 必须为 model')
        if not isinstance(content, dict) or not isinstance(content.get('parts'), list) or not content['parts']:
            errors.append('candidate.content.parts 必须为非空数组')
        elif any(not isinstance(part, dict) or not isinstance(part.get('text'), str) for part in content['parts']):
            errors.append('文本请求的 parts 必须包含有效 text')
    return errors


def usage_errors(protocol: str, payload: Any, *, strict_kimi: bool = False) -> list[str]:
    if not isinstance(payload, dict):
        return ['响应必须为 JSON 对象']
    key = 'usageMetadata' if protocol == 'gemini' else 'usage'
    usage = payload.get(key)
    if not isinstance(usage, dict):
        return [f'缺少 {key} 对象']
    required = {
        'openai': ('prompt_tokens', 'completion_tokens'),
        'openai_responses': ('input_tokens', 'output_tokens'),
        'anthropic': ('input_tokens', 'output_tokens'),
        'gemini': ('promptTokenCount', 'candidatesTokenCount'),
    }[protocol]
    errors = [f'{name} 必须为非负整数' for name in required if not _integer(usage.get(name))]
    optional = ('total_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'totalTokenCount', 'cachedContentTokenCount')
    for name in optional:
        if name in usage and usage[name] is not None and not _integer(usage[name]):
            errors.append(f'{name} 必须为非负整数')
    for name in ('prompt_tokens_details', 'input_tokens_details', 'completion_tokens_details', 'output_tokens_details', 'cache_creation'):
        if name not in usage:
            continue
        details = usage[name]
        if not isinstance(details, dict):
            errors.append(f'{name} 必须为对象')
            continue
        for field, value in details.items():
            if field.endswith('tokens') and value is not None and not _integer(value):
                errors.append(f'{name}.{field} 必须为非负整数')
    if protocol in {'openai', 'openai_responses'} and not errors:
        if (
            'total_tokens' in usage
            and usage['total_tokens'] is not None
            and usage['total_tokens'] != sum(usage[k] for k in required)
        ):
            errors.append('total_tokens 与输入、输出之和不符')
        details = usage.get('prompt_tokens_details' if protocol == 'openai' else 'input_tokens_details') or {}
        if (details.get('cached_tokens') or 0) > usage[required[0]]:
            errors.append('cached_tokens 超过输入 token 数')
    if strict_kimi:
        errors.extend(kimi_billing_usage_errors(protocol, payload))
    return errors

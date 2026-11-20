# Kimi K3.1 Pro 协议检查整改条例

**范围：** `kimi-k3.1-pro` 的 Chat Completions、Responses 和 Anthropic Messages 检查。以下为待实施、待验收事项。其他模型不直接复用 K3.1 Pro 的专属断言。

## 一、判定规则

| 判定 | 使用条件 |
| --- | --- |
| `conformed` | 有证据证明满足被测的官方契约。HTTP 状态码不能单独作为通过依据。 |
| `violated` | 响应明确违反契约，例如返回 HTTP 200 但正文不是合法的协议响应。 |
| `inconclusive` | 鉴权、限流、超时、对照请求失败或证据不足。记录原因，不计为通过。 |
| `observation` | 中转扩展、模型行为差异或性能现象。单独记录，不计作官方兼容。 |

正向用例先校验响应结构和终态，再检查业务字段；负向用例先确认合法对照可用，再检查拒绝原因是否指向被测字段。不要要求错误文案逐字相同。模型没按提示回答、但没有协议层证据时，不得据此判定中转改写。协议不适用时标明原因，不发请求。

## 二、P1：消除误判通过

| 编号 | 用例 | 整改与验收标准 |
| --- | --- | --- |
| K3-01 | `stream_usage` | Chat 显式请求 `stream_options.include_usage=true`；校验完整的 SSE 及 `[DONE]`，并检查 `[DONE]` 前最后一个数据块为 `choices: []`，包含非负整数的 `prompt_tokens`、`completion_tokens`、`total_tokens`，且总量一致。只有 `prompt_tokens`、缺 `[DONE]`、用量只在中间包、总量不一致均不得通过。Responses 和 Messages 按各自终态和用量字段验收。块间隔不是官方合规门槛，单列作缓冲诊断。 |
| K3-02 | `vision_url` 与正向视觉 | 公网图片 URL 是 K3.1 Pro 的拒绝用例；中转自行支持则记扩展。另用仓库内的有效图片做 Base64 正例，有条件再补 `ms://` 正例。答案不得出现在提示、文件名或 URL 中。HTTP 200 非 JSON 不得通过；正例必须读出预期内容，不能只验 HTTP 200。 |
| K3-03 | `stop` | 用成对请求，除 `stop` 外保持参数一致并给足输出预算。无 `stop` 的对照必须输出明确哨兵及后缀；有 `stop` 时应保留预期前缀、省略哨兵和后缀，并以正常终态结束。只返回 `1` 且 `finish_reason=length`、对照没输出哨兵、两次都提前结束，均不得通过。 |
| K3-04 | `tool_choice_named`、`reasoning_effort_invalid`、固定采样参数 | 每次只改一个参数。Chat 先证明同样的工具定义用 `required` 可调用，再要求具名选择的 400 指向 `tool_choice` 或思考冲突。非法档位和固定采样参数的拒绝也必须指向对应字段。`400 messages is required`、无关的递归 Schema 错误、合法对照失败，均不得给被测项判通过。 |

## 三、P2：补齐 K3.1 Pro 覆盖

- **K3-05 推理档位：** Chat 用 `reasoning_effort`，Responses 用 `reasoning.effort`，Messages 用 `output_config.effort`。分别测试 `low`、`high`、`max`、省略字段和非法值。官方默认是 `max`；思考内容非空只证明有思考输出，不证明指定档位生效。单次延迟或 token 差异也不能作为档位生效的硬断言。
- **K3-06 工具和多轮：** 首轮校验用例要求的工具名、参数、ID 和终态；Chat 第二轮原样回传完整 assistant message，包括 `reasoning_content` 和 `tool_calls`，再附上匹配 `tool_call_id` 的结果。工具结果使用首轮提示未暴露的随机标记。Responses 和 Messages 各用自己的续传结构。补测 ID 不匹配、第二轮缺失、assistant 消息被裁剪。
- **K3-07 Messages 适配：** 官方站用 `/anthropic/v1/messages` 和 Bearer 鉴权；中转的认证另行配置。结构化输出用 `output_config.format`（`type: json_schema` 与 `schema`），不因通用构造器缺少 `response_format` 而跳过。三套协议分别保存脱敏的官方响应样例，不混用字段和终态。

## 四、Postman 交付

- [ ] **K3-08** 生成独立的 `kimi-k3.1-pro` Collection。三套协议分开配置 URL 和认证，输出路径相对于当前仓库或由参数指定。不写入真实密钥，不靠替换 K2.5 请求体的模型名来生成 K3 用例。
- [ ] 自动验收项须有 `pm.test`，断言名带用例 ID，失败时显示实际状态或字段。Runner 无法可靠观察的指标明确标为手工或后端验收。
- [ ] 闭环必须验证第二轮。第一轮通过后才保存同次执行的状态，结束时清理；第一轮失败、第二轮没发出或断言没执行时，不得出具闭环通过结论。
- [ ] 用本地 fixture 验证 Runner：合规响应能通过；破坏字段或终态后，断言失败且退出码非零。报告区分通过、失败和跳过，不把模拟结果写成官方实测通过。

## 五、验收与放行

1. 先补 K3-01 至 K3-04 的反例，让旧实现复现误判；修改后要求反例不通过，合法正例不回归。
2. 运行 `python -m pytest -q tests/test_contract.py tests/test_contract_extended.py`，报告新增覆盖和跳过原因。上轮审查已有 364 项通过，现有测试全绿不能替代新增反例验收。
3. 本地验收后，再用同一模型、同一协议、同一请求体对照官方直连和中转。付费实测单独确认授权与请求量、token 预算；密钥及敏感原始响应不得进入共享报告。未直连时标注「仅文档比对与本地验证」。
4. **放行门槛：** P1 反例无一拿到 `conformed`，合法正例全部通过，Postman 自动项实际执行了断言。未完成项列为待验收，不计入兼容通过率。

## 官方参考

[K3.1 Pro 快速开始](https://platform.kimi.com/docs/guide/kimi-k3-1-pro-quickstart)、[模型参数参考](https://platform.kimi.com/docs/api/models-overview)、[流式输出](https://platform.kimi.com/docs/guide/utilize-the-streaming-output-feature-of-kimi-api)、[工具调用约束](https://platform.kimi.com/docs/guide/use-tool-choice)、[Chat](https://platform.kimi.com/docs/api/chat)、[Responses](https://platform.kimi.com/docs/api/responses)、[Messages](https://platform.kimi.com/docs/api/messages)。

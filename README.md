# relay_lab backend

中转站容量实验室的后端服务。**不依赖任何外部评测框架**：压测调度与统计代码已复制到
`relay_lab/vendor/`（来自 EvalScope，Apache-2.0，见 `vendor/NOTICE.md`），整个目录可以搬到
任意位置、在装了 `requirements.txt` 的任意 Python 3.10+ 环境里独立运行。

所有路径都以本目录为基准推导，不假设自己在某个仓库里。

支持四种线上协议：**OpenAI Chat Completions、OpenAI Responses、Anthropic Messages、Gemini**
（`protocol` 字段选择；请求构造与响应解析在 `protocols.py`，与容量、质量、契约三类实验共用）。

```bash
pip install -r requirements.txt          # 开发/自检再加 -r requirements-dev.txt（pytest、ruff）
python -m relay_lab --port 9011          # 在本目录内执行
# 或在任意目录执行：
python -m relay_lab --port 9011 --data /path/to/reports
```

Windows 上也可以直接双击 `start.cmd`（自动选本地 `.venv`，没有就退回 PATH 上的 python），
`stop.ps1` 只停这个服务：它会先停掉进行中的实验，再核对命令行里确实是 `-m relay_lab`。

## 参数

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--host` | `127.0.0.1` | 默认只监听本机 |
| `--port` | `9011` | |
| `--data` | `./data`（相对本目录） | 报告目录，可指向任意路径 |
| `--web` | `./web/dist` | 已构建的控制台；找不到就只提供接口（`/` 返回 503） |
| `--no-scheduler` | 关 | 不启动定时任务循环 |

控制台源码在 `web/`（Vite + React）。开发与构建：

```bash
cd web
npm install
npm run dev      # http://127.0.0.1:5173 ，代理到本机 9011
npm run build    # 产出 web/dist，供 python -m relay_lab 直接挂载
```

控制台也可以放在任意位置，用 `--web <dir>` 或环境变量 `RELAY_LAB_WEB` 指定。

接口文档：`http://127.0.0.1:9011/api/docs`。

## 测试

```bash
python -m pytest
```

`pytest.ini` 已把临时目录固定到 `.pytest-tmp`，Windows 上不需要额外参数。

## AI 后端接口

报告解读使用 OpenAI Python SDK 的 `AsyncOpenAI`；对话及工具编排使用 OpenAI Agents SDK 的
`Agent` 和 `Runner.run_streamed()`。不改写现有实验引擎，不调用任何实验启动或调度接口。

在服务端配置 `RELAY_LAB_AI_MODEL` 和 `RELAY_LAB_AI_API_KEY`。可选：

| 环境变量 | 用途 |
| --- | --- |
| `RELAY_LAB_AI_URL` | SDK 基址，默认 `https://api.openai.com/v1`；中转站基址按其规范填写，不带 `/chat/completions` 或 `/responses` |
| `RELAY_LAB_AI_PROTOCOL` | `openai`（默认，Chat Completions）或 `openai_responses`（Responses），失败不自动切换 |
| `RELAY_LAB_AI_ALLOWED_ORIGINS` | 逗号分隔的受信上游来源（协议、域名、端口），启动时写入白名单；登录后在控制台保存的新基址也会加入当前进程白名单。SDK 不跟随重定向 |

| 接口 | 约定 |
| --- | --- |
| `GET /api/ai/config` | 返回配置和 `has_key`、`configured`，不返回 Key |
| `PUT /api/ai/config` | 部分更新，只在当前进程生效。相同基址、省略 `api_key` 则保留 Key；更换基址默认清除 Key；也可显式传 `null` 清除 |
| `POST /api/ai/test` | 发送一次小型推理请求，确认基本连通，不验证工具调用能力 |
| `POST /api/runs/{run_id}/analyze` | 可选 `{"force":false,"focus":""}`。六类终态报告可解读；默认缓存成功结果，定向 `focus` 不覆盖缓存 |
| `POST /api/ai/chat` | `{"messages":[{"role":"user","content":"对比最近两份报告"}]}`；可选 `active_run_id`、`active_view`。无服务端聊天历史，最多 5 次模型轮次 |

上述接口沿用登录与同源校验。两个流接口返回 SSE：`delta` 含 `text`；对话还可发出 `tool_start`、
`tool_end` 和经校验的 `config_draft`。请求在流开始前不合法时返回 HTTP 错误；流开始后终止事件为
`done` 或 `error`。解读的 `done` 含 `analysis`、`cached`、`persisted`。正常断开时取消上游；已提交的完整解读不会回滚。

AI 上游会收到对话内容、裁剪后的报告和只读工具结果，请只配置受信服务，不要在对话中输入凭据。
Agents SDK 追踪默认在本项目的每次对话中关闭。草稿不含连接、Key、授权字段，必须回到实验入口手动确认。
`python -m pytest tests/test_ai.py` 使用本地 Mock 验证 SDK 请求，不发送真实流量。

## 环境注意事项

**`NO_PROXY` 里的方括号 IPv6 写法会让 HTTP 客户端直接崩溃。** 例如
`NO_PROXY=localhost,127.0.0.1,::1,[::1]`（部分开发工具会这样写）会被 `httpx` 解析成非法的
代理规则 `all://*[::1]`，于是**构造任何 `trust_env=True` 的客户端都会立刻抛
`InvalidURL: Invalid port: ':1]'`**，一个请求都发不出去。

`netutil.py` 在每次实验启动时做两件事，并把结果写进实验的 `warnings`：

1. 从本进程环境中摘掉 `httpx` 无法解析的 `NO_PROXY` 条目（仅限带方括号的写法，
   不带方括号的 `::1` 会保留，aiohttp 压测引擎仍能正常走直连）；
2. 探测环境代理是否可用；不可用时质量与契约检查改为直连，并在报告里说明。

这样问题是"被记录并降级"，而不是整轮实验变成一个没有原因的 `执行失败`。

## 目录

```
backend/
  relay_lab/
    __main__.py    CLI 入口，uvicorn 启动
    api.py         FastAPI 路由、同源中间件、错误体统一、静态挂载
    config.py      Pydantic 契约与校验
    search.py      容量搜索 / 速度 / 并行 / 质量 / 协议契约的运行控制
    engine.py      压测适配层：构造参数、跑一次、换算指标
    protocols.py   四种线上协议的适配层（请求构造与响应解析）
    contract.py    中转站协议保真度探测（22 项检查，按模型族选择默认项）
    window.py      滑动窗口 TPM 与分位序列
    netutil.py     出站 HTTP 的代理环境自愈
    quality.py     52 道内置质量题与规则判分
    scheduler.py   定时任务与内存态密钥
    runner.py      子进程执行、超时与停止
    store.py       报告持久化
    discovery.py   模型列表获取
    demo.py        本机 loopback SSE 模拟接口
    vendor/        复制自 EvalScope 的压测引擎（Apache-2.0）
      perf/        调度、SSE、指标聚合
      support/     logger、常量、格式化等支撑与 shim
      NOTICE.md    来源与改动说明
  web/             控制台源码（Vite + React）
    src/           页面与组件
    dist/          构建产物（可选，缺了只影响页面；npm run build 生成）
  data/            默认报告目录（可用 --data 改）
  tools/
    vendor_engine.py        重新复制上游代码的脚本（--source 指定上游源码）
    check_no_upstream.py    拦截 evalscope 导入后跑完整实验的自检脚本
    check_protocols.py      四种协议的 URL / 请求体 / 响应解析自检（无网络）
    check_contract_suite.py 用「会改写的站点」和「诚实的站点」双向验证契约套件
  tests/           98 个行为用例
  start.cmd        本机启动
  stop.ps1         本机停止（先停实验再停进程）
  LICENSE          Apache-2.0（vendor 代码沿用同一许可）
  requirements.txt
  requirements-dev.txt
  pytest.ini
  pyproject.toml
```

## 五类实验

| 类型 | 入口 | 回答的问题 | 是否产生规模流量 |
| --- | --- | --- | --- |
| `capacity` 容量 | `POST /api/runs` | 这个站点的 RPM / TPM 上限在哪 | 是（预算封顶） |
| `speed` 测速 | `POST /api/speed` | 固定请求数下的延迟与 TTFT | 少量 |
| `parallel` 并行 | `POST /api/parallel` | 并发逐档上升时吞吐如何变化 | 中等 |
| `quality` 质量 | `POST /api/quality` | 模型答得对不对（52 道固定题、规则判分） | 少量 |
| `contract` 协议 | `POST /api/contract` | 中转站有没有改写我的请求 | 单轮最多 28 次，支持每项重复 1–3 次 |

### 协议保真度检查（contract）

容量数字只说明"答得多快"，不说明"答的是不是我问的"。中转站常见的改写——
丢掉 `tools`、忽略 `stop`、改掉 `max_tokens`、把流式缓冲成一次性、只转发最后一条消息、
换用更便宜的模型——每一条都会让容量看起来比实际更好。

`POST /api/contract` 直接查请求层，共 22 项；按模型族选择默认项，无法表达的协议检查记为无法判定：

| 检查 | 高危 | 判定依据 |
| --- | --- | --- |
| `tools` | 是 | 必须恰好一次 lookup_ticket(T-1024)，参数须为完整合法 JSON、无额外字段；校验调用 ID 与协议结束原因 |
| `dynamic_tools` | 是 | system 消息声明的工具是否能被加载并调用 |
| `dynamic_tools_both` | 是 | system.tools 与不同的顶层工具并存时，仍须正确调用指定工具 |
| `dynamic_tools_mutex` | 是 | 同一 system 同时携带 content 与 tools，须返回 400 且说明互斥 |
| `json_schema` | 是 | 严格 schema 下字段/类型不符即视为约束未生效 |
| `schema_errors` | 否 | 非法 schema 是否返回规范的 400 错误类型 |
| `max_tokens` | 是 | 报告输出计数必须为非负整数，超过实际请求上限即不通过 |
| `stop` | 是 | 停止序列之后的数字仍然出现 |
| `stream` | 是 | 只统计正文/思考增量；至少 3 块、跨度 >20 ms 且至少两次间隔 ≥5 ms；须有协议正常结束事件，畸形 SSE、错误事件、结束后继续输出或缺少结束事件均不通过 |
| `system` | 否 | system 指令是否压过用户指令 |
| `multi_turn` | 否 | 能否引用上一轮内容 |
| `thinking` | 否 | 固定用 Anthropic Messages 启用 thinking，成功响应缺少 thinking 块即不通过 |
| `preserved_thinking` | 是 | 注入 reasoning_content，校验其信息能用于后续回答 |
| `thinking_roundtrip` | 是 | 首轮实际生成 n 并只回复 OK；完整回传 assistant（含思考与签名），第二轮须只回复 n×2 |
| `tool_roundtrip` | 是 | 完整保留首轮协议消息并匹配调用 ID 回传随机工具结果，第二轮须只回复结果且不再调用工具 |
| `errors` | 否 | 无效模型须返回可读的 4xx，error.type 必须为 resource_not_found_error，映射错误或缺失即不通过 |
| `cache` | 否 | 长前缀第二次发送是否命中（只认服务端计数） |
| `cache_headers` | 否 | 使用随机隔离长前缀请求 1h 写入：HTTP 200、写入 >0、首次读取 =0，且 OpenAI 的 1h 响应头或 Messages 的 usage 1h 计数 >0，任一不符即不通过 |
| `cache_roundtrip` | 否 | 验证隔离前缀 1h 写入，等待至少 15 秒后原样重发，须报告读取 >0；首轮失败即终止 |
| `routing` | 否 | 连续请求是否落到多个上游版本 |
| `response_shape` | 是 | Content-Type、协议类型、必需字段、角色、内容块和结束原因是否合法 |
| `usage` | 否 | 必需 token 计数为非负整数，检查总数及缓存边界，拒绝布尔、字符串、负数和非法详情字段 |

`json_schema` 要求整段正文严格为 JSON；代码围栏、解释文字、尾随内容、重复字段及 NaN/Infinity 均不通过。非法 schema 必须返回 `400 + error.type=invalid_request_error`，仅 code/message 不算通过。

`per_check` 会实际重复执行每项检查，每条结果记录 `repeat` 和实际发送的 `requests`；不支持的检查为 0 次，首轮失败的两轮检查为 1 次。`cache_wait_seconds` 默认为 15 秒，允许 15–300 秒，等待和多轮请求之间均支持停止。

**判定口径（重要）**：

- 只有"请求被改写"的**证据**才算 `violated`。模型不支持某项能力（纯文本模型收图片、
  站点没实现 `json_schema`）记为 `inconclusive`——把能力缺失说成造假会让整套检查失去意义。
- 缺失数据不算通过：无法取得测量数据记为 `inconclusive`；已明确请求的 thinking 块、错误类型、1h 缓存写入必要字段缺失记为 `violated`。
- `fidelity` 只在"有证据的项"上计算，`inconclusive` 单独计数，不摊进百分比。
- 缓存只认服务端返回的命中计数，**不凭耗时推断命中**。
- 1h 缓存写入检查对齐 Postman 第 12/14 项；缺失的读取计数按 0 处理，写入和 1h 计数必须为有效正数。字段只能证明服务端报告了 1h 写入，不能证明实际存活满 1 小时。
- 缓存闭环对齐 Postman 第 13/15 项：同一请求体、至少等待 15 秒、读取计数必须为正数；同样不代表已实测 1 小时存活。

### 指标口径

- 分位数同时给出 **P50 / P95 / P99**。短档位上 P99 常常就是最大值，P95 更稳定。
- **滑动窗口 TPM**：`window.py` 按每请求完成时间重建尾随窗口，能看出"前段很猛后段停摆"
  这种均值掩盖的塌陷；空数据返回空序列，不画假零线。
- **实测并发**：用 Little's Law（吞吐 × 平均延迟）算出来的**达成**并发，
  与请求的并发档位分开记录，两者不一致本身就是一个信号。

## 与上游引擎的关系

- 复制进来的只是运行路径：`arguments`、`benchmark`、`core`（调度与 SSE）、
  `plugin.api`（openai/default）、`plugin.datasets`（base/custom）与指标聚合。
  SLA 调优、AgentX 场景、hub 数据集、HTML 报告都没有复制，它们还依赖上游的注册表与语义层。
- 改动只有导入路径、去掉 AgentX 钩子、收紧插件注册，测量逻辑未改。
- 想跟进上游更新：`python tools/vendor_engine.py --source /path/to/evalscope`（也认环境变量
  `EVALSCOPE_SRC`），脚本重写 `vendor/perf` 与 `vendor/support`，保留 `NOTICE.md`。

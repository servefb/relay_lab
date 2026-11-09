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

控制台也可以放在任意位置，用 `--web <dir>` 或环境变量 `RELAY_LAB_WEB` 指定。

接口文档：`http://127.0.0.1:9011/api/docs`。

## 测试

```bash
python -m pytest
```

`pytest.ini` 已把临时目录固定到 `.pytest-tmp`，Windows 上不需要额外参数。

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
    contract.py    中转站协议保真度探测（9 项检查）
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
  web/dist/        控制台构建产物（可选，缺了只影响页面）
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
| `contract` 协议 | `POST /api/contract` | 中转站有没有改写我的请求 | 极少（约 10 次） |

### 协议保真度检查（contract）

容量数字只说明"答得多快"，不说明"答的是不是我问的"。中转站常见的改写——
丢掉 `tools`、忽略 `stop`、改掉 `max_tokens`、把流式缓冲成一次性、只转发最后一条消息、
换用更便宜的模型——每一条都会让容量看起来比实际更好。

`POST /api/contract` 直接查请求层，共 9 项：

| 检查 | 高危 | 判定依据 |
| --- | --- | --- |
| `tools` | 是 | 强制要求工具调用，返回文本即视为丢弃 |
| `json_schema` | 是 | 严格 schema 下字段/类型不符即视为约束未生效 |
| `max_tokens` | 是 | 报告输出远超上限（>4 倍）才算改写 |
| `stop` | 是 | 停止序列之后的数字仍然出现 |
| `stream` | 是 | 只有一个数据块 = 假流式 |
| `system` | 否 | system 指令是否压过用户指令 |
| `multi_turn` | 否 | 能否引用上一轮内容 |
| `errors` | 否 | 无效模型是否返回可读的非 2xx |
| `cache` | 否 | 长前缀第二次发送是否命中（只认服务端计数） |

**判定口径（重要）**：

- 只有"请求被改写"的**证据**才算 `violated`。模型不支持某项能力（纯文本模型收图片、
  站点没实现 `json_schema`）记为 `inconclusive`——把能力缺失说成造假会让整套检查失去意义。
- 缺失数据不算通过：没有 usage、没有缓存字段、没有 SSE 分帧，一律 `inconclusive` 并写明原因。
- `fidelity` 只在"有证据的项"上计算，`inconclusive` 单独计数，不摊进百分比。
- 缓存只认服务端返回的命中计数，**不凭耗时推断命中**。

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

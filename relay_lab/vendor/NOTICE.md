# 第三方代码说明

`relay_lab/vendor/` 下的代码复制自 **EvalScope**（https://github.com/modelscope/evalscope），
用于提供压测调度、SSE 解析与延迟/Token 统计。上游项目采用 Apache License 2.0。

- 上游版本：v1.14.1（基线提交 `9d298b839375e735fbbef992b4042e0852d0b8aa`）
- 复制方式：`tools/vendor_engine.py`，可重复执行
- 复制范围：`evalscope/perf` 的运行路径（arguments / benchmark / core / plugin.api /
  plugin.datasets.base+custom / utils），以及少量支撑模块（logger、constants、
  argument_utils、tqdm_utils、io_utils、uri_utils、model_utils、asyncio_runtime、
  import_utils）

## 我们对上游代码做的改动

只改了导入路径与一处场景钩子，行为未变：

1. 所有 `evalscope.*` 导入重写为 `relay_lab.vendor.*`。
2. `perf/arguments.py`：删除 AgentX 场景的字段、校验器和 `--scenario` 参数
   （本工具不运行外部 agent 场景，删除后不再需要 `perf/scenarios/`）。
3. `perf/plugin/api/__init__.py`、`perf/plugin/datasets/__init__.py`：只注册
   openai / default 两种 API 插件与 custom 数据集插件，未复制的插件不再被导入。

## 未复制的部分

`perf/sla/`（SLA 自动调优）、`perf/scenarios/`（AgentX）、`perf/utils/report/`（HTML 报告）、
`rich_display`、`local_server`、`multi_turn_benchmark` 以及 hub 数据集插件。
这些模块依赖框架的注册表、语义层与报告层，本工具不使用。

`vendor/support/` 中以 shim 形式补齐的符号：`dataset`（hub 数据集，调用即报错）、
`semantics`（仅用于表格格式化的 `format_perf_value`）。

## 许可证

上游版权归 ModelScope / Alibaba 及其贡献者所有，代码保留 Apache-2.0 许可。
本目录上两级的 `LICENSE` 为同一份 Apache-2.0 文本；分发本目录时请一并保留本文件。

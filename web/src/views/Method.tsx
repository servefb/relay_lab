import { Card, Notes } from '../components/ui';
import { useLab } from '../store';

const DEFAULT_WARNINGS = [
  '这是基于 EvalScope 压测引擎的容量探索，不是 MLPerf 认证测试。',
  'RPM 逐档提高目标速率；TPM 逐档提高并发。缺少指标时不判为达标。',
  '达到配置或预算上限时，只能报告已观测下界，不虚构最大值。',
];

export function Method() {
  const { runs, selectedId } = useLab();
  const run = runs.find((item) => item.run_id === selectedId);
  return (
    <>
      <p className="view-sub">这些是结果的适用边界，读数前请先看一遍。</p>
      <div className="stack">
        <div className="grid-2 align-start">
          <Card title="怎么读协议结论" desc="协议检查页的保真度与判定口径。">
            <Notes items={[
              '快速检查：核心高信号项 + 主协议，跳过缓存闭环等待；全量检查：完整 KVV 与多协议矩阵。',
              '全量默认多选 Chat Completions、Responses、Anthropic Messages；至少保留一种。',
              '每一项对照的是该模型族官方文档/示例契约，不是通用“好不好用”。',
              '“应该返回”来自官方约定；“实际返回”是中转站真实响应；两者不一致才判被改写。',
              '期望写明「规范 4xx」时，官方式参数拒绝记为符合；认证/限流 4xx 或证据不足仍为无法判定。',
              '「观察」是软证据（如缓存写入为 0、具名 tool_choice 被拒），不计入保真度，也不算被改写。',
              '保真度只算有硬证据的项；「无法判定 / 观察」不进分母，也不等于造假。',
              '缓存只认服务端命中计数；流式只认多块到达，不凭耗时推断。Kimi 专属缓存检查不会套到 GLM/通用模型。',
              '本页测协议保真，不评价答题质量——那是质量抽查的事。',
            ]} />
          </Card>
          <Card title="本次实验的限制" desc={run ? `${run.settings.label} · 配置指纹 ${run.fingerprint} · 报告不包含 API Key` : '尚未选择实验，显示默认口径。'}>
            <Notes items={run ? run.warnings : DEFAULT_WARNINGS} />
          </Card>
        </div>

        <Card title="实现边界" desc="哪些部分来自 EvalScope，哪些是 relay_lab 自有。">
          <Notes items={[
            '调度、HTTP、SSE、Token 与延迟统计使用 EvalScope 的压测引擎；搜索策略、预算、判定、质量题集、协议检查与页面是 relay_lab 自有实现。',
            '支持四种线上协议：Chat Completions、Responses、Anthropic Messages、Gemini；请求构造与响应解析共用同一适配层。',
            '协议检查有两种模式：快速（核心高信号项 + 主协议，跳过 15s 缓存等待）与全量（完整 KVV 专项、多协议矩阵与缓存 TTL 闭环）；默认全量。',
            '全量模式下可多选 Chat Completions、Responses、Anthropic Messages（不含 Gemini）；快速模式默认只跑主协议；与其它测试页的单选协议互不影响。',
            '速度/并行/质量/协议四类走同一套引擎或自写客户端，但都不做容量推断。',
            '默认质量题集为 MMLU 精选四选一子集（本地字母判分），另可选内置冒烟题；都不是完整榜单跑分。',
            '协议检查的“无法判定”不等于“没问题”：站点不支持某能力时无法从外部区分，只能如实标注。',
            '缺少延迟或 Token 指标时判为“未判定”，不会推成达标。',
            '控制台需登录；API Key 与 AI 密钥仅存当前会话内存，实验报告与 AI 对话均不落盘明文凭据。',
            'Orbit 助手只读分析历史实验与报告，不代发压测请求；解读结论仍以本页口径为准。',
          ]} />
        </Card>
      </div>
    </>
  );
}

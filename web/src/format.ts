import type { ContractItem, LabRun } from './types';

export function fmt(value: number | null | undefined, decimals = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toLocaleString('zh-CN', { maximumFractionDigits: decimals });
}

export const dateTime = (iso: string): string => new Date(iso).toLocaleString('zh-CN');

export const STATE_LABEL: Record<string, string> = {
  running: '测试中', stopping: '正在停止', stopped: '已停止', completed: '已完成',
  error: '执行异常', interrupted: '已中断', passed: '达标', failed: '不达标', unknown: '未判定',
};
export const FINDING_LABEL: Record<string, string> = {
  pending: '等待测试', bounded: '已发现不达标边界', lower_bound: '仅测到下界',
  latency_floor: '延迟地板超标', inconclusive: '证据不足',
};
export const SCHEDULE_LABEL: Record<string, string> = {
  starting: '启动中', busy: '被占用', needs_key: '缺少密钥', invalid: '配置无效',
  completed: '已完成', error: '失败', stopped: '已停止', running: '运行中',
};
export const KIND_LABEL: Record<string, string> = {
  capacity: '容量探测', speed: '速度测试', parallel: '并行测试', quality: '质量抽查', contract: '协议检查',
  load: '吞吐测试',
};

export const PROTOCOL_LABEL: Record<string, string> = {
  openai: 'Chat Completions',
  openai_responses: 'Responses',
  anthropic: 'Anthropic Messages',
  gemini: 'Gemini',
};

/** Compact protocol labels used by the console rows and Markdown export table. */
export const PROTOCOL_SHORT: Record<string, string> = {
  openai: 'Chat',
  openai_responses: 'Responses',
  anthropic: 'Anthropic',
  gemini: 'Gemini',
};

/** Four-valued verdicts: inconclusive/observation must never render as pass or fail. */
export const VERDICT_LABEL: Record<string, string> = {
  conformed: '符合',
  violated: '被改写',
  inconclusive: '无法判定',
  observation: '观察',
};

/** Export labels mirror the console: pass / fail / undetermined / soft observe. */
export const EXPORT_VERDICT_LABEL: Record<string, string> = {
  conformed: '通过',
  violated: '不通过',
  inconclusive: '无法判定',
  observation: '观察',
};

export const SEVERITY_LABEL: Record<string, string> = {
  high: '高', medium: '中', low: '低',
};

export const stateLabel = (status: string): string => STATE_LABEL[status] || status;

function contractExpected(item: ContractItem): string {
  return item.expected?.trim() || '—';
}

function contractObserved(item: ContractItem): string {
  return item.observed?.trim() || item.error?.trim() || '—';
}

function escapeCell(value: string): string {
  return value.replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');
}

function fenceBlock(language: string, body: string): string[] {
  const text = body.trim() || '（空）';
  // Avoid breaking the fence if the payload itself contains triple backticks.
  const fence = text.includes('```') ? '````' : '```';
  return [`${fence}${language}`, text, fence, ''];
}

/** Split backend multi-round dumps marked as `=== 第 N 轮请求/响应 ===`. */
function splitExportRounds(raw: string | undefined, kind: 'request' | 'response'): string[] {
  const text = raw?.trim() || '';
  if (!text) return [];
  const mark = kind === 'request' ? '请求' : '响应';
  const pattern = new RegExp(`(?:^|\\n)=== 第 \\d+ 轮${mark} ===\\n`);
  if (!pattern.test(`\n${text}`)) return [text];
  const parts = text
    .split(new RegExp(`(?:^|\\n)=== 第 \\d+ 轮${mark} ===\\n`))
    .map((part) => part.trim())
    .filter(Boolean);
  return parts.length ? parts : [text];
}

const PACK_LABEL: Record<string, string> = {
  standard: 'MMLU 精选',
  smoke: '内置冒烟',
};

/** Human-readable Markdown for quality smoke results. */
function qualityReportMarkdown(run: LabRun): string {
  const summary = run.quality;
  const pack = run.settings.pack ? (PACK_LABEL[run.settings.pack] || run.settings.pack) : '—';
  const lines = [
    '# 模型质量抽查报告',
    '',
    `- 实验：${run.settings.label}`,
    `- 状态：${stateLabel(run.status)}`,
    `- 时间：${dateTime(run.created_at)}`,
    `- 模型：${run.settings.model}`,
    `- 协议：${PROTOCOL_LABEL[run.settings.protocol] || run.settings.protocol}`,
    `- 地址：${run.settings.url}`,
    `- 题包：${pack}`,
    `- 运行 ID：${run.run_id}`,
    '',
    '## 汇总',
    '',
  ];
  if (summary) {
    lines.push(
      `- 准确率：${fmt(summary.accuracy)}%（${summary.passed}/${summary.total} 题通过）`,
      `- TTFT P50：${fmt(summary.ttft_p50_ms, 0)} ms`,
      `- 平均延迟：${fmt(summary.latency_avg_ms, 0)} ms`,
    );
    const levels = Object.entries(summary.by_level);
    if (levels.length) {
      lines.push('', '### 按难度', '');
      for (const [name, score] of levels) {
        lines.push(`- ${name}：${score.passed}/${score.total}`);
      }
    }
    const categories = Object.entries(summary.by_category);
    if (categories.length) {
      lines.push('', '### 按类别', '');
      for (const [name, score] of categories) {
        lines.push(`- ${name}：${score.passed}/${score.total}`);
      }
    }
  } else {
    lines.push('- 尚无汇总（运行可能未完成）');
  }
  if (run.message) {
    lines.push('', `说明：${run.message}`);
  }
  lines.push(
    '',
    '## 逐题一览',
    '',
    '| 序号 | 分类 / 难度（结论） | 来源 | 期望 | 回答 |',
    '| --- | --- | --- | --- | --- |',
  );
  for (const item of run.items) {
    const verdict = item.passed ? '通过' : '未通过';
    const media = item.media_count > 0 ? ` ·图${item.media_count}` : '';
    const answer = item.answer?.trim() || item.error?.trim() || '—';
    lines.push(
      `| ${String(item.index).padStart(2, '0')} | ${escapeCell(`${item.category}${media} / ${item.level}`)} · ${verdict} | ${escapeCell(item.source || '—')} | ${escapeCell(item.expected || '—')} | ${escapeCell(answer)} |`,
    );
  }

  lines.push('', '## 逐题明细', '');
  for (const item of run.items) {
    const verdict = item.passed ? '通过' : '未通过';
    const media = item.media_count > 0 ? ` · 图${item.media_count}` : '';
    lines.push(`### ${String(item.index).padStart(2, '0')} · ${item.category} / ${item.level}${media} — ${verdict}`, '');
    if (item.source) lines.push(`- 来源：${item.source}`);
    if (item.latency_ms != null) lines.push(`- 延迟：${fmt(item.latency_ms, 0)} ms`);
    if (item.ttft_ms != null) lines.push(`- TTFT：${fmt(item.ttft_ms, 0)} ms`);
    if (item.error?.trim()) lines.push(`- 错误：${item.error.trim()}`);
    lines.push('', '#### 题目');
    lines.push(...fenceBlock('text', item.prompt || '（空）'));
    lines.push('#### 期望');
    lines.push(...fenceBlock('text', item.expected || '（空）'));
    lines.push('#### 回答');
    lines.push(...fenceBlock('text', item.answer?.trim() || item.error?.trim() || '（空）'));
  }

  if (run.warnings.length) {
    lines.push('## 警告', '');
    for (const warning of run.warnings) lines.push(`- ${warning}`);
    lines.push('');
  }
  return lines.join('\n');
}

function contractProtocolLine(run: LabRun): string {
  const selected = run.settings.protocols?.length
    ? run.settings.protocols
    : [...new Set(run.contract.map((item) => item.protocol).filter(Boolean) as string[])];
  if (selected.length) {
    return selected.map((name) => PROTOCOL_LABEL[name] || name).join(' · ');
  }
  return PROTOCOL_LABEL[run.settings.protocol] || run.settings.protocol;
}

function contractProtocolShort(protocol?: string): string {
  if (!protocol) return '—';
  return PROTOCOL_SHORT[protocol] || PROTOCOL_LABEL[protocol] || protocol;
}

/** Prefer the short console label; strip legacy long backend titles for older runs. */
function contractCheckTitle(item: ContractItem): string {
  const title = item.title?.trim() || item.check;
  return title
    .replace(/（[^）]*）/g, '')
    .replace(/是否[^·—\-]*$/u, '')
    .replace(/是否被[^·—\-]*$/u, '')
    .trim() || title;
}

/** Human-readable Markdown with per-check request/response wire artifacts. */
function contractReportMarkdown(run: LabRun): string {
  const summary = run.contract_summary;
  const evidenced = summary ? summary.conformed + summary.violated : 0;
  const fidelity = summary && evidenced > 0 ? `${fmt(summary.fidelity)}%` : '—';
  const lines = [
    '# 协议检查报告',
    '',
    `- 实验：${run.settings.label}`,
    `- 状态：${stateLabel(run.status)}`,
    `- 时间：${dateTime(run.created_at)}`,
    `- 模型：${run.settings.model}`,
    `- 检查模式：${run.settings.mode === 'fast' ? '快速检查' : run.settings.mode === 'full' ? '全量检查' : '—'}`,
    `- 协议：${contractProtocolLine(run)}`,
    `- 地址：${run.settings.url}`,
    `- 运行 ID：${run.run_id}`,
    '',
    '## 汇总',
    '',
    `- 协议保真度：${fidelity}`,
  ];
  if (summary) {
    lines.push(
      `- 符合 ${summary.conformed} / 被改写 ${summary.violated} / 无法判定 ${summary.inconclusive}`
        + (summary.observation ? ` / 观察 ${summary.observation}` : ''),
      `- 高危违规：${summary.high_severity} 项`,
    );
  }
  if (run.message) {
    lines.push('', `说明：${run.message}`);
  }
  lines.push(
    '',
    '## 逐项一览',
    '',
    // Match the console table: protocol and verdict are their own columns.
    '| 序号 | 协议 | 检查项 | 应该返回 | 实际返回 | 结论 |',
    '| --- | --- | --- | --- | --- | --- |',
  );
  for (const item of run.contract) {
    const verdict = EXPORT_VERDICT_LABEL[item.verdict] || VERDICT_LABEL[item.verdict] || item.verdict;
    const observed = contractObserved(item);
    const note = item.detail?.trim() && item.detail.trim() !== observed ? `（${item.detail.trim()}）` : '';
    const protocol = contractProtocolShort(item.protocol);
    const title = contractCheckTitle(item);
    lines.push(
      `| ${String(item.index).padStart(2, '0')} | ${escapeCell(protocol)} | ${escapeCell(title)} | ${escapeCell(contractExpected(item))} | ${escapeCell(observed + note)} | ${escapeCell(verdict)} |`,
    );
  }

  lines.push('', '## 逐项明细（请求体 / 响应结果）', '');
  for (const item of run.contract) {
    const verdict = EXPORT_VERDICT_LABEL[item.verdict] || VERDICT_LABEL[item.verdict] || item.verdict;
    const observed = contractObserved(item);
    const protocol = contractProtocolShort(item.protocol);
    const title = contractCheckTitle(item);
    const heading = protocol !== '—'
      ? `### ${String(item.index).padStart(2, '0')} · [${protocol}] ${title} — ${verdict}`
      : `### ${String(item.index).padStart(2, '0')} · ${title} — ${verdict}`;
    lines.push(heading, '');
    if (protocol !== '—') lines.push(`- 协议：${protocol}`);
    lines.push(`- 检查 ID：\`${item.check}\``);
    lines.push(`- 应该返回：${contractExpected(item)}`);
    lines.push(`- 实际返回：${observed}`);
    if (item.detail?.trim()) lines.push(`- 判定说明：${item.detail.trim()}`);
    if (item.error?.trim()) lines.push(`- 错误：${item.error.trim()}`);
    if (item.requests) lines.push(`- 上游请求次数：${item.requests}`);
    if (item.latency_ms != null) lines.push(`- 延迟：${fmt(item.latency_ms, 0)} ms`);
    if (item.ttft_ms != null) lines.push(`- TTFT：${fmt(item.ttft_ms, 0)} ms`);
    const requestHeaders = item.request_headers ? Object.entries(item.request_headers) : [];
    if (requestHeaders.length) {
      lines.push(`- 请求头：${requestHeaders.map(([key, value]) => `${key}=${value}`).join(' · ')}`);
    }
    const headers = item.response_headers ? Object.entries(item.response_headers) : [];
    if (headers.length) {
      lines.push(`- 响应头：${headers.map(([key, value]) => `${key}=${value}`).join(' · ')}`);
    }
    const missing = item.missing_response_headers?.filter(Boolean) ?? [];
    if (missing.length) {
      lines.push(`- 缺失的响应头：${missing.join(' · ')}`);
    }
    const requestRounds = splitExportRounds(item.request_body, 'request');
    const responseRounds = splitExportRounds(item.response_body, 'response');
    if (requestRounds.length) {
      for (const [index, body] of requestRounds.entries()) {
        lines.push('', requestRounds.length > 1 ? `#### 请求体 · 第 ${index + 1} 轮` : '#### 请求体');
        lines.push(...fenceBlock('json', body));
      }
    } else {
      lines.push('', '#### 请求体', '', '（本次运行未记录请求体；请用更新后的后端重跑协议检查）', '');
    }
    const roundHeadersList = item.round_response_headers ?? [];
    if (responseRounds.length) {
      const keyPriority = [
        'msh-usage-cache-write-tokens-5m',
        'msh-usage-cache-write-tokens-1h',
        'x-request-id',
        'x-oneapi-request-id',
      ];
      for (const [index, body] of responseRounds.entries()) {
        lines.push('', responseRounds.length > 1 ? `#### 响应结果 · 第 ${index + 1} 轮` : '#### 响应结果');
        const roundHeaders = roundHeadersList[index];
        if (roundHeaders && Object.keys(roundHeaders).length > 0) {
          const ordered = [
            ...keyPriority.filter((k) => k in roundHeaders),
            ...Object.keys(roundHeaders).filter((k) => !keyPriority.includes(k)),
          ];
          lines.push(`- 关键响应头：${ordered.map((k) => `${k}=${roundHeaders[k]}`).join(' · ')}`);
        }
        const language = body.trimStart().startsWith('{')
          || body.trimStart().startsWith('[')
          || body.includes('\n{')
          ? 'json'
          : 'text';
        lines.push(...fenceBlock(language, body));
      }
    } else {
      lines.push('', '#### 响应结果', '', '（本次运行未记录响应原文；请用更新后的后端重跑协议检查）', '');
    }
  }

  if (run.warnings.length) {
    lines.push('## 警告', '');
    for (const warning of run.warnings) lines.push(`- ${warning}`);
    lines.push('');
  }
  return lines.join('\n');
}

function downloadBlob(filename: string, content: string, mime: string): void {
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export interface MatrixMarkdownRow {
  model: string;
  host: string;
  protocol: string;
  speed: string;
  parallel: string;
  capacity: string;
  quality: string;
  contract: string;
}

function markdownCell(value: string): string {
  return (value || '—').replace(/\|/g, '\\|').replace(/\r?\n/g, ' ');
}

/** Download the current overview matrix as a Markdown table. */
export function exportMatrixMarkdown(rows: MatrixMarkdownRow[], note: string): void {
  const header = '| 模型 | 站点 | 协议 | 速度（TTFT P50） | 并行（输出 TPM） | 容量（RPM） | 质量 | 协议保真度 |';
  const rule = '| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |';
  const body = rows.map((row) => [
    markdownCell(row.model),
    markdownCell(row.host),
    markdownCell(row.protocol),
    markdownCell(row.speed),
    markdownCell(row.parallel),
    markdownCell(row.capacity),
    markdownCell(row.quality),
    markdownCell(row.contract),
  ].join(' | '));
  const markdown = [
    '# 中转站 × 模型横评',
    '',
    note,
    '',
    header,
    rule,
    ...body.map((line) => `| ${line} |`),
    '',
  ].join('\n');
  const stamp = new Date().toISOString().slice(0, 10);
  downloadBlob(`relay-lab-matrix-${stamp}.md`, markdown, 'text/markdown;charset=utf-8');
}

export function exportRun(run: LabRun | undefined): void {
  if (!run) return;
  if (run.kind === 'contract') {
    downloadBlob(`${run.run_id}.md`, contractReportMarkdown(run), 'text/markdown;charset=utf-8');
    return;
  }
  if (run.kind === 'quality') {
    downloadBlob(`${run.run_id}.md`, qualityReportMarkdown(run), 'text/markdown;charset=utf-8');
    return;
  }
  downloadBlob(`${run.run_id}.json`, JSON.stringify(run, null, 2), 'application/json');
}

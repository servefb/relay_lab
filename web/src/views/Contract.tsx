import { Download } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';

import { api } from '../api';
import type { ContractModePreset, ContractProfilePreset } from '../api';
import { AiDiagnosePanel } from '../components/AiDiagnosePanel';
import { Card, ConnectionFields, ContractResultList, Field, Hint, Notes, RunFooter } from '../components/ui';
import { exportRun, fmt, stateLabel } from '../format';
import { useLab } from '../store';
import type { ContractSettings, Protocol } from '../types';

const DEFAULT_PROTOCOLS: Protocol[] = ['openai', 'openai_responses', 'anthropic'];

/** Fallback when /api/presets is unreachable — keep in sync with backend defaults. */
const FALLBACK_FAST_CHECKS: string[] = [
  'system', 'tools', 'tool_choice_auto', 'tool_choice_required', 'tool_choice_none',
  'dynamic_tools', 'dynamic_tools_mutex', 'json_schema', 'max_tokens', 'stop', 'multi_turn',
  'thinking', 'thinking_roundtrip', 'reasoning_effort', 'tool_roundtrip', 'errors', 'auth_missing',
  'stream', 'stream_usage', 'cache', 'cache_headers', 'vision_base64', 'fixed_sampling',
  'thinking_disable', 'response_shape', 'usage',
];

const FALLBACK_MODE_META: Record<'fast' | 'full', Omit<ContractModePreset, 'checks'>> = {
  fast: {
    label: '快速检查',
    description: '仅运行核心高信号协议项与主协议，跳过 15s 缓存等待',
  },
  full: {
    label: '全量检查',
    description: '覆盖全部 KVV 专项、多协议矩阵与缓存 TTL 闭环',
  },
};

type CheckDef = { id: string; title: string; desc: string; high: boolean };

/** Keep ids/order aligned with backend `CONTRACT_CHECKS` / `TITLES`. */
const CHECKS: CheckDef[] = [
  { id: 'system', title: 'system 指令', desc: 'system 压过用户指令；思考/正文不得出现未传入产品名；prompt_tokens 差分作辅证', high: false },
  { id: 'tools', title: '工具调用', desc: '恰好一次 lookup_ticket(T-1024)，参数/finish_reason/调用 ID 合规；Kimi 不传 tool_choice，GLM 仅 auto', high: true },
  { id: 'parallel_tools', title: '并行多工具', desc: '同轮并行返回 lookup_ticket + get_weather 两个结构化工具调用，防止仅在正文输出伪 tool_call', high: true },
  { id: 'stream_tools', title: '流式工具调用', desc: '流式 SSE 正常结束，且产生 1 次符合嵌套 JSON Schema 的工具调用；Chat / Responses / Anthropic', high: true },
  { id: 'tool_choice_auto', title: 'tool_choice=auto', desc: 'auto 下恰好一次 lookup_ticket(T-1024)', high: true },
  { id: 'tool_choice_required', title: 'tool_choice=required', desc: 'Chat/Anthropic 必调工具；Kimi Responses 官方不支持（预期 400，中转 200 记差异）', high: true },
  { id: 'tool_choice_none', title: 'tool_choice=none', desc: 'Chat/Anthropic 禁调工具；Kimi Responses 官方不支持（预期 400，中转 200 记差异）', high: true },
  { id: 'tool_choice_named', title: 'tool_choice 指定函数', desc: 'Kimi：思考开启时不兼容指定函数（Chat 官方支持但冲突），应 400；200 静默忽略记观察；Messages/Responses 未开放或同样冲突', high: false },
  { id: 'dynamic_tools', title: '动态工具', desc: 'Kimi Chat：system.tools（content 空）+ tool_choice=required 应 tool_calls', high: true },
  { id: 'dynamic_tools_both', title: '动态+顶层工具', desc: 'Kimi Chat：顶层 tools 与 system.tools 并存时 required 应调动态工具', high: true },
  { id: 'dynamic_tools_mutex', title: '动态工具互斥', desc: 'Kimi Chat：同条 system 同时 content+tools 应 400 且 error.type=invalid_request_error（文案差异仅告警）', high: true },
  { id: 'dynamic_tools_role', title: '动态工具角色', desc: 'Kimi Chat：user 消息携带 tools（非 system）应 HTTP 400', high: true },
  { id: 'dynamic_tools_dup', title: '动态工具重名', desc: 'Kimi Chat：顶层与 system.tools 同名应 400', high: true },
  { id: 'tool_choice_required_bare', title: 'required 无 tools', desc: '无 tools 却 tool_choice=required 应 HTTP 400', high: true },
  { id: 'json_schema', title: '结构化输出', desc: '严格符合约定 json_schema 字段与类型', high: true },
  { id: 'schema_errors', title: 'schema 错误', desc: 'Chat / Responses：非法 json_schema 应 400 且 error.type=invalid_request_error', high: false },
  { id: 'tool_schema_reject', title: 'tool schema 拒绝', desc: 'Kimi Chat：含无限递归 $ref 的工具 Schema 应 HTTP 400，不得 200 放行', high: true },
  { id: 'max_tokens', title: '输出上限', desc: 'max_tokens=24：completion_tokens 不得超限；finish_reason=length 为强证据', high: true },
  { id: 'stop', title: '停止序列', desc: '输出在 stop 序列处截断，其后内容不得继续出现', high: true },
  { id: 'multi_turn', title: '多轮上下文', desc: '多轮历史是否保留并可引用', high: false },
  { id: 'thinking', title: '思考块', desc: 'Chat / Responses / Anthropic：HTTP 200，思考内容与正文均保留', high: false },
  { id: 'preserved_thinking', title: '思考携带', desc: 'Chat / Anthropic：注入思考后续写 n×2；GLM/K3 丢思考判违规，其它偏差可记观察', high: true },
  { id: 'thinking_roundtrip', title: '思考续传', desc: 'Chat / Anthropic：完整回传首轮 assistant（含思考/签名），二轮正文等于首轮 n×2', high: true },
  { id: 'reasoning_effort', title: '思考强度', desc: 'Chat：reasoning_effort；Responses：reasoning.effort；Anthropic：thinking+effort 应开启思考', high: false },
  { id: 'reasoning_effort_invalid', title: '思考强度校验', desc: '恒思考 Kimi：Chat 官方返回 200 记观察；Responses 非法 reasoning.effort 必须返回 400', high: false },
  { id: 'tool_roundtrip', title: '工具回传', desc: '恰好 1 次工具调用，完整回传 assistant 与匹配 ID 的 tool 结果，正文仅输出随机 status', high: true },
  { id: 'tool_followup', title: '工具追问', desc: '回传 status 后追问 T-2048 应再次产生工具调用', high: true },
  { id: 'errors', title: '错误规范', desc: '无效 model：404/400/403 且 error.type 对照官方表；禁止客户端错映射 5xx', high: false },
  { id: 'auth_missing', title: '无鉴权 401', desc: '不带鉴权头须返回 401 且 error.type 对齐官方（禁止 new_api_error 等自定义类型）', high: false },
  { id: 'stream', title: '流式真实性', desc: '假流式（单块缓冲或间隔不足）应判违规', high: true },
  { id: 'stream_usage', title: '流式 usage', desc: '流式 200 且 usage 输出计数非空；Chat 需 stream_options.include_usage', high: false },
  { id: 'cache', title: '缓存命中', desc: 'Kimi 或 Anthropic：隔离前缀冷发 cached=0，复发 cached>0；无字段不凭耗时推断', high: false },
  { id: 'cache_headers', title: '缓存响应头', desc: 'Kimi：隔离长前缀冷发 write>0、read=0、Msh-Usage-Cache-Write-Tokens-1h>0（非流式）', high: false },
  { id: 'cache_headers_stream', title: '流式缓存响应头', desc: '同上，流式 1h', high: false },
  { id: 'cache_headers_5m', title: '5m 缓存响应头', desc: '同上，非流式 5m（Msh-Usage-Cache-Write-Tokens-5m）', high: false },
  { id: 'cache_headers_5m_stream', title: '流式 5m 缓存响应头', desc: '同上，流式 5m', high: false },
  { id: 'cache_roundtrip', title: '缓存闭环', desc: '1h 非流式写入通过后等待≥15s 原样重发，缓存读取>0', high: false },
  { id: 'cache_roundtrip_stream', title: '流式缓存闭环', desc: '同上，流式 1h', high: false },
  { id: 'cache_roundtrip_5m', title: '5m 缓存闭环', desc: '同上，非流式 5m', high: false },
  { id: 'cache_roundtrip_5m_stream', title: '流式 5m 缓存闭环', desc: '同上，流式 5m', high: false },
  { id: 'cache_tools_prefix', title: 'tools 前缀缓存', desc: 'Chat：大量 tools 定义的长前缀应产生 5m 缓存写入计数>0', high: false },
  { id: 'cache_tools_append', title: '追加 tools 仍命中', desc: 'Kimi Chat：写入后 messages 末尾追加 system.tools，缓存仍应命中', high: false },
  { id: 'cache_breakpoint', title: '缓存断点拒绝', desc: 'Kimi Chat：system content 含 prompt_cache_breakpoint 应 HTTP 400', high: false },
  { id: 'anthropic_cache_invalid', title: '非法缓存参数', desc: 'Anthropic Messages：与官方一致忽略非法 cache_control ttl/mode 并返回 200；网关 4xx 更严记观察', high: false },
  { id: 'cache_options_echo', title: '缓存选项回显', desc: 'Kimi Responses：显式传入 prompt_cache_options 时回显一致对象；若已正常写档且两档写头完整则未回显仅记观察（P2）', high: false },
  { id: 'vision_base64', title: '多模态 Base64', desc: 'Chat / Anthropic / Gemini：HTTP 200 且读出通行码 5827，证明图片被下发', high: true },
  { id: 'vision_url', title: '多模态 URL', desc: 'Chat：接受 http(s) image_url（200）或官方规范 4xx；不得 5xx', high: false },
  { id: 'vision_content_string', title: 'content 字符串', desc: 'Chat：接受序列化字符串 content（200）或官方规范 4xx；不得 5xx', high: false },
  { id: 'vision_video', title: '多模态视频', desc: 'Chat：接受 video_url data URL（200）或官方规范 4xx；不得 5xx', high: false },
  { id: 'vision_svg_reject', title: 'SVG 拒绝', desc: 'Chat / Responses：SVG 应 400/404/415/422，禁止 200 作答或 5xx 映射', high: false },
  { id: 'files_roundtrip', title: '文件 ID 闭环', desc: 'Chat / Responses：/v1/files 上传 → 查询元数据 → 模型通过 file_id 读取通行码 5827 → DELETE 清理', high: true },
  { id: 'fixed_sampling', title: '不可变采样', desc: '恒思考 Chat：thinking 开启时 temperature=0.6 应 4xx，禁止静默 200', high: false },
  { id: 'fixed_sampling_params', title: '采样参数矩阵', desc: '恒思考 Kimi Chat：top_p / presence_penalty / frequency_penalty / n 等应 4xx，禁止静默 200', high: false },
  { id: 'thinking_disable', title: 'thinking 关闭', desc: '恒思考 Chat：K2 允许 disabled→200 且无思考；K3 须 4xx 或 200 仍开启思考', high: false },
  { id: 'thinking_disable_enable', title: 'enable_thinking', desc: '非官方开关：enable_thinking=false 应 4xx，或 200 但思考仍开启', high: false },
  { id: 'thinking_disable_template', title: 'template 关思考', desc: '非官方开关：chat_template_kwargs 关闭思考应 4xx，或 200 但思考仍开启', high: false },
  { id: 'partial_mode', title: 'PartialMode', desc: 'Chat：半截 JSON 前缀续写恰好补全 2} 且 reasoning_content 为空', high: true },
  { id: 'partial_json_mix', title: 'Partial+JSON', desc: 'Chat：partial+json_object 应 200 续写合法 JSON（ok=true）且 reasoning_content 为空，或官方 4xx', high: false },
  { id: 'json_object', title: 'json_object', desc: 'Chat：response_format=json_object 透传；Kimi Responses 官方不支持（预期 400，中转 200 记差异）', high: true },
  { id: 'logprobs', title: 'logprobs', desc: 'Chat：200 须含 message.logprobs；4xx 记观察；禁止 5xx', high: false },
  { id: 'routing', title: '上游路由', desc: '同 host 连续请求上游版本标识是否一致', high: false },
  { id: 'response_shape', title: '响应结构', desc: 'HTTP 200 且响应结构合规；Responses 汇总全量 200 固定属性与 output[].id 的 resp_chat_ 转译痕迹', high: true },
  { id: 'usage', title: 'usage 计数', desc: '汇总全量 200 响应的必填计费字段（cache_write_tokens / thinking_tokens）完整性', high: true },
  { id: 'cache_write_headers_audit', title: '全量写档响应头', desc: 'Chat / Responses 汇总全量 200 写档响应头缺头数、两档求和不符数与正写档不明份数', high: true },
  { id: 'error_fields_audit', title: '4xx 错误码机器字段汇总', desc: '汇总全量 4xx 中非法 error.type、内部数字 code（400001）、param="" 与通用 MaaS 兜底文案占比', high: false },
];

const CHECK_BY_ID = Object.fromEntries(CHECKS.map((check) => [check.id, check])) as Record<string, CheckDef>;

/** Grouped for the picker; ids stay in backend canonical order within each group. */
const CHECK_GROUPS: { id: string; title: string; ids: string[] }[] = [
  { id: 'core', title: '基础与输出', ids: ['system', 'json_schema', 'json_object', 'partial_json_mix', 'schema_errors', 'max_tokens', 'stop', 'multi_turn', 'partial_mode', 'logprobs', 'response_shape', 'usage'] },
  { id: 'tools', title: '工具链路', ids: ['tools', 'parallel_tools', 'stream_tools', 'tool_choice_auto', 'tool_choice_required', 'tool_choice_none', 'tool_choice_named', 'dynamic_tools', 'dynamic_tools_both', 'dynamic_tools_mutex', 'dynamic_tools_role', 'dynamic_tools_dup', 'tool_choice_required_bare', 'tool_schema_reject', 'tool_roundtrip', 'tool_followup'] },
  { id: 'thinking', title: '思考链路', ids: ['thinking', 'preserved_thinking', 'thinking_roundtrip', 'reasoning_effort', 'reasoning_effort_invalid', 'fixed_sampling', 'fixed_sampling_params', 'thinking_disable', 'thinking_disable_enable', 'thinking_disable_template'] },
  { id: 'vision', title: '多模态', ids: ['vision_base64', 'vision_url', 'vision_content_string', 'vision_video', 'vision_svg_reject', 'files_roundtrip'] },
  { id: 'transport', title: '传输与错误', ids: ['errors', 'auth_missing', 'error_fields_audit', 'stream', 'stream_usage'] },
  { id: 'infra', title: '缓存与路由', ids: ['cache', 'cache_headers', 'cache_headers_stream', 'cache_headers_5m', 'cache_headers_5m_stream', 'cache_roundtrip', 'cache_roundtrip_stream', 'cache_roundtrip_5m', 'cache_roundtrip_5m_stream', 'cache_tools_prefix', 'cache_tools_append', 'cache_breakpoint', 'anthropic_cache_invalid', 'cache_options_echo', 'cache_write_headers_audit', 'routing'] },
];

/**
 * Fallback packs when /api/presets is unreachable.
 * Prefer live `contract_profiles` / `contract_modes` from the backend.
 */
const FALLBACK_FAMILY_CHECKS: Record<string, string[]> = {
  kimi: CHECKS.map((check) => check.id),
  glm: [
    'system', 'tools', 'parallel_tools', 'stream_tools', 'tool_choice_auto', 'tool_choice_required', 'tool_choice_none', 'tool_choice_named',
    'tool_choice_required_bare',
    'json_schema', 'schema_errors', 'tool_schema_reject', 'max_tokens', 'stop', 'multi_turn',
    'thinking', 'preserved_thinking', 'thinking_roundtrip', 'reasoning_effort',
    'tool_roundtrip', 'tool_followup', 'errors', 'auth_missing', 'stream', 'stream_usage',
    'vision_base64', 'vision_url', 'vision_content_string', 'vision_video', 'vision_svg_reject',
    'fixed_sampling', 'thinking_disable', 'thinking_disable_enable', 'thinking_disable_template',
    'partial_mode', 'partial_json_mix', 'json_object', 'logprobs', 'routing', 'response_shape', 'usage',
  ],
  generic: [
    'system', 'tools', 'parallel_tools', 'stream_tools', 'tool_choice_auto', 'tool_choice_required', 'tool_choice_none', 'tool_choice_named',
    'tool_choice_required_bare',
    'tool_roundtrip', 'tool_followup', 'json_schema', 'schema_errors', 'tool_schema_reject', 'max_tokens', 'stop', 'multi_turn',
    'errors', 'auth_missing', 'stream', 'stream_usage',
    'vision_base64', 'vision_url', 'vision_content_string', 'vision_video', 'vision_svg_reject',
    'partial_mode', 'partial_json_mix', 'json_object', 'logprobs', 'routing', 'response_shape', 'usage',
  ],
};

const FALLBACK_FAMILY_LABELS: Record<string, string> = {
  kimi: 'Kimi（Moonshot）',
  glm: '智谱 GLM',
  generic: '通用 OpenAI 兼容',
};

/** Mirror backend `resolve_profile` family key so the console preview matches the runner. */
function resolveFamilyKey(model: string): string {
  const name = model.trim().toLowerCase();
  if (!name) return 'generic';
  if (name.includes('kimi') || name.startsWith('moonshot') || name.startsWith('kimi-')) return 'kimi';
  if (name.includes('glm') || name.includes('chatglm') || name.startsWith('zhipu')) return 'glm';
  return 'generic';
}

/** Same rule as backend `select()`: mode check list ∩ family default_checks, mode order. */
function defaultsForMode(profileChecks: string[], modeChecks: string[]): string[] {
  const pack = new Set(profileChecks);
  return modeChecks.filter((id) => pack.has(id));
}

function primaryProtocol(protocol: Protocol): Protocol {
  return DEFAULT_PROTOCOLS.includes(protocol) ? protocol : 'openai';
}

const DEFAULTS: ContractSettings = {
  label: '',
  mode: 'full',
  checks: [],
  protocols: [...DEFAULT_PROTOCOLS],
  per_check: 1,
  max_parallel: 1,
  request_timeout: 60,
  cache_prefix_tokens: 2048,
};

export function Contract() {
  const { latest, startRun, basePayload, connection, pendingDraft, clearDraft, showNotice } = useLab();
  const [settings, setSettings] = useState<ContractSettings>(DEFAULTS);
  const [consent, setConsent] = useState(false);
  const [modes, setModes] = useState<Record<'fast' | 'full', ContractModePreset> | null>(null);
  const [profiles, setProfiles] = useState<Record<string, ContractProfilePreset> | null>(null);

  const run = latest('contract');
  const summary = run?.contract_summary ?? null;

  useEffect(() => {
    if (!pendingDraft || pendingDraft.kind !== 'contract') return;
    const s = pendingDraft.settings;
    setSettings((current) => {
      const next = { ...current };
      if (s.mode === 'fast' || s.mode === 'full') next.mode = s.mode;
      if (typeof s.label === 'string') next.label = s.label;
      if (Array.isArray(s.checks)) next.checks = s.checks.filter((item): item is string => typeof item === 'string');
      if (Array.isArray(s.protocols)) {
        next.protocols = s.protocols.filter((item): item is Protocol => (
          item === 'openai' || item === 'openai_responses' || item === 'anthropic' || item === 'gemini'
        ));
      }
      if (typeof s.per_check === 'number') next.per_check = s.per_check;
      if (typeof s.max_parallel === 'number') next.max_parallel = s.max_parallel;
      if (typeof s.request_timeout === 'number') next.request_timeout = s.request_timeout;
      if (typeof s.cache_prefix_tokens === 'number') next.cache_prefix_tokens = s.cache_prefix_tokens;
      return next;
    });
    clearDraft();
    showNotice('已填入 AI 推荐的协议检查参数，请确认后启动。');
  }, [pendingDraft, clearDraft, showNotice]);
  const family = resolveFamilyKey(connection.model);
  const profile = profiles?.[family];
  const profileLabel = profile?.label || FALLBACK_FAMILY_LABELS[family] || FALLBACK_FAMILY_LABELS.generic;
  const profileChecks = profile?.default_checks?.length
    ? profile.default_checks
    : (FALLBACK_FAMILY_CHECKS[family] ?? FALLBACK_FAMILY_CHECKS.generic);
  const modeKey = settings.mode;
  const modePreset = modes?.[modeKey];
  const modeLabel = modePreset?.label || FALLBACK_MODE_META[modeKey].label;
  const modeDesc = modePreset?.description || FALLBACK_MODE_META[modeKey].description;
  const modeChecks = modePreset?.checks?.length
    ? modePreset.checks
    : (modeKey === 'fast' ? FALLBACK_FAST_CHECKS : CHECKS.map((check) => check.id));
  const defaultIds = defaultsForMode(profileChecks, modeChecks);
  const defaultSet = new Set(defaultIds);
  const customizing = settings.checks.length > 0;
  const activeIds = customizing ? settings.checks : defaultIds;

  useEffect(() => {
    api.presets()
      .then((payload) => {
        if (payload.contract_modes) setModes(payload.contract_modes);
        if (payload.contract_profiles) setProfiles(payload.contract_profiles);
      })
      .catch(() => {});
  }, []);

  // Selecting another model family always re-applies that family's official pack.
  useEffect(() => {
    setSettings((current) => (current.checks.length ? { ...current, checks: [] } : current));
  }, [family]);

  const update = (patch: Partial<ContractSettings>) => setSettings((current) => ({ ...current, ...patch }));
  const chooseMode = (mode: 'fast' | 'full') => {
    update({
      mode,
      checks: [],
      protocols: mode === 'fast' ? [primaryProtocol(connection.protocol)] : [...DEFAULT_PROTOCOLS],
    });
  };
  const toggle = (id: string) => {
    const base = customizing ? settings.checks : defaultIds;
    update({
      checks: base.includes(id) ? base.filter((item) => item !== id) : [...base, id],
    });
  };
  const useOfficialDefault = () => update({ checks: [] });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const mode = settings.mode;
    void startRun('/api/contract', {
      ...basePayload(),
      ...settings,
      mode,
      // Empty checks → backend resolves by mode ∩ family default_checks.
      checks: customizing ? settings.checks : [],
      // Empty protocols → full: all three wires; fast: primary wire from connection.protocol.
      protocols: settings.protocols.length
        ? settings.protocols
        : (mode === 'fast' ? [] : DEFAULT_PROTOCOLS),
      label: settings.label.trim() || `协议检查 · ${modeLabel} · ${connection.model}`,
      authorized: consent,
    }, '协议检查');
  };

  return (
    <>
      <p className="view-sub">
        可选快速或全量检查；全量可多选 Chat Completions、Responses、Anthropic Messages。容量测快慢，这里测保真。
      </p>
      <div className="split">
        <form className="card form-card" onSubmit={submit}>
          <h2 className="card-title">连接模型</h2>
          <p className="card-desc">地址、Key 与模型在所有测试页之间共享；本页协议可多选，换模型后检查包会自动匹配。</p>
          <ConnectionFields
            multiProtocols={settings.protocols}
            onMultiProtocols={(protocols) => update({ protocols })}
          />
          <hr />
          <h2 className="card-title">检查模式</h2>
          <p className="card-desc">{modeDesc}</p>
          <div className="chips">
            {(['fast', 'full'] as const).map((id) => (
              <button
                key={id}
                type="button"
                aria-pressed={settings.mode === id}
                onClick={() => chooseMode(id)}
              >
                {modes?.[id]?.label || FALLBACK_MODE_META[id].label}
              </button>
            ))}
          </div>
          <h2 className="card-title">检查项</h2>
          <p className="card-desc">
            已按 <b>{profileLabel}</b> / <b>{modeLabel}</b> 自动匹配
            {connection.model ? <>（{connection.model}）</> : null}：
            {customizing
              ? <>已自选 <b>{settings.checks.length}</b> / {defaultIds.length} 项。</>
              : <><b>{defaultIds.length}</b> 项。</>}
            {customizing && (
              <>
                {' '}
                <button type="button" className="link" onClick={useOfficialDefault}>恢复自动匹配</button>
              </>
            )}
          </p>
          <details className="advanced">
            <summary>自定义检查项 <span>通常不用开</span></summary>
            <div className="contract-pick">
              {CHECK_GROUPS.map((group) => {
                const items = group.ids
                  .map((id) => CHECK_BY_ID[id])
                  .filter((check) => check && defaultSet.has(check.id));
                if (!items.length) return null;
                return (
                  <div key={group.id} className="contract-pick-group">
                    <div className="contract-pick-head">
                      {group.title}
                      <span>{items.filter((check) => activeIds.includes(check.id)).length}/{items.length}</span>
                    </div>
                    <div className="contract-opts">
                      {items.map((check) => {
                        const on = activeIds.includes(check.id);
                        return (
                          <label
                            key={check.id}
                            className="contract-opt"
                            data-on={on ? '1' : '0'}
                            title={check.desc}
                          >
                            <input type="checkbox" checked={on} onChange={() => toggle(check.id)} />
                            <span>{check.title}</span>
                          </label>
                        );
                      })}
                    </div>
                  </div>
                );
              })}
              <Hint>非本模型族的厂商专有项不会出现在默认包里；换模型后会重新自动匹配。</Hint>
            </div>
          </details>
          <details className="advanced">
            <summary>高级参数 <span>通常不用改</span></summary>
            <div className="adv-grid">
              <Field label="单请求超时（秒）">
                <input type="number" min={2} max={300} value={settings.request_timeout}
                  onChange={(event) => update({ request_timeout: Number(event.target.value) })} />
              </Field>
              <Field label="缓存前缀长度（token）">
                <input type="number" min={512} max={8192} step={256} value={settings.cache_prefix_tokens}
                  onChange={(event) => update({ cache_prefix_tokens: Number(event.target.value) })} />
              </Field>
              <Field label="每项重复次数">
                <input type="number" min={1} max={3} value={settings.per_check}
                  onChange={(event) => update({ per_check: Number(event.target.value) })} />
              </Field>
            </div>
            <Hint>缓存与缓存响应头会连发长前缀；路由检查会连发短请求。不配置长前缀可能测不出缓存命中。</Hint>
          </details>
          <RunFooter kind="contract" label="开始协议检查" consent={consent} onConsent={setConsent}
            consentText="我有权限测试此接口，知晓会产生少量请求费用。" />
        </form>

        <div className="stack">
          <Card title="最近一次检查" desc={run ? stateLabel(run.status) : '还没有记录。'}>
            {/*
              Fidelity is a ratio over *evidenced* checks. Rendering "0%" when nothing was
              evidenced reads as "everything was rewritten", which is the opposite of what
              an all-inconclusive run means, so the dash must be shown instead.
            */}
            {(() => {
              const evidenced = summary ? summary.conformed + summary.violated : 0;
              return <div className="score">{summary && evidenced > 0 ? `${fmt(summary.fidelity)}%` : '—'}</div>;
            })()}
            {summary && (
              <>
                <div className="bars">
                  <div className="bar-row">
                    <span>符合</span>
                    <div className="bar"><i style={{ width: `${summary.total ? (100 * summary.conformed) / summary.total : 0}%` }} /></div>
                    <b>{summary.conformed}/{summary.total}</b>
                  </div>
                  <div className="bar-row">
                    <span>被改写</span>
                    <div className="bar"><i style={{ width: `${summary.total ? (100 * summary.violated) / summary.total : 0}%` }} /></div>
                    <b>{summary.violated}</b>
                  </div>
                  <div className="bar-row">
                    <span>无法判定</span>
                    <div className="bar"><i style={{ width: `${summary.total ? (100 * summary.inconclusive) / summary.total : 0}%` }} /></div>
                    <b>{summary.inconclusive}</b>
                  </div>
                  {(summary.observation ?? 0) > 0 && (
                    <div className="bar-row">
                      <span>观察</span>
                      <div className="bar"><i style={{ width: `${summary.total ? (100 * summary.observation) / summary.total : 0}%` }} /></div>
                      <b>{summary.observation}</b>
                    </div>
                  )}
                </div>
                <Hint>
                  {summary.conformed + summary.violated > 0
                    ? `保真度只在“有证据”的项上计算（${summary.conformed + summary.violated} 项），无法判定与观察不计入分母`
                    : '本次没有任何一项取得硬证据，不能给出保真度；请先看下方每项说明'}
                  {summary.high_severity > 0 ? `；其中高危违规 ${summary.high_severity} 项` : ''}。
                </Hint>
              </>
            )}
            {run && !summary && <Hint>{run.message}</Hint>}
          </Card>
          <AiDiagnosePanel run={run} />
          <Card title="怎么读结论" desc="详细口径见「测试口径」页。">
            <Notes items={[
              '保真度只算有硬证据的项；「无法判定 / 观察」不进分母，也不等于造假。',
              '“应该返回”对照官方契约，“实际返回”是中转站响应；不一致才判被改写。',
              '本页测协议保真，不评价答题质量。',
            ]} />
          </Card>
        </div>
      </div>

      <Card title="逐项结果" desc={run ? run.message : '—'} className="table-card"
        actions={<button type="button" className="pill" disabled={!run} onClick={() => exportRun(run)}><Download size={13} />导出报告</button>}>
        <ContractResultList
          items={(run?.contract ?? []).map((item) => ({
            index: item.index,
            check: item.check,
            // Prefer the short picker label so the protocol badge + title do not fight for one line.
            title: CHECK_BY_ID[item.check]?.title || item.title,
            desc: CHECK_BY_ID[item.check]?.desc,
            verdict: item.verdict,
            expected: item.expected,
            observed: item.observed || item.error || '',
            detail: item.detail,
            protocol: item.protocol,
            request_headers: item.request_headers,
            response_headers: item.response_headers,
            request_body: item.request_body,
            response_body: item.response_body,
            missing_response_headers: item.missing_response_headers,
          }))}
        />
      </Card>
    </>
  );
}

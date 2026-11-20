export type Protocol = 'openai' | 'openai_responses' | 'anthropic' | 'gemini';
export type RunKind = 'capacity' | 'speed' | 'parallel' | 'quality' | 'contract' | 'load';
export type RunStatus = 'running' | 'completed' | 'stopping' | 'stopped' | 'error' | 'interrupted';
export type StageStatus = 'running' | 'passed' | 'failed' | 'unknown';
export type Goal = 'both' | 'rpm' | 'tpm';

export interface LabStage {
  index: number;
  phase: string;
  pressure: number;
  repeat: number;
  status: StageStatus;
  requests: number;
  succeeded: number;
  seconds: number;
  success_percent: number;
  partial_results?: boolean;
  window_rate_seconds?: number | null;
  wall_seconds?: number | null;
  token_source?: 'reported' | 'estimated' | 'missing' | 'unknown';
  window_requests?: number | null;
  window_succeeded?: number | null;
  window_success_percent?: number | null;
  rpm: number;
  input_tpm: number | null;
  output_tpm: number | null;
  total_tpm: number | null;
  ttft_p50_ms: number | null;
  /** P95 is the steadier capacity signal: on a short stage P99 is often just the max. */
  ttft_p95_ms: number | null;
  ttft_p99_ms: number | null;
  tpot_p50_ms: number | null;
  tpot_p95_ms: number | null;
  tpot_p99_ms: number | null;
  latency_avg_ms: number | null;
  latency_p50_ms: number | null;
  latency_p90_ms: number | null;
  latency_p95_ms: number | null;
  latency_p99_ms: number | null;
  /** Achieved concurrency by Little's Law, not the requested level. */
  mean_concurrency: number | null;
  peak_concurrency: number | null;
  mean_input_tokens: number | null;
  mean_output_tokens: number | null;
  reason: string;
  /** Why requests failed in this stage, counted from the per-request log. */
  failure_reasons: Record<string, number>;
}

export interface LabFinding {
  phase: 'rpm' | 'tpm';
  value: number | null;
  pressure: number | null;
  status: 'pending' | 'bounded' | 'lower_bound' | 'latency_floor' | 'inconclusive';
  verified: boolean;
  message: string;
}

export interface QualityItem {
  index: number;
  category: string;
  level: string;
  prompt: string;
  expected: string;
  source?: string;
  answer: string;
  passed: boolean;
  ttft_ms: number | null;
  latency_ms: number | null;
  error: string | null;
  media_count: number;
}

export interface QualitySummary {
  total: number;
  passed: number;
  accuracy: number;
  by_category: Record<string, { passed: number; total: number }>;
  by_level: Record<string, { passed: number; total: number }>;
  ttft_p50_ms?: number | null;
  latency_avg_ms: number | null;
  output_tps: number | null;
}

/**
 * Four-valued verdicts: `inconclusive` is missing evidence; `observation` is soft
 * evidence that must not dilute fidelity. Only hard rewrite evidence is `violated`.
 */
export type ContractVerdict = 'conformed' | 'violated' | 'inconclusive' | 'observation';
export type ContractSeverity = 'high' | 'medium' | 'low';

export interface ContractItem {
  index: number;
  check: string;
  title: string;
  prompt: string;
  expected: string;
  observed: string;
  verdict: ContractVerdict;
  severity: ContractSeverity;
  detail: string;
  /** Wire protocol for this row when the suite covers multiple protocols. */
  protocol?: string;
  latency_ms: number | null;
  ttft_ms: number | null;
  error: string | null;
  response_headers?: Record<string, string>;
  /** Per-round response headers aligned with each round in `response_body`. */
  round_response_headers?: Record<string, string>[];
  /** Outbound request headers with secrets redacted. */
  request_headers?: Record<string, string>;
  /** Pretty-printed wire request JSON captured for the export report. */
  request_body?: string;
  /** Pretty-printed wire response (or reconstructed summary) for the export report. */
  response_body?: string;
  /** Expected response-header names that were absent for header-checking probes. */
  missing_response_headers?: string[];
  /** How many upstream requests this probe spent (roundtrips may be >1). */
  requests?: number;
}

export interface CheckScore {
  conformed: number;
  violated: number;
  inconclusive: number;
  observation: number;
  total: number;
}

export interface ContractSummary {
  total: number;
  conformed: number;
  violated: number;
  inconclusive: number;
  /** Soft rows (cache miss / capability gap); excluded from fidelity. */
  observation: number;
  /** Share of *evidenced* checks that conformed; inconclusive/observation excluded. */
  fidelity: number;
  by_check: Record<string, CheckScore>;
  high_severity: number;
}

/** Union of every settings shape; fields only exist on the matching run kind. */
export interface RunSettings {
  label: string;
  url: string;
  model: string;
  protocol: Protocol;
  /** Contract multi-select; absent on other run kinds. */
  protocols?: Protocol[];
  /** Contract check mode: fast (core + primary wire) or full (complete matrix). */
  mode?: 'fast' | 'full';
  anthropic_version?: string;
  max_rpm?: number;
  max_requests?: number;
  max_parallel?: number;
  requests?: number;
  levels?: number[];
  stage_seconds?: number;
  start_rpm?: number;
  /** throughput mode only */
  concurrency_start?: number;
  concurrency_step?: number;
  window_seconds?: number;
  windows?: number;
  /** Throughput only. False means the whole body comes back in one response. */
  stream?: boolean;
  output_tokens?: number;
  success_percent?: number;
  /** quality / contract only */
  pack?: 'standard' | 'smoke';
  per_category?: number;
  per_check?: number;
  checks?: string[];
}

export interface LoadSummary {
  peak_concurrency: number | null;
  peak_rpm: number | null;
  suggested_rpm: number | null;
  success_total: number;
  request_total: number;
  windows: number;
  saturated: boolean;
  stop_reason: string | null;
}

/** Persisted AI diagnosis attached to a completed experiment. */
export interface AIAnalysis {
  summary_md: string;
  model: string;
  protocol: string;
  created_at: string;
}

export interface LabRun {
  run_id: string;
  created_at: string;
  kind: RunKind;
  settings: RunSettings;
  fingerprint: string;
  status: RunStatus;
  message: string;
  reserved_requests: number;
  reserved_tokens: number;
  stages: LabStage[];
  findings: LabFinding[];
  items: QualityItem[];
  quality: QualitySummary | null;
  load: LoadSummary | null;
  contract: ContractItem[];
  contract_summary: ContractSummary | null;
  warnings: string[];
  /** Optional AI diagnosis; older experiment.json files omit this. */
  ai_analysis?: AIAnalysis | null;
}

/** AI upstream wire; backend OpenAI SDK only supports these two. */
export type AIProtocol = 'openai' | 'openai_responses';

/** Public AI config (never includes the raw API key). */
export interface AIConfig {
  url: string;
  model: string;
  protocol: AIProtocol;
  timeout_seconds: number;
  has_key: boolean;
  configured: boolean;
  config_error?: string | null;
}

export interface AIConfigUpdate {
  url?: string;
  model?: string;
  protocol?: AIProtocol;
  timeout_seconds?: number;
  /** Omit to keep the previous key; null clears it. */
  api_key?: string | null;
}

export type DraftKind = 'capacity' | 'speed' | 'parallel' | 'quality' | 'contract' | 'load';

/** Validated parameter draft from the AI assistant. */
export interface ConfigDraft {
  kind: DraftKind;
  /** Target hash view, e.g. capacity / contract. */
  view: string;
  rationale: string;
  settings: Record<string, unknown>;
  highlight?: string[];
}

export interface AIChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface AIToolStep {
  id: string;
  name: string;
  label: string;
  status: 'running' | 'done' | 'error';
  detail?: string;
  ms?: number;
}

export interface Schedule {
  schedule_id: string;
  label: string;
  kind: 'capacity' | 'speed' | 'parallel' | 'quality' | 'contract';
  interval_minutes: number;
  created_at: string;
  next_run_at: string;
  enabled: boolean;
  needs_key: boolean;
  last_run_id: string | null;
  last_status: string | null;
  capacity: RunSettings | null;
  speed: RunSettings | null;
  parallel: RunSettings | null;
  quality: RunSettings | null;
  contract: RunSettings | null;
}

export interface CapacitySettings {
  label: string;
  goal: Goal;
  prompt_reuse: boolean;
  dataset_preset: string | null;
  input_tokens: number | null;
  stage_seconds: number;
  repeats: number;
  max_steps: number;
  refine_steps: number;
  start_rpm: number;
  max_rpm: number;
  max_parallel: number;
  max_tpm_parallel: number;
  max_requests: number;
  token_reserve: number;
  max_wall_seconds: number;
  request_timeout: number;
  cooldown_seconds: number;
  success_percent: number;
  ttft_p99_ms: number;
  tpot_p99_ms: number;
  capacity_ratio: number;
  short_output_tokens: number;
  long_output_tokens: number;
  seed: number;
}

export interface ContractSettings {
  label: string;
  /** fast = core probes on primary wire; full = complete KVV + multi-protocol. */
  mode: 'fast' | 'full';
  checks: string[];
  /** Subset of Chat / Responses / Anthropic; empty → all three (full) or primary wire (fast). */
  protocols: Protocol[];
  per_check: number;
  max_parallel: number;
  request_timeout: number;
  cache_prefix_tokens: number;
}

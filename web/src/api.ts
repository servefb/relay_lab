import type { AIAnalysis, AIChatMessage, AIConfig, AIConfigUpdate, ConfigDraft, LabRun, Schedule } from './types';

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers as Record<string, string> | undefined) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = (payload as { error?: string }).error || `请求失败（${response.status}）`;
    if (response.status === 401 && !path.startsWith('/api/auth/')) {
      window.dispatchEvent(new Event('relay-lab:unauthorized'));
    }
    throw new ApiError(message, response.status);
  }
  return payload as T;
}

const post = <T>(path: string, body: unknown, signal?: AbortSignal): Promise<T> =>
  request<T>(path, { method: 'POST', body: JSON.stringify(body), signal });

export interface DatasetPreset {
  key: string;
  name: string;
  input_tokens: number | null;
  note: string;
}

export interface ContractModePreset {
  label: string;
  description: string;
  checks: string[];
}

export interface ContractProfilePreset {
  family: string;
  label: string;
  default_checks: string[];
  supports_dynamic_tools: boolean;
  supports_preserved_thinking: boolean;
  default_temperature: number;
  allows_thinking_disable: boolean;
}

export interface PresetsResponse {
  datasets: DatasetPreset[];
  contract_modes?: Record<'fast' | 'full', ContractModePreset>;
  contract_profiles?: Record<string, ContractProfilePreset>;
  contract_profile_examples?: Record<string, string>;
}

export interface AuthUser {
  user: string;
}

export type SseHandler = (event: string, data: unknown) => void;

/**
 * Read a text/event-stream response. Dispatches named SSE events; unnamed events
 * fall back to `message`. Aborts cleanly when `signal` fires or the reader ends.
 */
export async function streamSse(
  path: string,
  body: unknown,
  onEvent: SseHandler,
  signal?: AbortSignal,
): Promise<void> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    const message = (payload as { error?: string }).error || `请求失败（${response.status}）`;
    if (response.status === 401) {
      window.dispatchEvent(new Event('relay-lab:unauthorized'));
    }
    throw new ApiError(message, response.status);
  }
  if (!response.body) {
    throw new ApiError('浏览器不支持流式响应', response.status);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let eventName = 'message';
  let dataLines: string[] = [];

  const flush = () => {
    if (!dataLines.length) {
      eventName = 'message';
      return;
    }
    const raw = dataLines.join('\n');
    dataLines = [];
    const name = eventName;
    eventName = 'message';
    let parsed: unknown = raw;
    try {
      parsed = JSON.parse(raw);
    } catch {
      // keep raw string for plain-text deltas
    }
    onEvent(name, parsed);
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline = buffer.indexOf('\n');
    while (newline >= 0) {
      let line = buffer.slice(0, newline);
      buffer = buffer.slice(newline + 1);
      if (line.endsWith('\r')) line = line.slice(0, -1);
      if (!line) {
        flush();
      } else if (line.startsWith('event:')) {
        eventName = line.slice(6).trim() || 'message';
      } else if (line.startsWith('data:')) {
        dataLines.push(line.slice(5).replace(/^ /, ''));
      }
      newline = buffer.indexOf('\n');
    }
  }
  buffer += decoder.decode();
  if (buffer.trim()) {
    const leftover = buffer.replace(/\r$/, '');
    if (leftover.startsWith('event:')) eventName = leftover.slice(6).trim() || 'message';
    else if (leftover.startsWith('data:')) dataLines.push(leftover.slice(5).replace(/^ /, ''));
  }
  flush();
}

export const api = {
  me: () => request<AuthUser>('/api/auth/me'),
  login: (username: string, password: string) =>
    post<{ ok: boolean; user: string }>('/api/auth/login', { username, password }),
  logout: () => post<{ ok: boolean }>('/api/auth/logout', {}),
  runs: () => request<{ runs: LabRun[] }>('/api/runs'),
  presets: () => request<PresetsResponse>('/api/presets'),
  schedules: () => request<{ schedules: Schedule[] }>('/api/schedules'),
  models: (url: string, apiKey: string, signal: AbortSignal) =>
    post<{ models: string[] }>('/api/models', { url, api_key: apiKey || undefined }, signal),
  start: (path: string, body: unknown) => post<LabRun>(path, body),
  startContract: (body: unknown) => post<LabRun>('/api/contract', body),
  stop: (runId: string) => post<LabRun>(`/api/runs/${runId}/stop`, {}),
  createSchedule: (body: unknown) => post<Schedule>('/api/schedules', body),
  runSchedule: (id: string, apiKey?: string) => post<Schedule>(`/api/schedules/${id}/run`, { api_key: apiKey || undefined }),
  toggleSchedule: (id: string, enabled: boolean) => post<Schedule>(`/api/schedules/${id}/toggle`, { enabled }),
  deleteSchedule: (id: string) => request<unknown>(`/api/schedules/${id}`, { method: 'DELETE' }),

  aiConfig: () => request<AIConfig>('/api/ai/config'),
  updateAiConfig: (body: AIConfigUpdate) =>
    request<AIConfig>('/api/ai/config', { method: 'PUT', body: JSON.stringify(body) }),
  testAiConfig: () => post<{ ok: boolean; model?: string; protocol?: string; message?: string }>('/api/ai/test', {}),
  analyzeRun: (
    runId: string,
    body: { force?: boolean; focus?: string },
    onEvent: SseHandler,
    signal?: AbortSignal,
  ) => streamSse(`/api/runs/${runId}/analyze`, body, onEvent, signal),
  aiChat: (
    body: { messages: AIChatMessage[]; active_run_id?: string; active_view?: string },
    onEvent: SseHandler,
    signal?: AbortSignal,
  ) => streamSse('/api/ai/chat', body, onEvent, signal),
};

export type { AIAnalysis, ConfigDraft };

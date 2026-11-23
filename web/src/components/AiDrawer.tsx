import {
  Check, ChevronDown, ChevronRight, CircleCheck, Eraser, GitCompareArrows,
  History as HistoryIcon, MessageSquarePlus, Orbit, RefreshCw, Search, Settings2,
  ShieldAlert, SlidersHorizontal, Square, Trash2, X,
} from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ComponentType, FormEvent, KeyboardEvent } from 'react';

import { ApiError, api } from '../api';
import { useLab } from '../store';
import type { AIChatMessage, AIToolStep, ConfigDraft } from '../types';
import { Markdown } from './Markdown';
import { Hint } from './ui';

const MODEL_CACHE_KEY = 'relay-lab-ai-model-cache';

interface ChatTurn {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  tools?: AIToolStep[];
  drafts?: ConfigDraft[];
  error?: string;
  streaming?: boolean;
}

interface ChatSession {
  id: string;
  title: string;
  updatedAt: number;
  turns: ChatTurn[];
}

const STORAGE_KEY = 'relay-lab-ai-chat-sessions';
const MAX_SESSIONS = 40;

/** Single-run diagnosis stays on result pages; drawer focuses on cross-run questions. */
const SUGGESTIONS: {
  label: string;
  prompt: string;
  icon: ComponentType<{ size?: number; strokeWidth?: number }>;
}[] = [
  {
    label: '对比最近两次同类型实验',
    prompt: '对比最近两次同类型实验的核心指标，指出差异与可信度变化。',
    icon: GitCompareArrows,
  },
  {
    label: '推荐一套压测或协议检查参数',
    prompt: '根据历史表现，帮我推荐一套稳妥的压测或协议检查参数。',
    icon: SlidersHorizontal,
  },
  {
    label: '最近有哪些高危协议违规？',
    prompt: '最近有没有高危协议违规记录？列出关键检查项。',
    icon: ShieldAlert,
  },
];

function newId(prefix: string) {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`;
}

function titleFromTurns(turns: ChatTurn[]): string {
  const first = turns.find((turn) => turn.role === 'user' && turn.content.trim());
  if (!first) return '新对话';
  const text = first.content.trim().replace(/\s+/g, ' ');
  return text.length > 36 ? `${text.slice(0, 36)}…` : text;
}

function loadSessions(): ChatSession[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as ChatSession[];
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item) => item && typeof item.id === 'string' && Array.isArray(item.turns))
      .map((item) => ({
        id: item.id,
        title: item.title || titleFromTurns(item.turns) || '对话',
        updatedAt: typeof item.updatedAt === 'number' ? item.updatedAt : Date.now(),
        turns: item.turns.map((turn) => ({ ...turn, streaming: false })),
      }))
      .sort((a, b) => b.updatedAt - a.updatedAt)
      .slice(0, MAX_SESSIONS);
  } catch {
    return [];
  }
}

function persistSessions(sessions: ChatSession[]) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(sessions.slice(0, MAX_SESSIONS)));
  } catch {
    /* quota / private mode — ignore */
  }
}

function loadModelCache(url: string): string[] {
  try {
    const raw = localStorage.getItem(MODEL_CACHE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as { url?: string; models?: string[] };
    if (!parsed || parsed.url !== url || !Array.isArray(parsed.models)) return [];
    return parsed.models.filter((item): item is string => typeof item === 'string' && Boolean(item));
  } catch {
    return [];
  }
}

function saveModelCache(url: string, models: string[]) {
  try {
    localStorage.setItem(MODEL_CACHE_KEY, JSON.stringify({ url, models: models.slice(0, 200) }));
  } catch {
    /* ignore */
  }
}

function uniqueModels(...groups: (string | undefined | null)[][]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const group of groups) {
    for (const item of group) {
      const id = (item || '').trim();
      if (!id || seen.has(id)) continue;
      seen.add(id);
      out.push(id);
    }
  }
  return out;
}

function upsertSession(sessions: ChatSession[], session: ChatSession): ChatSession[] {
  const without = sessions.filter((item) => item.id !== session.id);
  if (!session.turns.length) return without;
  return [session, ...without].sort((a, b) => b.updatedAt - a.updatedAt).slice(0, MAX_SESSIONS);
}

function startOfDay(offsetDays = 0): number {
  const date = new Date();
  date.setHours(0, 0, 0, 0);
  date.setDate(date.getDate() + offsetDays);
  return date.getTime();
}

function groupSessions(sessions: ChatSession[]): { label: string; items: ChatSession[] }[] {
  const today = startOfDay(0);
  const yesterday = startOfDay(-1);
  const buckets: { label: string; items: ChatSession[] }[] = [
    { label: '今天', items: [] },
    { label: '昨天', items: [] },
    { label: '更早', items: [] },
  ];
  for (const session of sessions) {
    if (session.updatedAt >= today) buckets[0].items.push(session);
    else if (session.updatedAt >= yesterday) buckets[1].items.push(session);
    else buckets[2].items.push(session);
  }
  return buckets.filter((bucket) => bucket.items.length > 0);
}

function draftDiff(settings: Record<string, unknown>, highlight?: string[]): { key: string; value: string }[] {
  const keys = highlight?.length ? highlight : Object.keys(settings);
  return keys
    .filter((key) => settings[key] !== undefined)
    .slice(0, 12)
    .map((key) => {
      const value = settings[key];
      const text = Array.isArray(value) || (value && typeof value === 'object')
        ? JSON.stringify(value)
        : String(value);
      return { key, value: text };
    });
}

function ToolSteps({ steps }: { steps: AIToolStep[] }) {
  const [open, setOpen] = useState(true);
  if (!steps.length) return null;
  return (
    <div className="ai-tools">
      <button type="button" className="ai-tools-toggle" onClick={() => setOpen((v) => !v)}>
        {open ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        工具调用 {steps.length}
      </button>
      {open && (
        <ul>
          {steps.map((step) => (
            <li key={step.id} data-status={step.status}>
              <span>{step.label || step.name}</span>
              <small>
                {step.status === 'running' ? '进行中' : step.status === 'error' ? '失败' : '完成'}
                {step.ms != null ? ` · ${step.ms} ms` : ''}
                {step.detail ? ` · ${step.detail}` : ''}
              </small>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DraftCard({ draft }: { draft: ConfigDraft }) {
  const { applyDraft } = useLab();
  const rows = draftDiff(draft.settings, draft.highlight);
  return (
    <div className="ai-draft">
      <header>
        <strong>推荐参数 · {draft.kind}</strong>
        <small>{draft.rationale || '经后端校验的调参草稿'}</small>
      </header>
      {rows.length > 0 && (
        <dl>
          {rows.map((row) => (
            <div key={row.key}>
              <dt>{row.key}</dt>
              <dd>{row.value}</dd>
            </div>
          ))}
        </dl>
      )}
      <button type="button" className="btn primary" onClick={() => applyDraft(draft)}>
        一键填入对应实验页
      </button>
    </div>
  );
}

function ModelSwitcher({
  current,
  options,
  disabled,
  loading,
  saving,
  onSelect,
  onRefresh,
}: {
  current: string;
  options: string[];
  disabled?: boolean;
  loading?: boolean;
  saving?: boolean;
  onSelect: (model: string) => void;
  onRefresh: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const root = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setOpen(false);
    };
    document.addEventListener('mousedown', onDoc);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onDoc);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  useEffect(() => {
    if (!open) {
      setQuery('');
      return;
    }
    const id = window.setTimeout(() => searchRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [open]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter((id) => id.toLowerCase().includes(q));
  }, [options, query]);

  const label = current || (options.length ? '选择模型' : '暂无模型');

  return (
    <div className={`ai-model-switch ${open ? 'is-open' : ''}`} ref={root}>
      <button
        type="button"
        className="ai-model-trigger"
        disabled={disabled || saving}
        aria-haspopup="listbox"
        aria-expanded={open}
        title={current || '切换模型'}
        onClick={() => {
          setOpen((v) => {
            const next = !v;
            if (next && options.length <= 1) onRefresh();
            return next;
          });
        }}
      >
        <span>{saving ? '切换中…' : label}</span>
        <ChevronDown size={13} strokeWidth={2} />
      </button>
      {open && (
        <div className="ai-model-menu" role="listbox" aria-label="切换模型">
          <div className="ai-model-search">
            <input
              ref={searchRef}
              type="text"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="搜索模型…"
              aria-label="搜索模型"
            />
          </div>
          <div className="ai-model-menu-list">
            {filtered.length ? filtered.map((id) => {
              const active = id === current;
              return (
                <button
                  key={id}
                  type="button"
                  role="option"
                  aria-selected={active}
                  className={`ai-model-option ${active ? 'active' : ''}`}
                  onClick={() => {
                    setOpen(false);
                    if (!active) onSelect(id);
                  }}
                >
                  <span>{id}</span>
                  {active ? <Check size={14} strokeWidth={2.2} /> : null}
                </button>
              );
            }) : (
              <p className="ai-model-empty">
                {options.length ? '没有匹配的模型' : '还没有模型列表，点下方刷新'}
              </p>
            )}
          </div>
          <div className="ai-model-menu-foot">
            <button
              type="button"
              className="ai-model-refresh-row"
              disabled={loading}
              onClick={() => { void onRefresh(); }}
            >
              <RefreshCw size={13} className={loading ? 'spin' : undefined} />
              {loading ? '刷新中…' : '刷新模型列表'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function AiDrawer() {
  const {
    drawerOpen, setDrawerOpen, openAiConfig, aiConfig, setAiConfig, ensureAiConfig,
    connection, selectedId, pendingPrompt, clearPendingPrompt, showAlert, showNotice,
  } = useLab();
  const [sessionId, setSessionId] = useState(() => newId('chat'));
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [showHistory, setShowHistory] = useState(false);
  const [historyQuery, setHistoryQuery] = useState('');
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [loadingModels, setLoadingModels] = useState(false);
  const [savingModel, setSavingModel] = useState(false);
  const abortRef = useRef<AbortController | null>(null);
  const modelsAbort = useRef<AbortController | null>(null);
  const scroller = useRef<HTMLDivElement | null>(null);
  const historyRoot = useRef<HTMLDivElement | null>(null);
  const historySearch = useRef<HTMLInputElement | null>(null);
  const turnsRef = useRef(turns);
  const sessionIdRef = useRef(sessionId);
  const sessionsRef = useRef(sessions);
  const [storageReady, setStorageReady] = useState(false);
  turnsRef.current = turns;
  sessionIdRef.current = sessionId;
  sessionsRef.current = sessions;

  const view = typeof window !== 'undefined' ? window.location.hash.replace('#', '') || 'overview' : 'overview';

  const snapshotCurrent = useCallback((): ChatSession | null => {
    const currentTurns = turnsRef.current;
    if (!currentTurns.length) return null;
    return {
      id: sessionIdRef.current,
      title: titleFromTurns(currentTurns),
      updatedAt: Date.now(),
      turns: currentTurns.map((turn) => ({ ...turn, streaming: false })),
    };
  }, []);

  const saveCurrentInto = useCallback((list: ChatSession[]) => {
    const snap = snapshotCurrent();
    return snap ? upsertSession(list, snap) : list.filter((item) => item.id !== sessionIdRef.current);
  }, [snapshotCurrent]);

  const send = useCallback(async (raw: string) => {
    const content = raw.trim();
    if (!content || busy) return;

    const config = await ensureAiConfig();
    if (!config?.configured || !config.has_key) {
      openAiConfig();
      showAlert('请先配置 AI 模型与密钥');
      return;
    }

    setShowHistory(false);

    const history: AIChatMessage[] = [
      ...turnsRef.current
        .filter((turn) => turn.role === 'user' || (turn.role === 'assistant' && turn.content))
        .map((turn) => ({ role: turn.role, content: turn.content })),
      { role: 'user', content },
    ];

    const userTurn: ChatTurn = { id: `u-${Date.now()}`, role: 'user', content };
    const assistantId = `a-${Date.now()}`;
    setTurns((current) => [
      ...current,
      userTurn,
      { id: assistantId, role: 'assistant', content: '', tools: [], drafts: [], streaming: true },
    ]);
    setInput('');
    setBusy(true);

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    let assembled = '';
    const tools: AIToolStep[] = [];
    const drafts: ConfigDraft[] = [];

    const patchAssistant = (patch: Partial<ChatTurn>) => {
      setTurns((current) => current.map((turn) => (
        turn.id === assistantId ? { ...turn, ...patch, tools: [...tools], drafts: [...drafts] } : turn
      )));
    };

    try {
      await api.aiChat(
        {
          messages: history,
          active_run_id: selectedId || undefined,
          active_view: view || undefined,
        },
        (event, data) => {
          const payload = (data && typeof data === 'object') ? data as Record<string, unknown> : {};
          if (event === 'delta') {
            const chunk = typeof payload.text === 'string'
              ? payload.text
              : typeof data === 'string' ? data : '';
            if (chunk) {
              assembled += chunk;
              patchAssistant({ content: assembled });
            }
            return;
          }
          if (event === 'tool_start') {
            const id = String(payload.id || payload.name || `t-${tools.length}`);
            tools.push({
              id,
              name: String(payload.name || 'tool'),
              label: String(payload.label || payload.name || '工具调用'),
              status: 'running',
              detail: typeof payload.detail === 'string' ? payload.detail : undefined,
            });
            patchAssistant({});
            return;
          }
          if (event === 'tool_end') {
            const id = String(payload.id || '');
            const idx = tools.findIndex((step) => step.id === id || step.name === payload.name);
            const target = idx >= 0 ? tools[idx] : tools[tools.length - 1];
            if (target) {
              target.status = payload.error ? 'error' : 'done';
              if (typeof payload.ms === 'number') target.ms = payload.ms;
              if (typeof payload.detail === 'string') target.detail = payload.detail;
              if (payload.error) target.detail = String(payload.error);
            }
            patchAssistant({});
            return;
          }
          if (event === 'config_draft') {
            const draft = payload as unknown as ConfigDraft;
            if (draft?.kind && draft.settings) drafts.push(draft);
            patchAssistant({});
            return;
          }
          if (event === 'error') {
            patchAssistant({
              error: String(payload.message || payload.error || '对话失败'),
              streaming: false,
            });
          }
          if (event === 'done') {
            if (typeof payload.content === 'string' && payload.content) assembled = payload.content;
            patchAssistant({ content: assembled, streaming: false });
          }
        },
        controller.signal,
      );
      patchAssistant({ content: assembled, streaming: false });
    } catch (err) {
      if ((err as Error).name === 'AbortError') {
        patchAssistant({ content: assembled || '（已停止）', streaming: false });
      } else {
        const message = err instanceof ApiError ? err.message : (err as Error).message;
        patchAssistant({ error: message || '对话失败', streaming: false });
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      setBusy(false);
    }
  }, [busy, ensureAiConfig, openAiConfig, selectedId, showAlert, view]);

  useEffect(() => {
    const stored = loadSessions();
    setSessions(stored);
    if (stored[0]?.turns.length) {
      setSessionId(stored[0].id);
      setTurns(stored[0].turns);
    }
    setStorageReady(true);
  }, []);

  useEffect(() => {
    if (!storageReady || busy) return;
    const next = saveCurrentInto(sessionsRef.current);
    setSessions(next);
    persistSessions(next);
  }, [turns, busy, saveCurrentInto, storageReady]);

  useEffect(() => {
    if (!drawerOpen) return;
    void ensureAiConfig();
  }, [drawerOpen, ensureAiConfig]);

  useEffect(() => {
    if (!drawerOpen) return;
    const url = aiConfig?.url || connection.url || '';
    setModelOptions(uniqueModels(
      [aiConfig?.model],
      connection.models,
      url ? loadModelCache(url) : [],
    ));
  }, [drawerOpen, aiConfig?.url, aiConfig?.model, connection.models, connection.url]);

  const modelChoices = useMemo(() => uniqueModels(
    [aiConfig?.model],
    modelOptions,
    connection.models,
  ), [aiConfig?.model, modelOptions, connection.models]);

  const refreshModels = useCallback(async () => {
    const url = (aiConfig?.url || connection.url || '').trim();
    const key = connection.apiKey.trim();
    if (!url) {
      openAiConfig();
      showAlert('请先配置 AI API 地址');
      return;
    }
    if (!key) {
      showAlert('刷新模型需要实验连接里的 API Key（已保存的 AI 密钥不会回传前端）');
      return;
    }
    const controller = new AbortController();
    modelsAbort.current?.abort();
    modelsAbort.current = controller;
    setLoadingModels(true);
    try {
      const payload = await api.models(url, key, controller.signal);
      if (modelsAbort.current !== controller) return;
      const next = uniqueModels([aiConfig?.model], payload.models);
      setModelOptions(next);
      saveModelCache(url, next);
      showNotice(next.length ? `已获取 ${next.length} 个模型` : '未返回可用模型');
    } catch (err) {
      if (modelsAbort.current !== controller) return;
      if ((err as Error).name === 'AbortError') return;
      const message = err instanceof ApiError ? err.message : (err as Error).message;
      showAlert(message || '模型列表获取失败');
    } finally {
      if (modelsAbort.current === controller) {
        modelsAbort.current = null;
        setLoadingModels(false);
      }
    }
  }, [aiConfig?.model, aiConfig?.url, connection.apiKey, connection.url, openAiConfig, showAlert, showNotice]);

  const changeModel = useCallback(async (model: string) => {
    const nextModel = model.trim();
    if (!nextModel || nextModel === aiConfig?.model || savingModel) return;
    setSavingModel(true);
    try {
      const next = await api.updateAiConfig({ model: nextModel });
      setAiConfig(next);
      setModelOptions((current) => uniqueModels([nextModel], current));
      if (next.url) saveModelCache(next.url, uniqueModels([nextModel], modelOptions));
      showNotice(`已切换模型 · ${nextModel}`);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : (err as Error).message;
      showAlert(message || '切换模型失败');
    } finally {
      setSavingModel(false);
    }
  }, [aiConfig?.model, modelOptions, savingModel, setAiConfig, showAlert, showNotice]);

  useEffect(() => {
    if (!drawerOpen || !pendingPrompt) return;
    const prompt = pendingPrompt;
    clearPendingPrompt();
    void send(prompt);
  }, [drawerOpen, pendingPrompt, clearPendingPrompt, send]);

  useEffect(() => {
    if (!drawerOpen || showHistory) return;
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight, behavior: 'smooth' });
  }, [turns, busy, drawerOpen, showHistory]);

  useEffect(() => {
    if (!showHistory) {
      setHistoryQuery('');
      return;
    }
    const focusId = window.setTimeout(() => historySearch.current?.focus(), 0);
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === 'Escape') setShowHistory(false);
    };
    document.addEventListener('keydown', onKey);
    return () => {
      window.clearTimeout(focusId);
      document.removeEventListener('keydown', onKey);
    };
  }, [showHistory]);

  useEffect(() => () => {
    abortRef.current?.abort();
    modelsAbort.current?.abort();
  }, []);

  const historyRows = useMemo(() => {
    const snap = snapshotCurrent();
    const merged = snap ? upsertSession(sessions, snap) : sessions;
    const q = historyQuery.trim().toLowerCase();
    const safe = merged.filter((session) => session && typeof session.id === 'string');
    if (!q) return safe;
    return safe.filter((session) => (session.title || '').toLowerCase().includes(q));
  }, [historyQuery, sessions, snapshotCurrent, turns]);

  const historyGroups = useMemo(() => groupSessions(historyRows), [historyRows]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setBusy(false);
  }, []);

  const startNewChat = useCallback(() => {
    if (busy) stop();
    const next = saveCurrentInto(sessionsRef.current);
    setSessions(next);
    persistSessions(next);
    setSessionId(newId('chat'));
    setTurns([]);
    setInput('');
    setShowHistory(false);
  }, [busy, saveCurrentInto, stop]);

  const openSession = useCallback((session: ChatSession) => {
    if (session.id === sessionIdRef.current) {
      setShowHistory(false);
      return;
    }
    if (busy) stop();
    const next = saveCurrentInto(sessionsRef.current);
    setSessions(next);
    persistSessions(next);
    setSessionId(session.id);
    setTurns(session.turns.map((turn) => ({ ...turn, streaming: false })));
    setInput('');
    setShowHistory(false);
  }, [busy, saveCurrentInto, stop]);

  const removeSession = useCallback((id: string) => {
    const next = sessionsRef.current.filter((item) => item.id !== id);
    setSessions(next);
    persistSessions(next);
    if (id === sessionIdRef.current) {
      if (busy) stop();
      setSessionId(newId('chat'));
      setTurns([]);
      setInput('');
    }
  }, [busy, stop]);

  const clearCurrent = useCallback(() => {
    if (busy) stop();
    const next = sessionsRef.current.filter((item) => item.id !== sessionIdRef.current);
    setSessions(next);
    persistSessions(next);
    setTurns([]);
    setInput('');
  }, [busy, stop]);

  if (!drawerOpen) return null;

  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    void send(input);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      void send(input);
    }
  };

  const historyPanel = (
    <div className="ai-history-panel" ref={historyRoot}>
      <div className="ai-history-search">
        <Search size={14} strokeWidth={2} />
        <input
          ref={historySearch}
          className="ai-history-search-input"
          type="search"
          value={historyQuery}
          onChange={(e) => setHistoryQuery(e.target.value)}
          placeholder="搜索对话…"
          aria-label="搜索对话"
        />
      </div>
      <div className="ai-history-scroll">
        {historyGroups.length ? historyGroups.map((group) => (
          <div key={group.label} className="ai-history-group">
            <div className="ai-history-label">{group.label}</div>
            <ul>
              {group.items.map((session) => {
                const active = session.id === sessionId;
                return (
                  <li key={session.id} className={active ? 'active' : ''}>
                    <button
                      type="button"
                      className="ai-history-main"
                      onClick={() => openSession(session)}
                    >
                      <span className="ai-history-ico" aria-hidden>
                        {active
                          ? <CircleCheck size={15} strokeWidth={1.8} />
                          : <Orbit size={15} strokeWidth={1.8} />}
                      </span>
                      <span className="ai-history-title">{session.title || '新对话'}</span>
                    </button>
                    <button
                      type="button"
                      className="icon-btn ai-history-delete"
                      title="删除"
                      aria-label="删除对话"
                      onClick={() => removeSession(session.id)}
                    >
                      <Trash2 size={13} />
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        )) : (
          <p className="ai-history-empty">
            {historyQuery.trim() ? '没有匹配的对话' : '还没有历史对话'}
          </p>
        )}
      </div>
      <div className="ai-history-foot">
        <button type="button" onClick={startNewChat}>
          <MessageSquarePlus size={14} strokeWidth={1.8} />
          新对话
        </button>
      </div>
    </div>
  );

  return (
    <>
      <div className="ai-drawer-backdrop" onClick={() => setDrawerOpen(false)} aria-hidden />
      <aside className="ai-drawer" role="dialog" aria-label="Orbit">
        <header className="ai-drawer-head">
          <div>
            <strong><Orbit size={16} /> Orbit</strong>
            <small>
              {!aiConfig?.configured
                ? '未配置'
                : !aiConfig.has_key
                  ? '缺密钥'
                  : aiConfig.model
                    ? '已就绪'
                    : '未选模型'}
            </small>
          </div>
          <div className="ai-drawer-actions">
            <button
              type="button"
              className={`icon-btn ${showHistory ? 'active' : ''}`}
              title="历史对话"
              aria-label="历史对话"
              aria-pressed={showHistory}
              onClick={() => setShowHistory((v) => !v)}
            >
              <HistoryIcon size={16} />
            </button>
            <button type="button" className="icon-btn" title="新对话" aria-label="新对话" onClick={startNewChat}>
              <MessageSquarePlus size={16} />
            </button>
            <button type="button" className="icon-btn" title="模型设置" aria-label="模型设置" onClick={openAiConfig}>
              <Settings2 size={16} />
            </button>
            <button
              type="button"
              className="icon-btn"
              title="清空当前对话"
              aria-label="清空当前对话"
              onClick={clearCurrent}
            >
              <Eraser size={16} />
            </button>
            <button type="button" className="icon-btn" title="关闭" aria-label="关闭" onClick={() => setDrawerOpen(false)}>
              <X size={16} />
            </button>
          </div>
        </header>

        {showHistory ? historyPanel : (
          <div className={`ai-messages ${turns.length ? '' : 'is-empty'}`} ref={scroller}>
            {!turns.length ? (
              <div className="ai-empty">
                <h3 className="ai-empty-hello">接下来做什么？</h3>
                <div className="ai-suggest">
                  {SUGGESTIONS.map((item) => {
                    const Icon = item.icon;
                    return (
                      <button
                        key={item.label}
                        type="button"
                        disabled={busy}
                        onClick={() => void send(item.prompt)}
                      >
                        <span className="ai-suggest-ico" aria-hidden>
                          <Icon size={15} strokeWidth={1.7} />
                        </span>
                        <span>{item.label}</span>
                      </button>
                    );
                  })}
                </div>
              </div>
            ) : turns.map((turn) => (
              <div key={turn.id} className={`ai-bubble ${turn.role}`}>
                {turn.role === 'assistant' && turn.tools && turn.tools.length > 0 && (
                  <ToolSteps steps={turn.tools} />
                )}
                {turn.content ? <Markdown source={turn.content} /> : (
                  turn.streaming ? <p className="ai-typing">思考中…</p> : null
                )}
                {turn.drafts?.map((draft, idx) => <DraftCard key={`${turn.id}-d-${idx}`} draft={draft} />)}
                {turn.error && <Hint error>{turn.error}</Hint>}
              </div>
            ))}
          </div>
        )}

        {!showHistory && (
          <form className={`ai-composer ${turns.length ? '' : 'is-welcome'}`} onSubmit={onSubmit}>
            <div className="ai-composer-box">
              <textarea
                rows={2}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="想问什么都可以…"
                disabled={busy}
              />
              <div className="ai-composer-bar">
                <div className="ai-composer-meta">
                  <ModelSwitcher
                    current={aiConfig?.model || ''}
                    options={modelChoices}
                    disabled={busy}
                    loading={loadingModels}
                    saving={savingModel}
                    onSelect={(model) => { void changeModel(model); }}
                    onRefresh={() => { void refreshModels(); }}
                  />
                </div>
                {busy ? (
                  <button type="button" className="pill" onClick={stop}><Square size={13} />停止</button>
                ) : (
                  <button type="submit" className="ai-send" disabled={!input.trim()} aria-label="发送">
                    <Orbit size={15} strokeWidth={1.8} />
                  </button>
                )}
              </div>
            </div>
          </form>
        )}
      </aside>
    </>
  );
}

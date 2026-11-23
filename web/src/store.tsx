import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { ApiError, api } from './api';
import type { AIConfig, ConfigDraft, LabRun, Protocol, RunKind, Schedule } from './types';

export interface Connection {
  url: string;
  protocol: Protocol;
  anthropicVersion: string;
  apiKey: string;
  model: string;
  models: string[];
  loading: boolean;
  error: string;
  hint: string;
}

interface LabContextValue {
  runs: LabRun[];
  schedules: Schedule[];
  connection: Connection;
  busy: boolean;
  active: LabRun | undefined;
  selectedId: string;
  alert: string;
  notice: string;
  /** True when a model is picked and nothing else is running. */
  ready: boolean;
  aiConfig: AIConfig | null;
  drawerOpen: boolean;
  configModalOpen: boolean;
  pendingDraft: ConfigDraft | null;
  pendingPrompt: string | null;
  latest: (kind: RunKind) => LabRun | undefined;
  setSelectedId: (id: string) => void;
  setUrl: (url: string) => void;
  setProtocol: (protocol: Protocol) => void;
  setAnthropicVersion: (version: string) => void;
  setApiKey: (key: string) => void;
  setModel: (model: string) => void;
  /** Fill site, protocol and model together so a matrix cell can jump into a test. */
  prefillConnection: (next: { url: string; protocol: Protocol; model: string }) => void;
  fetchModels: () => Promise<void>;
  /** Connection fields every run endpoint accepts. */
  basePayload: () => {
    url: string;
    model: string;
    protocol: Protocol;
    anthropic_version?: string;
    api_key: string | undefined;
  };
  startRun: (path: string, body: object, label: string) => Promise<void>;
  stopActive: () => Promise<void>;
  loadRuns: () => Promise<void>;
  loadSchedules: () => Promise<void>;
  showAlert: (message: string) => void;
  clearAlert: () => void;
  showNotice: (message: string) => void;
  setAiConfig: (config: AIConfig | null) => void;
  ensureAiConfig: () => Promise<AIConfig | null>;
  setDrawerOpen: (open: boolean) => void;
  openAiDrawer: (prompt?: string, runId?: string) => void;
  openAiConfig: () => void;
  closeAiConfig: () => void;
  applyDraft: (draft: ConfigDraft) => void;
  clearDraft: () => void;
  clearPendingPrompt: () => void;
}

const DEFAULT_HINT = '填写地址和 Key，然后点击“获取模型”。';

const LabContext = createContext<LabContextValue | null>(null);

export function LabProvider({ children }: { children: ReactNode }) {
  const [runs, setRuns] = useState<LabRun[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [busy, setBusy] = useState(false);
  const [selectedId, setSelectedId] = useState('');
  const [alert, setAlert] = useState('');
  const [notice, setNotice] = useState('');
  const [aiConfig, setAiConfig] = useState<AIConfig | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [configModalOpen, setConfigModalOpen] = useState(false);
  const [pendingDraft, setPendingDraft] = useState<ConfigDraft | null>(null);
  const [pendingPrompt, setPendingPrompt] = useState<string | null>(null);
  const [connection, setConnection] = useState<Connection>({
    url: '', protocol: 'openai', anthropicVersion: '2023-06-01', apiKey: '', model: '',
    models: [], loading: false, error: '', hint: DEFAULT_HINT,
  });
  const discovery = useRef<AbortController | null>(null);
  const aiConfigLoaded = useRef(false);

  const active = runs.find((run) => run.status === 'running' || run.status === 'stopping');
  const ready = !busy && !active && !connection.loading && connection.models.includes(connection.model);

  const loadRuns = useCallback(async () => {
    const payload = await api.runs();
    setRuns(payload.runs);
    setSelectedId((current) => current || payload.runs[0]?.run_id || '');
  }, []);

  const loadSchedules = useCallback(async () => {
    const payload = await api.schedules();
    setSchedules(payload.schedules);
  }, []);

  useEffect(() => {
    let closed = false;
    let socket: WebSocket | null = null;
    let retries = 0;
    let retryTimer: number | null = null;

    const connect = () => {
      if (closed) return;
      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const ws = new WebSocket(`${scheme}://${window.location.host}/api/ws`);
      socket = ws;

      ws.onopen = () => {
        retries = 0;
        setAlert('');
      };
      ws.onmessage = (event) => {
        let payload: { type?: string; runs?: LabRun[]; run?: LabRun };
        try {
          payload = JSON.parse(String(event.data));
        } catch {
          return;
        }
        if (payload.type === 'snapshot' && payload.runs) {
          const snapshot = payload.runs;
          setRuns(snapshot);
          setSelectedId((current) => current || snapshot[0]?.run_id || '');
          return;
        }
        if (payload.type === 'run' && payload.run) {
          const incoming = payload.run;
          setRuns((current) => {
            const next = [incoming, ...current.filter((item) => item.run_id !== incoming.run_id)];
            next.sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
            return next.slice(0, 50);
          });
          setSelectedId((current) => current || incoming.run_id);
        }
      };
      ws.onclose = (event) => {
        socket = null;
        if (closed) return;
        // 4401 = session missing/expired. Keep reconnecting would loop forever after a
        // backend restart that rotated the JWT secret.
        if (event.code === 4401) {
          setAlert('登录已过期，请重新登录');
          window.dispatchEvent(new Event('relay-lab:unauthorized'));
          return;
        }
        // The server may restart mid-experiment; reconnect with a capped backoff and
        // fetch a snapshot instead of pretending the last frame is still current.
        setAlert('与实验服务的连接已断开，正在自动重连…');
        const delay = Math.min(8000, 500 * 2 ** retries);
        retries += 1;
        // After several failed attempts, check whether the HTTP session is gone so we
        // stop spinning when the close code was swallowed as 1006. Network errors mean
        // the backend is still down — keep reconnecting.
        if (retries === 3 || retries === 6) {
          api.me().catch((error: unknown) => {
            if (closed) return;
            if (!(error instanceof ApiError) || error.status !== 401) return;
            if (retryTimer !== null) {
              window.clearTimeout(retryTimer);
              retryTimer = null;
            }
            setAlert('登录已过期，请重新登录');
            window.dispatchEvent(new Event('relay-lab:unauthorized'));
          });
        }
        retryTimer = window.setTimeout(connect, delay);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    loadSchedules().catch(() => {});

    const onVisible = () => {
      if (document.hidden) return;
      // A backgrounded tab can have its socket killed without a close event firing
      // promptly; force a fresh connection and one HTTP read when it comes back.
      if (!socket) {
        if (retryTimer !== null) {
          window.clearTimeout(retryTimer);
          retryTimer = null;
        }
        retries = 0;
        connect();
      }
      loadRuns().catch(() => {});
    };
    document.addEventListener('visibilitychange', onVisible);

    return () => {
      closed = true;
      document.removeEventListener('visibilitychange', onVisible);
      if (retryTimer !== null) window.clearTimeout(retryTimer);
      socket?.close();
    };
  }, [loadRuns, loadSchedules]);

  const invalidate = useCallback((patch: Partial<Connection>) => {
    discovery.current?.abort();
    discovery.current = null;
    setConnection((current) => ({
      ...current, ...patch, models: [], model: '', loading: false, error: '', hint: '连接信息已变化，请重新获取模型。',
    }));
  }, []);

  const prefillConnection = useCallback((next: { url: string; protocol: Protocol; model: string }) => {
    discovery.current?.abort();
    discovery.current = null;
    setConnection((current) => ({
      ...current,
      url: next.url.trim(),
      protocol: next.protocol,
      model: next.model,
      models: next.model ? [next.model] : [],
      loading: false,
      error: '',
      hint: current.apiKey
        ? '已带入站点、协议和模型，确认后即可开测。'
        : '已带入站点、协议和模型。密钥仍需填写后才能开测。',
    }));
  }, []);

  const fetchModels = useCallback(async () => {
    const controller = new AbortController();
    discovery.current?.abort();
    discovery.current = controller;
    const { url, apiKey } = connection;
    setConnection((current) => ({ ...current, loading: true, error: '', hint: '正在读取模型列表，不会开始压测…' }));
    try {
      const payload = await api.models(url.trim(), apiKey, controller.signal);
      if (discovery.current !== controller) return;
      setConnection((current) => ({
        ...current,
        models: payload.models,
        hint: payload.models.length
          ? `已获取 ${payload.models.length} 个模型，请从下拉框选择。`
          : '该 Key 没有返回可用模型，请检查权限或联系站点管理员。',
      }));
    } catch (error) {
      if (discovery.current !== controller) return;
      setConnection((current) => ({
        ...current, models: [], error: (error as Error).message || '模型列表获取失败，请稍后重试。',
      }));
    } finally {
      if (discovery.current === controller) {
        discovery.current = null;
        setConnection((current) => ({ ...current, loading: false }));
      }
    }
  }, [connection]);

  const basePayload = useCallback(() => ({
    url: connection.url,
    model: connection.model,
    protocol: connection.protocol,
    // Only Anthropic reads this; sending it elsewhere would be rejected as an unknown field.
    anthropic_version: connection.protocol === 'anthropic' ? connection.anthropicVersion : undefined,
    api_key: connection.apiKey || undefined,
  }), [connection]);

  const startRun = useCallback(async (path: string, body: object, label: string) => {
    setBusy(true);
    setAlert('');
    try {
      const run = await api.start(path, body);
      setRuns((current) => [run, ...current.filter((item) => item.run_id !== run.run_id)]);
      setSelectedId(run.run_id);
      setConnection((current) => ({ ...current, apiKey: '' }));
      setNotice(`${label}已启动：${run.settings.label}。密钥已从页面清除。`);
    } catch (error) {
      setAlert((error as Error).message || '启动失败');
    } finally {
      setBusy(false);
    }
  }, []);

  const stopActive = useCallback(async () => {
    if (!active) return;
    setBusy(true);
    try {
      await api.stop(active.run_id);
      await loadRuns();
    } catch (error) {
      setAlert((error as Error).message || '停止失败，请重试');
    } finally {
      setBusy(false);
    }
  }, [active, loadRuns]);

  const ensureAiConfig = useCallback(async () => {
    if (aiConfigLoaded.current && aiConfig) return aiConfig;
    try {
      const next = await api.aiConfig();
      aiConfigLoaded.current = true;
      setAiConfig(next);
      return next;
    } catch {
      aiConfigLoaded.current = true;
      return aiConfig;
    }
  }, [aiConfig]);

  const openAiDrawer = useCallback((prompt?: string, runId?: string) => {
    if (runId) setSelectedId(runId);
    if (prompt) setPendingPrompt(prompt);
    setDrawerOpen(true);
  }, []);

  const applyDraft = useCallback((draft: ConfigDraft) => {
    setPendingDraft(draft);
    setDrawerOpen(false);
    const view = draft.view || (draft.kind === 'load' ? 'capacity' : draft.kind);
    if (window.location.hash !== `#${view}`) {
      window.location.hash = `#${view}`;
    }
  }, []);

  const value = useMemo<LabContextValue>(() => ({
    runs,
    schedules,
    connection,
    busy,
    active,
    selectedId,
    alert,
    notice,
    ready,
    aiConfig,
    drawerOpen,
    configModalOpen,
    pendingDraft,
    pendingPrompt,
    latest: (kind) => runs.find((run) => run.kind === kind),
    setSelectedId,
    setUrl: (url) => invalidate({ url }),
    setProtocol: (protocol) => invalidate({ protocol }),
    setAnthropicVersion: (anthropicVersion) => setConnection((current) => ({ ...current, anthropicVersion })),
    setApiKey: (apiKey) => invalidate({ apiKey }),
    setModel: (model) => setConnection((current) => ({ ...current, model })),
    prefillConnection,
    fetchModels,
    basePayload,
    startRun,
    stopActive,
    loadRuns,
    loadSchedules,
    showAlert: setAlert,
    clearAlert: () => setAlert(''),
    showNotice: setNotice,
    setAiConfig: (config) => {
      aiConfigLoaded.current = true;
      setAiConfig(config);
    },
    ensureAiConfig,
    setDrawerOpen,
    openAiDrawer,
    openAiConfig: () => setConfigModalOpen(true),
    closeAiConfig: () => setConfigModalOpen(false),
    applyDraft,
    clearDraft: () => setPendingDraft(null),
    clearPendingPrompt: () => setPendingPrompt(null),
  }), [runs, schedules, connection, busy, active, selectedId, alert, notice, ready, aiConfig,
    drawerOpen, configModalOpen, pendingDraft, pendingPrompt, invalidate, prefillConnection,
    fetchModels, basePayload, startRun, stopActive, loadRuns, loadSchedules, ensureAiConfig,
    openAiDrawer, applyDraft]);

  return <LabContext.Provider value={value}>{children}</LabContext.Provider>;
}

export function useLab(): LabContextValue {
  const context = useContext(LabContext);
  if (!context) throw new Error('useLab must be used inside LabProvider');
  return context;
}

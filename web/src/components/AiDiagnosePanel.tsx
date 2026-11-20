import { MessageSquare, Orbit, RefreshCw, Square } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import { ApiError, api } from '../api';
import { dateTime } from '../format';
import { useLab } from '../store';
import type { AIAnalysis, LabRun } from '../types';
import { Markdown } from './Markdown';
import { Card, Hint } from './ui';

function analysisFromDone(payload: Record<string, unknown>, fallback: string, aiConfigModel?: string, aiConfigProtocol?: string): AIAnalysis {
  const nested = payload.analysis;
  const source = (nested && typeof nested === 'object') ? nested as Record<string, unknown> : payload;
  const summary = typeof source.summary_md === 'string' && source.summary_md
    ? source.summary_md
    : fallback;
  return {
    summary_md: summary,
    model: String(source.model || aiConfigModel || ''),
    protocol: String(source.protocol || aiConfigProtocol || ''),
    created_at: String(source.created_at || new Date().toISOString()),
  };
}

function isFinished(run: LabRun): boolean {
  return run.status === 'completed' || run.status === 'stopped' || run.status === 'error' || run.status === 'interrupted';
}

export function AiDiagnosePanel({ run, embedded = false }: { run?: LabRun | null; embedded?: boolean }) {
  const {
    aiConfig, ensureAiConfig, openAiDrawer, openAiConfig, showAlert,
  } = useLab();
  const ready = Boolean(run && isFinished(run));
  const cached = ready ? (run?.ai_analysis ?? null) : null;
  const [text, setText] = useState(cached?.summary_md || '');
  const [meta, setMeta] = useState(cached);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState('');
  const abortRef = useRef<AbortController | null>(null);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    setText(cached?.summary_md || '');
    setMeta(cached);
    setError('');
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
  }, [run?.run_id, cached?.summary_md, cached?.created_at, ready]);

  useEffect(() => () => { abortRef.current?.abort(); }, []);

  useEffect(() => {
    if (!streaming) return;
    const node = bodyRef.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [text, streaming]);

  const ensureReady = async () => {
    const config = await ensureAiConfig();
    if (!config?.configured || !config.has_key) {
      openAiConfig();
      showAlert('请先配置 AI 模型与密钥');
      return false;
    }
    return true;
  };

  const analyze = async (force: boolean) => {
    if (!run || !ready) return;
    if (!(await ensureReady())) return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setStreaming(true);
    setError('');
    if (force || !text) setText('');
    let assembled = force || !text ? '' : text;
    try {
      await api.analyzeRun(
        run.run_id,
        { force },
        (event, data) => {
          const payload = (data && typeof data === 'object') ? data as Record<string, unknown> : {};
          if (event === 'delta') {
            const chunk = typeof payload.text === 'string'
              ? payload.text
              : typeof data === 'string' ? data : '';
            if (chunk) {
              assembled += chunk;
              setText(assembled);
            }
            return;
          }
          if (event === 'done') {
            const next = analysisFromDone(payload, assembled, aiConfig?.model, aiConfig?.protocol);
            setText(next.summary_md);
            setMeta(next);
            return;
          }
          if (event === 'error') {
            setError(String(payload.message || payload.error || '解读失败'));
          }
        },
        controller.signal,
      );
    } catch (err) {
      if ((err as Error).name === 'AbortError') return;
      const message = err instanceof ApiError ? err.message : (err as Error).message;
      setError(message || '解读失败');
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
        setStreaming(false);
      }
    }
  };

  const stop = () => {
    abortRef.current?.abort();
    abortRef.current = null;
    setStreaming(false);
  };

  const askFollowUp = () => {
    if (!run) return;
    openAiDrawer(`请继续解读实验 ${run.run_id}（${run.settings.label}）的报告，结合异常指标给出可执行排查建议。`, run.run_id);
  };

  const actions = (
    <div className="ai-diagnose-actions">
      {streaming ? (
        <button type="button" className="pill" onClick={stop}><Square size={13} />停止</button>
      ) : (
        <button
          type="button"
          className="pill"
          disabled={!ready}
          onClick={() => void analyze(Boolean(text))}
        >
          {text ? <RefreshCw size={13} /> : <Orbit size={13} />}
          {text ? '重新解读' : '开始解读'}
        </button>
      )}
      {text && !streaming && ready && (
        <button type="button" className="pill" onClick={askFollowUp}>
          <MessageSquare size={13} />在 Orbit 中追问
        </button>
      )}
    </div>
  );

  const hasContent = Boolean(text) || streaming || Boolean(error);
  const body = hasContent ? (
    <div ref={bodyRef} className={`ai-diagnose-body ${streaming ? 'streaming' : ''}`}>
      {error && <Hint error>{error}</Hint>}
      {text ? (
        <Markdown source={text} />
      ) : streaming ? (
        <p className="ai-typing">正在生成解读…</p>
      ) : null}
    </div>
  ) : null;

  const idleHint = ready ? '用当前报告生成结论与排查建议。' : '跑完一次实验后再解读。';

  if (embedded) {
    return (
      <section className={`ai-diagnose is-embedded ${hasContent ? '' : 'is-idle'}`}>
        <header className="ai-diagnose-head">
          <div>
            <strong><Orbit size={15} /> AI 智能解读</strong>
            <small>
              {meta?.created_at
                ? `${meta.model || '模型未记'} · ${dateTime(meta.created_at)}`
                : idleHint}
            </small>
          </div>
          {actions}
        </header>
        {body}
      </section>
    );
  }

  return (
    <Card
      className={`ai-diagnose-card ${hasContent ? '' : 'is-idle'}`}
      title={<span className="ai-diagnose-title"><Orbit size={15} /> AI 智能解读</span>}
      desc={meta?.created_at ? `${meta.model || '模型未记'} · ${dateTime(meta.created_at)}` : idleHint}
      actions={actions}
    >
      {body}
    </Card>
  );
}

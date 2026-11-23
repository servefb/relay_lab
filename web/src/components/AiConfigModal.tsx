import { LoaderCircle, Plug, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';

import { ApiError, api } from '../api';
import { useLab } from '../store';
import type { AIProtocol } from '../types';
import { Hint, SecretInput, Select } from './ui';

function asAiProtocol(value: string): AIProtocol {
  return value === 'openai_responses' ? 'openai_responses' : 'openai';
}

export function AiConfigModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { connection, aiConfig, setAiConfig, showNotice, showAlert } = useLab();
  const [url, setUrl] = useState('');
  const [protocol, setProtocol] = useState<AIProtocol>('openai');
  const [apiKey, setApiKey] = useState('');
  const [clearKey, setClearKey] = useState(false);
  const [reuseModel, setReuseModel] = useState('');
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [hint, setHint] = useState('');
  const [error, setError] = useState('');

  useEffect(() => {
    if (!open) return;
    setUrl(aiConfig?.url || '');
    setProtocol(asAiProtocol(aiConfig?.protocol || 'openai'));
    setApiKey('');
    setClearKey(false);
    setReuseModel('');
    setHint(
      aiConfig?.config_error
        ? aiConfig.config_error
        : aiConfig?.has_key
          ? '已配置密钥（不会回传明文）'
          : '尚未配置密钥',
    );
    setError('');
  }, [open, aiConfig]);

  if (!open) return null;

  const reuseConnection = () => {
    const nextProtocol = asAiProtocol(connection.protocol);
    setUrl(connection.url);
    setProtocol(nextProtocol);
    setReuseModel(connection.model || '');
    if (connection.apiKey) {
      setApiKey(connection.apiKey);
      setClearKey(false);
    }
    setHint(
      connection.protocol === 'anthropic' || connection.protocol === 'gemini'
        ? '已带入实验连接；Orbit 仅支持 Chat / Responses，协议已改为 Chat Completions。模型请在 Orbit 输入栏选择。'
        : '已带入当前实验连接；模型请在 Orbit 输入栏选择，保存后才会生效。',
    );
    setError('');
  };

  const payload = () => ({
    url: url.trim(),
    protocol,
    api_key: clearKey ? null : (apiKey.trim() || undefined),
    ...(reuseModel.trim() ? { model: reuseModel.trim() } : {}),
  });

  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const next = await api.updateAiConfig(payload());
      setAiConfig(next);
      setApiKey('');
      setClearKey(false);
      setReuseModel('');
      setHint(next.has_key ? '已保存（密钥仅存内存）' : '已保存（未配置密钥）');
      showNotice('AI 模型配置已更新');
      onClose();
    } catch (err) {
      const message = err instanceof ApiError ? err.message : (err as Error).message;
      setError(message || '保存失败');
    } finally {
      setBusy(false);
    }
  };

  const test = async () => {
    if (!(aiConfig?.model || reuseModel.trim())) {
      setError('请先在 Orbit 输入栏选择模型');
      return;
    }
    setTesting(true);
    setError('');
    try {
      const next = await api.updateAiConfig(payload());
      setAiConfig(next);
      setApiKey('');
      setClearKey(false);
      setReuseModel('');
      const result = await api.testAiConfig();
      const message = ('message' in result && typeof result.message === 'string' && result.message)
        || (result.ok ? `连通性正常 · ${result.model || next.model}` : '连通性测试失败');
      setHint(message);
      showNotice(message);
    } catch (err) {
      const message = err instanceof ApiError ? err.message : (err as Error).message;
      setError(message || '连通性测试失败');
      showAlert(message || '连通性测试失败');
    } finally {
      setTesting(false);
    }
  };

  const canTest = Boolean(aiConfig?.model || reuseModel.trim());

  return (
    <div className="ai-modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="ai-modal card"
        role="dialog"
        aria-modal="true"
        aria-labelledby="ai-config-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="card-head">
          <div>
            <h2 id="ai-config-title" className="card-title">AI 模型设置</h2>
            <p className="card-desc">密钥仅保存在后端进程内存，不会回传明文，也不会落盘。请填写 SDK 基址，不要带 /chat/completions。模型在 Orbit 输入栏选择。</p>
          </div>
          <button type="button" className="icon-btn" aria-label="关闭" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <form className="ai-config-form" onSubmit={save}>
          <label className="field">
            <span className="field-label">API 地址</span>
            <input type="url" value={url} onChange={(e) => setUrl(e.target.value)}
              placeholder="https://api.openai.com/v1 或中转站基址" autoComplete="off" />
          </label>

          <div className="field">
            <span className="field-label">协议</span>
            <Select
              ariaLabel="协议"
              value={protocol}
              onChange={(next) => setProtocol(asAiProtocol(next))}
              options={[
                { value: 'openai', label: 'Chat Completions' },
                { value: 'openai_responses', label: 'Responses' },
              ]}
            />
          </div>

          <label className="field">
            <span className="field-label">API Key</span>
            <SecretInput
              value={apiKey}
              onChange={(e) => { setApiKey(e.target.value); setClearKey(false); }}
              placeholder={aiConfig?.has_key && !clearKey ? '已保存密钥 · 留空则保留' : '仅存内存，不落盘'}
              autoComplete="off"
            />
          </label>
          {aiConfig?.has_key && (
            <label className="consent">
              <input type="checkbox" checked={clearKey} onChange={(e) => setClearKey(e.target.checked)} />
              <span>清除已保存的密钥</span>
            </label>
          )}

          {error ? <Hint error>{error}</Hint> : <Hint>{hint}</Hint>}

          <div className="ai-config-actions">
            <button type="button" className="pill" onClick={reuseConnection}>
              复用当前实验连接
            </button>
            <button type="button" className="pill" disabled={testing || busy || !canTest} onClick={() => void test()}>
              {testing ? <LoaderCircle size={14} className="spin" /> : <Plug size={14} />}
              测试连通性
            </button>
            <button type="submit" className="btn primary" disabled={busy || testing}>
              {busy ? '保存中…' : '保存'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

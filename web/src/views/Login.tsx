import { ArrowLeft } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';

import { api } from '../api';
import { DotField } from '../components/DotField';
import { Field, Hint, SecretInput } from '../components/ui';

export function Login({ onSuccess, onBack }: { onSuccess: (user: string) => void; onBack?: () => void }) {
  const [username, setUsername] = useState('admin');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    document.title = '登录 · Relay Lab';
  }, []);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      const payload = await api.login(username.trim(), password);
      onSuccess(payload.user);
    } catch (err) {
      setError((err as Error).message || '登录失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <DotField />
      <div className="login-shell">
        <form className="login-card card" onSubmit={(event) => { void submit(event); }}>
          <div className="login-top">
            <div className="login-brand">
              <span className="mark"><img src="/logo.png" alt="" width={36} height={36} /></span>
              <div>
                <strong>Relay Lab</strong>
                <small>CONSOLE</small>
              </div>
            </div>
            {onBack && (
              <button type="button" className="login-back" onClick={onBack}>
                <ArrowLeft size={14} strokeWidth={2} />
                <span>返回首页</span>
              </button>
            )}
          </div>
          <h1>登录</h1>
          <p className="card-desc">登录后进入 Relay Lab 控制台。</p>
          <Field label="用户名">
            <input
              type="text"
              name="username"
              autoComplete="username"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
            />
          </Field>
          <Field label="密码">
            <SecretInput
              name="password"
              autoComplete="current-password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              autoFocus
              required
            />
          </Field>
          {error && <Hint error>{error}</Hint>}
          <button type="submit" className="btn primary login-submit" disabled={busy || !username.trim() || !password}>
            {busy ? '登录中…' : '进入控制台'}
          </button>
        </form>
      </div>
    </>
  );
}

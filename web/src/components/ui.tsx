import { Check, ChevronDown, ChevronRight, Eye, EyeOff, Minus, X } from 'lucide-react';
import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { InputHTMLAttributes, ReactNode } from 'react';

import { fmt, PROTOCOL_SHORT, stateLabel } from '../format';
import { useLab } from '../store';
import type { ContractVerdict, Protocol, RunKind } from '../types';

export type SelectOption = {
  value: string;
  label: string;
  disabled?: boolean;
};

/** Custom select — native menus stay unstyled on Windows, so we draw our own. */
export function Select({
  value,
  options,
  onChange,
  disabled = false,
  placeholder = '请选择',
  ariaLabel,
  className = '',
  size = 'field',
  searchable = false,
}: {
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
  ariaLabel?: string;
  className?: string;
  size?: 'field' | 'compact';
  searchable?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const root = useRef<HTMLDivElement | null>(null);
  const searchRef = useRef<HTMLInputElement | null>(null);
  const listId = useId();
  const selected = options.find((item) => item.value === value);
  const showSearch = searchable || options.length > 8;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return options;
    return options.filter((item) => item.label.toLowerCase().includes(q) || item.value.toLowerCase().includes(q));
  }, [options, query]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (event: MouseEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
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
    if (!showSearch) return;
    const id = window.setTimeout(() => searchRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, [open, showSearch]);

  return (
    <div
      className={`ui-select size-${size} ${open ? 'is-open' : ''} ${disabled ? 'is-disabled' : ''} ${className}`}
      ref={root}
    >
      <button
        type="button"
        className="ui-select-trigger"
        disabled={disabled}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => { if (!disabled) setOpen((v) => !v); }}
      >
        <span className={selected ? '' : 'is-placeholder'}>{selected?.label || placeholder}</span>
        <ChevronDown size={size === 'compact' ? 13 : 15} strokeWidth={2} />
      </button>
      {open && (
        <div className="ui-select-menu" role="listbox" id={listId} aria-label={ariaLabel || '选项'}>
          {showSearch && (
            <div className="ui-select-search">
              <input
                ref={searchRef}
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="搜索…"
                aria-label="搜索选项"
              />
            </div>
          )}
          <div className="ui-select-list">
            {filtered.length ? filtered.map((item) => {
              const active = item.value === value;
              return (
                <button
                  key={item.value}
                  type="button"
                  role="option"
                  aria-selected={active}
                  disabled={item.disabled}
                  className={`ui-select-option ${active ? 'active' : ''}`}
                  onClick={() => {
                    if (item.disabled) return;
                    setOpen(false);
                    if (item.value !== value) onChange(item.value);
                  }}
                >
                  <span>{item.label}</span>
                  {active ? <Check size={14} strokeWidth={2.2} /> : null}
                </button>
              );
            }) : (
              <p className="ui-select-empty">没有匹配项</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

/** White panel with the small letter-spaced title and a muted description line. */
export function Card({ title, desc, actions, className = '', children }: {
  title?: ReactNode;
  desc?: ReactNode;
  actions?: ReactNode;
  className?: string;
  children?: ReactNode;
}) {
  return (
    <section className={`card ${className}`}>
      {(title || actions) && (
        <header className="card-head">
          <div>
            {title && <h2 className="card-title">{title}</h2>}
            {desc && <p className="card-desc">{desc}</p>}
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function Field({ label, children, className = '' }: { label: ReactNode; children: ReactNode; className?: string }) {
  return (
    <label className={`field ${className}`}>
      <span className="field-label">{label}</span>
      {children}
    </label>
  );
}

export function SecretInput(props: InputHTMLAttributes<HTMLInputElement>) {
  const [shown, setShown] = useState(false);
  return (
    <span className="secret">
      <input {...props} type={shown ? 'text' : 'password'} />
      <button type="button" aria-label={shown ? '隐藏' : '显示'} onClick={() => setShown(!shown)}>
        {shown ? <EyeOff size={16} /> : <Eye size={16} />}
      </button>
    </span>
  );
}

export function Hint({ children, error = false }: { children: ReactNode; error?: boolean }) {
  return <p className={error ? 'hint error' : 'hint'} role="status">{children}</p>;
}

/** Same three wires as retest_kimi.py (oa / rsp / ant); short id + path for the picker. */
const CONTRACT_PROTOCOL_OPTIONS: { id: Protocol; label: string; path: string }[] = [
  { id: 'openai', label: 'openai', path: '/v1/chat/completions' },
  { id: 'openai_responses', label: 'responses', path: '/v1/responses' },
  { id: 'anthropic', label: 'anthropic', path: '/v1/messages' },
];

/** Connection block shared by every test page; state lives in the store so all pages stay in sync. */
export function ConnectionFields({
  multiProtocols,
  onMultiProtocols,
}: {
  /** Contract page: multi-select among the three supported wires. */
  multiProtocols?: Protocol[];
  onMultiProtocols?: (next: Protocol[]) => void;
}) {
  const { connection, setUrl, setProtocol, setAnthropicVersion, setApiKey, setModel, fetchModels } = useLab();
  const { url, protocol, anthropicVersion, apiKey, model, models, loading, error, hint } = connection;
  const multi = multiProtocols != null && onMultiProtocols != null;
  const toggleProtocol = (id: Protocol) => {
    if (!onMultiProtocols || !multiProtocols) return;
    if (multiProtocols.includes(id)) {
      if (multiProtocols.length <= 1) return;
      onMultiProtocols(multiProtocols.filter((item) => item !== id));
      return;
    }
    onMultiProtocols(
      CONTRACT_PROTOCOL_OPTIONS.map((option) => option.id).filter(
        (item) => item === id || multiProtocols.includes(item),
      ),
    );
  };
  return (
    <div className="connection">
      <Field label="API 地址">
        <input type="url" value={url} onChange={(event) => setUrl(event.target.value)}
          placeholder="https://example.com 或完整接口地址" autoComplete="off" />
      </Field>
      {multi ? (
        <Field label="接口协议">
          <div className="protocol-multi" role="group" aria-label="选择要测试的协议">
            {CONTRACT_PROTOCOL_OPTIONS.map((option) => {
              const on = multiProtocols.includes(option.id);
              return (
                <button
                  key={option.id}
                  type="button"
                  aria-pressed={on}
                  onClick={() => toggleProtocol(option.id)}
                >
                  <strong>{option.label}</strong>
                  <small>{option.path}</small>
                </button>
              );
            })}
          </div>
        </Field>
      ) : (
        <div className="two">
          <Field label="接口协议">
            <Select
              ariaLabel="接口协议"
              value={protocol}
              onChange={(next) => setProtocol(next as Protocol)}
              options={[
                { value: 'openai', label: 'Chat Completions' },
                { value: 'openai_responses', label: 'Responses' },
                { value: 'anthropic', label: 'Anthropic Messages' },
                { value: 'gemini', label: 'Gemini' },
              ]}
            />
          </Field>
          <Field label="API Key">
            <SecretInput value={apiKey} onChange={(event) => setApiKey(event.target.value)}
              placeholder="仅本次使用" autoComplete="off" />
          </Field>
        </div>
      )}
      {multi ? (
        <div className="two">
          <Field label="API Key">
            <SecretInput value={apiKey} onChange={(event) => setApiKey(event.target.value)}
              placeholder="仅本次使用" autoComplete="off" />
          </Field>
          {multiProtocols.includes('anthropic') ? (
            <Field label="anthropic-version">
              <Select
                ariaLabel="anthropic-version"
                value={anthropicVersion}
                onChange={setAnthropicVersion}
                options={[
                  { value: '2023-06-01', label: '2023-06-01（默认 / 多数中继）' },
                  { value: '2026-10-21', label: '2026-10-21（官方最新）' },
                  { value: '2023-01-01', label: '2023-01-01（初版）' },
                ]}
              />
            </Field>
          ) : <div />}
        </div>
      ) : protocol === 'anthropic' ? (
        <Field label="anthropic-version">
          <Select
            ariaLabel="anthropic-version"
            value={anthropicVersion}
            onChange={setAnthropicVersion}
            options={[
              { value: '2023-06-01', label: '2023-06-01（默认 / 多数中继）' },
              { value: '2026-10-21', label: '2026-10-21（官方最新）' },
              { value: '2023-01-01', label: '2023-01-01（初版）' },
            ]}
          />
        </Field>
      ) : null}
      <div className="field">
        <div className="model-head">
          <span className="field-label">模型 ID</span>
          <button type="button" className="pill" disabled={loading || !url.trim()} onClick={() => { void fetchModels(); }}>
            {loading ? '获取中…' : models.length ? '刷新模型' : '获取模型'}
          </button>
        </div>
        <Select
          ariaLabel="模型 ID"
          value={models.includes(model) ? model : ''}
          disabled={loading || models.length === 0}
          placeholder={models.length ? '请选择模型' : '请先获取模型列表'}
          searchable
          onChange={setModel}
          options={models.map((id) => ({ value: id, label: id }))}
        />
      </div>
      <Hint error={!!error}>{error || hint}</Hint>
    </div>
  );
}

/** Consent checkbox + start/stop buttons, aligned bottom-right like a form footer. */
export function RunFooter({ kind, label, consent, onConsent, consentText = '我有权限测试此接口，知晓会产生请求费用。' }: {
  kind: RunKind;
  label: string;
  consent: boolean;
  onConsent: (value: boolean) => void;
  consentText?: string;
}) {
  const { ready, busy, active, stopActive } = useLab();
  const mine = active?.kind === kind;
  return (
    <div className="form-foot">
      <label className="consent">
        <input type="checkbox" checked={consent} onChange={(event) => onConsent(event.target.checked)} />
        <span>{consentText}</span>
      </label>
      <div className="form-buttons">
        {mine && <button type="button" className="btn danger" onClick={() => { void stopActive(); }}>停止</button>}
        <button type="submit" className="btn primary" disabled={!(ready && consent)}>
          {busy ? '正在提交…' : mine ? '运行中' : label}
        </button>
      </div>
    </div>
  );
}

export function Sparkline({ rows }: { rows: { value: number | null; label: string }[] }) {
  const usable = rows.filter((row): row is { value: number; label: string } => Number.isFinite(row.value));
  const width = 460;
  const height = 54;
  const max = Math.max(1, ...usable.map((row) => row.value)) * 1.2;
  const x = (index: number) => (usable.length <= 1 ? width / 2 : (index / (usable.length - 1)) * width);
  const y = (value: number) => height - (value / max) * (height - 6);
  return (
    <svg className="spark" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" role="img"
      aria-label={usable.length ? '随档位变化' : '暂无数据'}>
      <line x1="0" y1={height - 0.5} x2={width} y2={height - 0.5} stroke="rgba(20,22,26,0.1)" strokeDasharray="3 4" />
      {usable.length > 0 && (
        <polyline fill="none" stroke="var(--accent)" strokeWidth="1.6"
          points={usable.map((row, index) => `${x(index).toFixed(1)},${y(row.value).toFixed(1)}`).join(' ')} />
      )}
      {usable.map((row, index) => (
        <circle key={index} cx={x(index).toFixed(1)} cy={y(row.value).toFixed(1)} r="2.6" fill="var(--accent)">
          <title>{row.label}</title>
        </circle>
      ))}
    </svg>
  );
}

function RollDigit({ digit }: { digit: string }) {
  const n = Number(digit);
  const offset = Number.isFinite(n) ? n : 0;
  return (
    <span className="roll-digit" aria-hidden="true">
      <span className="roll-strip" style={{ transform: `translateY(-${offset}em)` }}>
        {'0123456789'.split('').map((item) => <span key={item}>{item}</span>)}
      </span>
    </span>
  );
}

/** Live odometer: rolls each digit. Trailing units like "ms" / "%" are kept beside the number. */
export function Tick({ text, className, suffix }: { text: string; className?: string; suffix?: ReactNode }) {
  const parsed = text.match(/^([\d.,]+)\s*([%a-zA-Z]*)$/);
  const body = parsed ? parsed[1] : text;
  const unit = parsed?.[2] || null;
  const chars = [...body];
  return (
    <b className={className} aria-label={text}>
      <span className="roll">
        {chars.map((char, index) => {
          const slot = chars.length - 1 - index;
          if (/\d/.test(char)) return <RollDigit key={`d${slot}`} digit={char} />;
          return <span key={`s${slot}`} className="roll-sep">{char}</span>;
        })}
      </span>
      {unit ? <i>{unit}</i> : null}
      {suffix}
    </b>
  );
}

export function Stat({ value, label, sub }: { value: ReactNode; label: string; sub?: ReactNode }) {
  const ticking = typeof value === 'string' || typeof value === 'number';
  return (
    <article className="stat">
      {ticking ? <Tick text={String(value)} /> : <b>{value}</b>}
      <span>{label}</span>
      {sub && <em>{sub}</em>}
    </article>
  );
}

export function Verdict({ status, children }: { status: string; children?: ReactNode }) {
  return <span className={`verdict ${status}`}>{children ?? stateLabel(status)}</span>;
}

/** Pass / fail as check and cross; observation is soft; inconclusive stays a neutral dash. */
export function ContractMark({ verdict }: { verdict: ContractVerdict | string }) {
  if (verdict === 'conformed') {
    return (
      <span className="contract-mark pass" title="通过" aria-label="通过">
        <Check size={18} strokeWidth={2.6} />
      </span>
    );
  }
  if (verdict === 'violated') {
    return (
      <span className="contract-mark fail" title="不通过" aria-label="不通过">
        <X size={18} strokeWidth={2.6} />
      </span>
    );
  }
  if (verdict === 'observation') {
    return (
      <span className="contract-mark observe" title="观察" aria-label="观察">
        <Eye size={16} strokeWidth={2.4} />
      </span>
    );
  }
  return (
    <span className="contract-mark unknown" title="无法判定" aria-label="无法判定">
      <Minus size={16} strokeWidth={2.4} />
    </span>
  );
}

type ContractListItem = {
  index: number;
  check: string;
  title: string;
  /** One-line probe intent; shown under the short title when present. */
  desc?: string;
  verdict: ContractVerdict | string;
  expected?: string;
  observed?: string;
  detail?: string;
  protocol?: string;
  request_headers?: Record<string, string>;
  response_headers?: Record<string, string>;
  request_body?: string;
  response_body?: string;
  missing_response_headers?: string[];
};

/** Soft list of protocol checks: 协议 / 检查项 / 应该返回 / 实际返回 / 结论. */
export function ContractResultList({ items }: { items: ContractListItem[] }) {
  const [openKey, setOpenKey] = useState<string | null>(null);
  if (!items.length) return <p className="empty">还没有协议检查数据。</p>;
  const showProtocol = items.some((item) => item.protocol);
  return (
    <div className={`contract-rows${showProtocol ? ' has-protocol' : ''}`}>
      <div className="contract-head" aria-hidden="true">
        {showProtocol ? <span>协议</span> : null}
        <span>检查项</span>
        <span>应该返回</span>
        <span>实际返回</span>
        <span>结论</span>
      </div>
      {items.map((item) => {
        const expected = item.expected?.trim() || '—';
        const observed = compactObserved(item.observed);
        const note = item.detail?.trim() && item.detail.trim() !== observed ? item.detail.trim() : '';
        const protocol = item.protocol ? (PROTOCOL_SHORT[item.protocol] || item.protocol) : '';
        const desc = item.desc?.trim() || '';
        const checkTitle = desc ? `${item.title} · ${desc}` : item.title;
        const rowKey = `${item.index}-${item.protocol || ''}-${item.check}`;
        const open = openKey === rowKey;
        const hasWire = Boolean(
          Object.keys(item.request_headers || {}).length
          || Object.keys(item.response_headers || {}).length
          || item.request_body?.trim()
          || item.response_body?.trim()
          || item.missing_response_headers?.length,
        );
        return (
          <div key={rowKey} className={`contract-block${open ? ' open' : ''}`}>
            <div className="contract-row">
              {showProtocol ? (
                <span className="contract-protocol">{protocol || '—'}</span>
              ) : null}
              <div className="contract-check" title={checkTitle}>
                <strong>{item.title}</strong>
                {desc ? <small>{desc}</small> : null}
              </div>
              <span className="contract-expected" title={expected}>{expected}</span>
              <button
                type="button"
                className={`contract-actual${hasWire ? ' clickable' : ''}`}
                disabled={!hasWire}
                aria-expanded={open}
                title={hasWire ? (open ? '收起请求/响应详情' : '展开请求/响应详情') : undefined}
                onClick={() => setOpenKey(open ? null : rowKey)}
              >
                <span className="contract-actual-head">
                  {hasWire ? (
                    open
                      ? <ChevronDown size={14} strokeWidth={2.2} aria-hidden="true" />
                      : <ChevronRight size={14} strokeWidth={2.2} aria-hidden="true" />
                  ) : null}
                  <span className="contract-observed" title={item.observed?.trim() || observed}>{observed}</span>
                </span>
                {note ? <small className="contract-note" title={note}>{note}</small> : null}
              </button>
              <ContractMark verdict={item.verdict} />
            </div>
            {open && hasWire ? <ContractWireDetail item={item} /> : null}
          </div>
        );
      })}
    </div>
  );
}

function formatHeaderMap(headers?: Record<string, string>): string {
  const entries = Object.entries(headers || {});
  if (!entries.length) return '（无）';
  return entries.map(([key, value]) => `${key}: ${value}`).join('\n');
}

/** Split backend multi-round wire dumps marked as `=== 第 N 轮请求/响应 ===`. */
function splitRoundArtifacts(raw: string | undefined, kind: 'request' | 'response'): string[] {
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

function ContractWireDetail({ item }: { item: ContractListItem }) {
  const missing = item.missing_response_headers?.filter(Boolean) ?? [];
  const requestHeaders = Object.entries(item.request_headers || {});
  const responseHeaders = Object.entries(item.response_headers || {});
  const requestRounds = splitRoundArtifacts(item.request_body, 'request');
  const responseRounds = splitRoundArtifacts(item.response_body, 'response');
  // Only render header blocks when there is something to inspect; empty「（无）」is noise.
  return (
    <div className="contract-wire">
      {requestHeaders.length ? (
        <WireBlock title="请求头" body={formatHeaderMap(item.request_headers)} />
      ) : null}
      {requestRounds.length ? requestRounds.map((body, index) => (
        <WireBlock
          key={`req-${index}`}
          title={requestRounds.length > 1 ? `请求体 · 第 ${index + 1} 轮` : '请求体'}
          body={body}
        />
      )) : (
        <WireBlock title="请求体" body="（本次运行未记录请求体）" />
      )}
      {responseHeaders.length ? (
        <WireBlock title="响应头" body={formatHeaderMap(item.response_headers)} />
      ) : null}
      {missing.length ? (
        <div className="contract-wire-missing">
          <strong>缺失的响应头</strong>
          <ul>
            {missing.map((name) => <li key={name}><code>{name}</code></li>)}
          </ul>
        </div>
      ) : null}
      {responseRounds.length ? responseRounds.map((body, index) => (
        <WireBlock
          key={`res-${index}`}
          title={responseRounds.length > 1 ? `响应体 · 第 ${index + 1} 轮` : '响应体'}
          body={body}
        />
      )) : (
        <WireBlock title="响应体" body="（本次运行未记录响应原文）" />
      )}
    </div>
  );
}

function WireBlock({ title, body }: { title: string; body: string }) {
  return (
    <div className="contract-wire-block">
      <strong>{title}</strong>
      <pre>{body}</pre>
    </div>
  );
}

/**
 * Keep the console row readable: tool-choice failures used to dump the model essay
 * into「实际返回」. Prefer a short evidence label; full wire body stays in the export.
 */
function compactObserved(value?: string): string {
  const text = value?.trim() || '';
  if (!text) return '—';
  const flat = text.replace(/\s+/g, ' ');
  if (flat.length <= 72) return flat;
  // Long prose / markdown replies are evidence shape, not a scannable label.
  if (/[。！？；]/.test(flat) || flat.includes('**') || flat.split(' ').length > 12) {
    return `文本回复（${flat.length} 字）`;
  }
  return `${flat.slice(0, 68)}…`;
}

export function EmptyRow({ colSpan, children }: { colSpan: number; children: ReactNode }) {
  return <tr><td colSpan={colSpan} className="empty">{children}</td></tr>;
}

export function Notes({ items }: { items: string[] }) {
  return <ul className="notes">{items.map((text) => <li key={text}>{text}</li>)}</ul>;
}

export const ms = (value: number | null | undefined): string => (value === null || value === undefined ? '—' : `${fmt(value, 0)} ms`);

import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';

import { api } from '../api';
import { Card, ConnectionFields, Field, Hint, Notes, SecretInput, Select } from '../components/ui';
import { SCHEDULE_LABEL, dateTime } from '../format';
import { useLab } from '../store';
import type { Schedule } from '../types';

type ScheduleKind = 'speed' | 'parallel' | 'capacity' | 'quality' | 'contract';

function summary(item: Schedule): string {
  if (item.kind === 'speed') return `速度 · ${item.speed?.requests} 个请求`;
  if (item.kind === 'parallel') return `并行 · ${item.parallel?.levels?.join('/')} 并发`;
  if (item.kind === 'quality') return `质量 · 每类 ${item.quality?.per_category ?? 8} 题`;
  if (item.kind === 'contract') {
    const mode = item.contract?.mode === 'fast' ? '快速' : '全量';
    return `协议 · ${mode} · ${item.contract?.checks?.length || '自动'} 项`;
  }
  return `容量 · 起始 ${item.capacity?.start_rpm} RPM`;
}

export function Schedules() {
  const { schedules, loadSchedules, connection, ready, showAlert, showNotice } = useLab();
  const [label, setLabel] = useState('');
  const [interval, setIntervalMinutes] = useState(60);
  const [kind, setKind] = useState<ScheduleKind>('speed');
  const [requests, setRequests] = useState(20);
  const [levels, setLevels] = useState('1, 4');
  const [startRpm, setStartRpm] = useState(30);
  const [consent, setConsent] = useState(false);
  const [keys, setKeys] = useState<Record<string, string>>({});

  useEffect(() => {
    loadSchedules().catch(() => {});
    const timer = window.setInterval(() => {
      if (!document.hidden) loadSchedules().catch(() => {});
    }, 10000);
    return () => window.clearInterval(timer);
  }, [loadSchedules]);

  const act = async (action: () => Promise<unknown>, fallback: string, done?: string) => {
    try {
      await action();
      await loadSchedules();
      if (done) showNotice(done);
    } catch (error) {
      showAlert((error as Error).message || fallback);
    }
  };

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const target = { url: connection.url, model: connection.model, protocol: connection.protocol };
    const body: Record<string, unknown> = {
      label: label.trim() || `${connection.model} 定时任务`,
      kind,
      interval_minutes: interval,
      api_key: connection.apiKey || undefined,
    };
    if (kind === 'speed') body.speed = { ...target, requests };
    else if (kind === 'parallel') {
      body.parallel = { ...target, levels: levels.split(',').map((value) => Number(value.trim())).filter(Number.isFinite) };
    } else if (kind === 'quality') body.quality = { ...target, per_category: 8 };
    else if (kind === 'contract') body.contract = { ...target, mode: 'full', checks: [] };
    else body.capacity = { ...target, start_rpm: startRpm };
    void act(() => api.createSchedule(body), '创建失败', '定时任务已创建。密钥只保存在内存。');
  };

  return (
    <>
      <p className="view-sub">按间隔重复执行。密钥只留在内存，服务重启后需要重新输入。</p>
      <div className="split">
        <form className="card form-card" onSubmit={submit}>
          <h2 className="card-title">新建定时任务</h2>
          <p className="card-desc">容量探测使用快速探索预设；并行测试每档 15 秒。</p>
          <div className="two">
            <Field label="名称"><input type="text" maxLength={80} value={label} onChange={(e) => setLabel(e.target.value)} placeholder="例如：每小时测速" /></Field>
            <Field label="间隔（分钟）"><input type="number" min={5} max={1440} value={interval} onChange={(e) => setIntervalMinutes(Number(e.target.value))} /></Field>
          </div>
          <div className="two">
            <Field label="任务类型">
              <Select
                ariaLabel="任务类型"
                value={kind}
                onChange={(next) => setKind(next as ScheduleKind)}
                options={[
                  { value: 'speed', label: '速度测试' },
                  { value: 'parallel', label: '并行测试' },
                  { value: 'capacity', label: '容量探测' },
                  { value: 'quality', label: '质量抽查' },
                  { value: 'contract', label: '协议检查' },
                ]}
              />
            </Field>
            {kind === 'speed' && <Field label="请求数"><input type="number" min={1} max={500} value={requests} onChange={(e) => setRequests(Number(e.target.value))} /></Field>}
            {kind === 'parallel' && <Field label="并发档位"><input type="text" value={levels} onChange={(e) => setLevels(e.target.value)} /></Field>}
            {kind === 'capacity' && <Field label="起始 RPM"><input type="number" min={1} max={6000} value={startRpm} onChange={(e) => setStartRpm(Number(e.target.value))} /></Field>}
          </div>
          <hr />
          <h2 className="card-title">连接模型</h2>
          <p className="card-desc">地址、Key 与模型在所有测试页之间共享。</p>
          <ConnectionFields />
          <div className="form-foot">
            <label className="consent">
              <input type="checkbox" checked={consent} onChange={(e) => setConsent(e.target.checked)} />
              <span>我确认这些请求会真实发出，并知晓长期运行的费用。</span>
            </label>
            <div className="form-buttons">
              <button type="submit" className="btn primary" disabled={!(ready && consent)}>创建定时任务</button>
            </div>
          </div>
        </form>

        <div className="stack">
          <Card title="任务列表" desc={`${schedules.length} 个任务`}>
            <div className="schedule-list">
              {schedules.length ? schedules.map((item) => (
                <div key={item.schedule_id} className="schedule-row">
                  <div className="schedule-main">
                    <span className={`dot ${item.enabled ? 'on' : ''}`} />
                    <div>
                      <strong>{item.label}</strong>
                      <small>{summary(item)} · 每 {item.interval_minutes} 分钟</small>
                      <small>下次 {dateTime(item.next_run_at)} · {(item.last_status && SCHEDULE_LABEL[item.last_status]) || item.last_status || '未运行'}</small>
                    </div>
                  </div>
                  {item.needs_key && (
                    <SecretInput placeholder="补填 Key" value={keys[item.schedule_id] ?? ''}
                      onChange={(e) => setKeys((current) => ({ ...current, [item.schedule_id]: e.target.value }))} />
                  )}
                  <div className="schedule-actions">
                    <button type="button" className="pill"
                      onClick={() => act(() => api.runSchedule(item.schedule_id, keys[item.schedule_id]), '立即执行失败', '已触发一次执行；结果会出现在实验记录里。')}>
                      立即执行
                    </button>
                    <button type="button" className="pill" onClick={() => act(() => api.toggleSchedule(item.schedule_id, !item.enabled), '操作失败')}>
                      {item.enabled ? '停用' : '启用'}
                    </button>
                    <button type="button" className="pill danger-text" onClick={() => act(() => api.deleteSchedule(item.schedule_id), '删除失败')}>删除</button>
                  </div>
                </div>
              )) : <Hint>还没有定时任务。</Hint>}
            </div>
          </Card>
          <Card title="说明">
            <Notes items={[
              '调度器每 20 秒检查一次，误差在 20 秒内。',
              '同时只允许一个实验运行；撞车时该次记为“被占用”，下个周期重试。',
              '密钥不落盘；重启后任务保留，但需要重新输入密钥才能继续。',
            ]} />
          </Card>
        </div>
      </div>
    </>
  );
}

import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';

import { api } from '../api';
import type { DatasetPreset } from '../api';
import { AiDiagnosePanel } from '../components/AiDiagnosePanel';
import { LoadResults } from '../components/LoadResults';
import { Card, ConnectionFields, Field, Hint, RunFooter, Select, Verdict } from '../components/ui';
import { fmt } from '../format';
import { useLab } from '../store';
import type { CapacitySettings, Goal } from '../types';

const DEFAULTS: CapacitySettings = {
  label: '',
  goal: 'both',
  prompt_reuse: false,
  dataset_preset: 'default',
  input_tokens: null,
  stage_seconds: 30,
  repeats: 1,
  max_steps: 5,
  refine_steps: 3,
  start_rpm: 60,
  max_rpm: 480,
  max_parallel: 8,
  max_tpm_parallel: 16,
  max_requests: 1000,
  token_reserve: 3000000,
  max_wall_seconds: 1200,
  request_timeout: 60,
  cooldown_seconds: 10,
  success_percent: 90,
  ttft_p99_ms: 3000,
  tpot_p99_ms: 50,
  capacity_ratio: 0.7,
  short_output_tokens: 32,
  long_output_tokens: 1024,
  seed: 42,
};

type NumericField = Exclude<keyof CapacitySettings, 'label' | 'goal' | 'prompt_reuse' | 'dataset_preset' | 'input_tokens'>;

const ADVANCED: [NumericField, string, number, number, number][] = [
  ['start_rpm', '起始 RPM', 1, 6000, 1],
  ['max_rpm', '目标 RPM 安全上限', 1, 6000, 1],
  ['max_parallel', '最大并发（RPM 在途）', 1, 128, 1],
  ['max_tpm_parallel', 'TPM 并发上限', 1, 128, 1],
  ['max_requests', '请求预算（两轮均分）', 3, 20000, 1],
  ['stage_seconds', '每档秒数', 5, 600, 1],
  ['repeats', '每档重复次数', 1, 3, 1],
  ['max_steps', '倍增档数上限', 1, 8, 1],
  ['refine_steps', '二分复测次数', 0, 5, 1],
  ['max_wall_seconds', '总时长上限（秒）', 30, 7200, 1],
  ['request_timeout', '单请求超时（秒）', 2, 300, 1],
  ['cooldown_seconds', '档间冷却（秒）', 0, 120, 1],
  ['success_percent', '成功率下限（%）', 90, 100, 0.1],
  ['ttft_p99_ms', 'TTFT P99 上限（ms）', 1, 120000, 1],
  ['tpot_p99_ms', 'TPOT P99 上限（ms）', 1, 10000, 1],
  ['capacity_ratio', '建议容量折算比例', 0.1, 1, 0.05],
  ['short_output_tokens', 'RPM 测试输出上限', 8, 256, 1],
  ['long_output_tokens', 'TPM 测试输出上限', 64, 4096, 1],
  ['token_reserve', '估算 Token 预留预算', 10000, 100000000, 1000],
  ['seed', '数据集种子', 0, 2147483647, 1],
];

const PRESETS = {
  quick: { stage_seconds: 30, repeats: 1, max_requests: 1000, token_reserve: 3000000, max_wall_seconds: 1200, cooldown_seconds: 10 },
  steady: { stage_seconds: 60, repeats: 3, max_requests: 4000, token_reserve: 20000000, max_wall_seconds: 3600, cooldown_seconds: 15 },
};
type Preset = keyof typeof PRESETS;

const GOALS: [Goal, string][] = [['both', '两项都测'], ['rpm', '最大 RPM'], ['tpm', '输出 TPM']];

interface LoadForm {
  prompt_reuse: boolean;
  stream: boolean;
  concurrency_start: number;
  concurrency_step: number;
  window_seconds: number;
  windows: number;
  output_tokens: number;
  max_requests: number;
  request_timeout: number;
  success_percent: number;
  circuit_success_percent: number;
  max_consecutive_failures: number;
  avg_latency_ms: number;
  ttft_p99_ms: number;
  tpot_p99_ms: number;
  capacity_ratio: number;
}

const LOAD_DEFAULTS: LoadForm = {
  prompt_reuse: false,
  stream: true,
  concurrency_start: 100,
  concurrency_step: 0,
  window_seconds: 60,
  windows: 1,
  output_tokens: 64,
  max_requests: 12000,
  request_timeout: 150,
  success_percent: 90,
  circuit_success_percent: 80,
  max_consecutive_failures: 20,
  avg_latency_ms: 150000,
  ttft_p99_ms: 3000,
  tpot_p99_ms: 50,
  capacity_ratio: 0.7,
};

type LoadNumberField = Exclude<keyof LoadForm, 'prompt_reuse' | 'stream'>;

const LOAD_CORE: [LoadNumberField, string, number, number, number][] = [
  ['concurrency_start', '并发路数', 1, 256, 1],
  ['window_seconds', '测试时长（秒）', 5, 60, 1],
  ['output_tokens', '单请求输出上限', 8, 512, 8],
  ['max_requests', '请求上限（兜底）', 10, 20000, 10],
  ['request_timeout', '单请求超时（秒）', 2, 300, 1],
];

const LOAD_CIRCUIT: [LoadNumberField, string, number, number, number][] = [
  ['circuit_success_percent', '成功率阈值（%）', 0, 100, 0.5],
  ['max_consecutive_failures', '最大连续失败次数', 0, 10000, 1],
  ['avg_latency_ms', '平均响应时间阈值（ms）', 0, 600000, 1000],
];

const LOAD_SLA: [LoadNumberField, string, number, number, number][] = [
  ['success_percent', '成功率下限（%）', 0, 100, 0.5],
  ['ttft_p99_ms', 'TTFT P99 上限（ms）', 1, 120000, 1],
  ['tpot_p99_ms', 'TPOT P99 上限（ms）', 1, 10000, 1],
  ['capacity_ratio', '建议容量折算比例', 0.1, 1, 0.05],
];

const LOAD_ADVANCED: [LoadNumberField, string, number, number, number][] = [
  ['concurrency_step', '每窗递增并发', 0, 64, 1],
  ['windows', '窗口数量', 1, 12, 1],
];

export function Capacity() {
  const { startRun, connection, basePayload, latest, pendingDraft, clearDraft, showNotice } = useLab();
  const [mode, setMode] = useState<'capacity' | 'load'>('capacity');
  const [load, setLoad] = useState<LoadForm>(LOAD_DEFAULTS);
  const [settings, setSettings] = useState<CapacitySettings>(DEFAULTS);
  const [preset, setPreset] = useState<Preset>('quick');
  const [consent, setConsent] = useState(false);
  const [datasets, setDatasets] = useState<DatasetPreset[]>([]);

  // The preset list lives on the server so the console and the runner can never
  // disagree about what a named workload means.
  useEffect(() => {
    api.presets().then((payload) => setDatasets(payload.datasets)).catch(() => {});
  }, []);

  useEffect(() => {
    if (!pendingDraft || (pendingDraft.kind !== 'capacity' && pendingDraft.kind !== 'load')) return;
    const s = pendingDraft.settings;
    if (pendingDraft.kind === 'load') {
      setMode('load');
      setLoad((current) => {
        const next = { ...current };
        (Object.keys(LOAD_DEFAULTS) as (keyof LoadForm)[]).forEach((key) => {
          const value = s[key];
          if (key === 'prompt_reuse' || key === 'stream') {
            if (typeof value === 'boolean') next[key] = value;
            return;
          }
          if (typeof value === 'number') next[key] = value;
        });
        return next;
      });
      if (typeof s.label === 'string') setSettings((current) => ({ ...current, label: s.label as string }));
    } else {
      setMode('capacity');
      setSettings((current) => {
        const next = { ...current };
        (Object.keys(DEFAULTS) as (keyof CapacitySettings)[]).forEach((key) => {
          const value = s[key];
          if (value === undefined) return;
          if (key === 'goal' && (value === 'both' || value === 'rpm' || value === 'tpm')) {
            next.goal = value;
            return;
          }
          if (key === 'prompt_reuse' && typeof value === 'boolean') {
            next.prompt_reuse = value;
            return;
          }
          if (key === 'dataset_preset' && (typeof value === 'string' || value === null)) {
            next.dataset_preset = value;
            return;
          }
          if (key === 'label' && typeof value === 'string') {
            next.label = value;
            return;
          }
          if (key === 'input_tokens' && (typeof value === 'number' || value === null)) {
            next.input_tokens = value;
            return;
          }
          if (typeof value === 'number' && key !== 'label' && key !== 'goal' && key !== 'prompt_reuse' && key !== 'dataset_preset' && key !== 'input_tokens') {
            next[key] = value;
          }
        });
        return next;
      });
    }
    clearDraft();
    showNotice('已填入 AI 推荐的容量/吞吐参数，请确认后启动。');
  }, [pendingDraft, clearDraft, showNotice]);

  const active = datasets.find((item) => item.key === (settings.dataset_preset ?? 'default'));

  const loadRun = latest('load');
  const capacityRun = latest('capacity');

  const update = (patch: Partial<CapacitySettings>) => setSettings((current) => ({ ...current, ...patch }));
  const updateLoad = (patch: Partial<LoadForm>) => setLoad((current) => ({ ...current, ...patch }));

  const choosePreset = (name: Preset) => {
    setPreset(name);
    update(PRESETS[name]);
  };

  const numberField = (
    fields: [LoadNumberField, string, number, number, number][],
    patch: (field: LoadNumberField, value: number) => void,
  ) => (
    <div className="adv-grid">
      {fields.map(([field, label, min, max, step]) => (
        <Field key={field} label={label}>
          <input type="number" min={min} max={max} step={step} value={load[field]}
            onChange={(event) => {
              const value = Number(event.target.value);
              if (Number.isFinite(value)) patch(field, value);
            }} />
        </Field>
      ))}
    </div>
  );

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (mode === 'load') {
      void startRun('/api/load', {
        ...load,
        ...basePayload(),
        label: settings.label.trim() || `${connection.model} · 吞吐 ${load.window_seconds * load.windows} 秒`,
        authorized: consent,
      }, '吞吐测试');
      return;
    }
    void startRun('/api/runs', {
      ...settings,
      ...basePayload(),
      label: settings.label.trim() || `${connection.model} · ${preset === 'quick' ? '快速探索' : '稳定复核'}`,
      authorized: consent,
    }, '容量探测');
  };

  return (
    <>
      <p className="view-sub">倍增加压，找到当前条件下的合格吞吐；严格 SLA 下会给下界而不是绝对极限。</p>
      <div className="split">
        <form className="card form-card" onSubmit={submit}>
          <h2 className="card-title">连接模型</h2>
          <p className="card-desc">地址、Key 与模型在所有测试页之间共享。</p>
          <ConnectionFields />
          <hr />
          <div className="presets" role="group" aria-label="测试模式">
            <button type="button" aria-pressed={mode === 'capacity'} onClick={() => setMode('capacity')}>
              <strong>容量探测</strong><small>倍增加压找边界，逐档判定</small>
            </button>
            <button type="button" aria-pressed={mode === 'load'} onClick={() => setMode('load')}>
              <strong>快速吞吐</strong><small>恒定并发 100 路 · 跑满 1 分钟</small>
            </button>
          </div>

          {mode === 'capacity' && (<>
          <h2 className="card-title">测试目标</h2>
          <p className="card-desc">选择要找的上限，以及每档跑多久。</p>
          <div className="segmented" role="group" aria-label="测试目标">
            {GOALS.map(([goal, label]) => (
              <button key={goal} type="button" aria-pressed={settings.goal === goal} onClick={() => update({ goal })}>{label}</button>
            ))}
          </div>
          <div className="presets">
            <button type="button" aria-pressed={preset === 'quick'} onClick={() => choosePreset('quick')}>
              <strong>快速探索</strong><small>每档 30 秒 · 先了解大概</small>
            </button>
            <button type="button" aria-pressed={preset === 'steady'} onClick={() => choosePreset('steady')}>
              <strong>稳定复核</strong><small>每档 60 秒 × 3 次 · 消耗更多</small>
            </button>
          </div>

          {datasets.length > 0 && (
            <>
              <h2 className="card-title">测试数据集</h2>
              <p className="card-desc">输入长度决定瓶颈落在哪儿；不同数据集的容量数字不可直接比较。</p>
              <Field label="负载形态">
                <Select
                  ariaLabel="负载形态"
                  value={settings.dataset_preset ?? 'default'}
                  onChange={(next) => update({ dataset_preset: next, input_tokens: null })}
                  options={datasets.map((item) => ({ value: item.key, label: item.name }))}
                />
              </Field>
              {active && <Hint>{active.note}</Hint>}
            </>
          )}

          <Field label="请求复用">
            <Select
              ariaLabel="请求复用"
              value={settings.prompt_reuse ? 'on' : 'off'}
              onChange={(next) => update({ prompt_reuse: next === 'on' })}
              options={[
                { value: 'off', label: '关闭：每条不同（冷启动）' },
                { value: 'on', label: '开启：全部复用（缓存命中）' },
              ]}
            />
          </Field>

          <div className="callout">
            最多 <b>{fmt(settings.max_requests, 0)}</b> 个请求 · 并发不超过 <b>{settings.max_parallel}</b> ·
            总时长上限 <b>{fmt(settings.max_wall_seconds / 60)}</b> 分钟 ·
            建议容量按实测 × <b>{settings.capacity_ratio}</b>
          </div>

          <details className="advanced">
            <summary>高级参数 <span>通常不用改</span></summary>
            <div className="adv-grid">
              {ADVANCED.map(([field, label, min, max, step]) => (
                <Field key={field} label={label}>
                  <input type="number" min={min} max={max} step={step} value={settings[field]}
                    onChange={(event) => {
                      const value = Number(event.target.value);
                      if (Number.isFinite(value)) update({ [field]: value });
                    }} />
                </Field>
              ))}
            </div>
          </details>
          </>)}

          {mode === 'load' && (<>
            <Field label="请求形式">
              <div className="segmented cols-2" role="group" aria-label="请求形式">
                <button type="button" aria-pressed={load.stream} onClick={() => updateLoad({ stream: true })}>流式</button>
                <button type="button" aria-pressed={!load.stream} onClick={() => updateLoad({ stream: false })}>非流式</button>
              </div>
            </Field>
            <Field label="请求复用">
              <div className="segmented cols-2" role="group" aria-label="请求复用">
                <button type="button" aria-pressed={!load.prompt_reuse} onClick={() => updateLoad({ prompt_reuse: false })}>冷启动</button>
                <button type="button" aria-pressed={load.prompt_reuse} onClick={() => updateLoad({ prompt_reuse: true })}>缓存命中</button>
              </div>
            </Field>

            <details className="advanced">
              <summary>
                恒定并发
                <span>{load.concurrency_start} 路 · {load.window_seconds}s · 输出 {load.output_tokens}</span>
              </summary>
              {numberField(LOAD_CORE, (field, value) => updateLoad({ [field]: value }))}
            </details>

            <details className="advanced">
              <summary>
                熔断与 SLA
                <span>成功率 {load.success_percent}% · TTFT {load.ttft_p99_ms}ms</span>
              </summary>
              <div className="advanced-block">
                <h3>熔断阈值</h3>
                {numberField(LOAD_CIRCUIT, (field, value) => updateLoad({ [field]: value }))}
              </div>
              <div className="advanced-block">
                <h3>SLA 判定标准</h3>
                {numberField(LOAD_SLA, (field, value) => updateLoad({ [field]: value }))}
              </div>
            </details>

            <details className="advanced">
              <summary>
                并发爬坡
                <span>{load.concurrency_step > 0 ? `每窗 +${load.concurrency_step}` : '默认关闭'}</span>
              </summary>
              {numberField(LOAD_ADVANCED, (field, value) => updateLoad({ [field]: value }))}
            </details>

            <div className="callout callout-tags">
              <span>{load.stream ? '流式' : '非流式'}</span>
              <span>
                {load.concurrency_step > 0 && load.windows > 1
                  ? <>爬坡 <b>{load.concurrency_start}</b> +<b>{load.concurrency_step}</b>/窗</>
                  : <>并发 <b>{load.concurrency_start}</b> 路</>}
              </span>
              <span>时长 <b>{load.window_seconds}</b>s × <b>{load.windows}</b> 窗</span>
              <span>上限 <b>{fmt(load.max_requests, 0)}</b></span>
              <span>容量 × <b>{load.capacity_ratio}</b></span>
            </div>
          </>)}

          <RunFooter kind={mode === 'load' ? 'load' : 'capacity'}
            label={mode === 'load' ? '开始吞吐测试' : '开始探测'} consent={consent} onConsent={setConsent}
            consentText="我有权限测试此接口，已确认上述负载和预算，并知晓可能产生费用。" />
        </form>

        <div className="stack">
          {mode === 'capacity' ? (
            <>
              {capacityRun && capacityRun.stages.length > 0 && (
                <Card title="运行进度" className="table-card" desc={capacityRun.message}>
                  <div className="scroll">
                    <table className="stage-table">
                      <thead>
                        <tr><th>档位</th><th>目标</th><th>已完成</th><th>成功率</th><th>判定</th></tr>
                      </thead>
                      <tbody>
                        {capacityRun.stages.map((stage) => (
                          <tr key={stage.index}>
                            <td className="num">#{stage.index}<small>{stage.phase.toUpperCase()}</small></td>
                            <td className="num">
                              {stage.phase === 'rpm' ? `${fmt(stage.pressure, 0)} RPM` : `${stage.pressure} 并发`}
                            </td>
                            <td className="num">{fmt(stage.succeeded, 0)}/{fmt(stage.requests, 0)}</td>
                            <td className="num">{fmt(stage.success_percent)}%</td>
                            <td><Verdict status={stage.status} /></td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </Card>
              )}
              <Card title="实验会怎么跑" desc="每档都有独立的请求、Token 与时长上限，超出即停止。">
                <ol className="flow">
                  <li><b>倍增</b>从起始压力翻倍，直到某档不达标或到上限。</li>
                  <li><b>向下折半</b>首档就不达标时，减半找一个达标的下界。</li>
                  <li><b>二分复测</b>在达标与不达标之间收窄，给出边界区间。</li>
                </ol>
              </Card>
              <Card title="当前预算">
                <dl className="kv">
                  <div><dt>请求上限</dt><dd>{fmt(settings.max_requests, 0)}</dd></div>
                  <div><dt>估算 Token</dt><dd>{fmt(settings.token_reserve, 0)}</dd></div>
                  <div><dt>总时长</dt><dd>{fmt(settings.max_wall_seconds / 60)} 分钟</dd></div>
                  <div><dt>每档</dt><dd>{settings.stage_seconds} 秒 × {settings.repeats} 次</dd></div>
                </dl>
              </Card>
              <AiDiagnosePanel run={capacityRun} />
            </>
          ) : (
            <>
            <Card title="吞吐结果" className="table-card"
              desc={
                !loadRun
                  ? '还没有吞吐测试记录；填好参数后开始。'
                  : (loadRun.status === 'running' || loadRun.status === 'stopping')
                    ? loadRun.message
                    : undefined
              }>
              {loadRun ? <LoadResults run={loadRun} /> : <Hint>开始一次吞吐测试后，这里展示本窗实测吞吐与延迟。</Hint>}
            </Card>
            <AiDiagnosePanel run={loadRun} />
            </>
          )}
        </div>
      </div>
    </>
  );
}

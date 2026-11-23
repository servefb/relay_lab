import { Download } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';

import { AiDiagnosePanel } from '../components/AiDiagnosePanel';
import { Card, ConnectionFields, EmptyRow, Field, Hint, Notes, RunFooter, Sparkline } from '../components/ui';
import { exportRun, fmt, stateLabel } from '../format';
import { useLab } from '../store';

export function Parallel() {
  const { latest, startRun, basePayload, connection, pendingDraft, clearDraft, showNotice } = useLab();
  const [levels, setLevels] = useState('1, 2, 4, 8');
  const [seconds, setSeconds] = useState(15);
  const [tokens, setTokens] = useState(256);
  const [consent, setConsent] = useState(false);

  const run = latest('parallel');
  const stages = run?.stages ?? [];

  useEffect(() => {
    if (!pendingDraft || pendingDraft.kind !== 'parallel') return;
    const s = pendingDraft.settings;
    if (Array.isArray(s.levels)) setLevels(s.levels.join(', '));
    else if (typeof s.levels === 'string') setLevels(s.levels);
    if (typeof s.stage_seconds === 'number') setSeconds(s.stage_seconds);
    if (typeof s.output_tokens === 'number') setTokens(s.output_tokens);
    clearDraft();
    showNotice('已填入 AI 推荐的并行测试参数，请确认后启动。');
  }, [pendingDraft, clearDraft, showNotice]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    const parsed = levels.split(',')
      .map((value) => Number(value.trim()))
      .filter((value) => Number.isFinite(value) && value >= 1 && value <= 128);
    void startRun('/api/parallel', {
      ...basePayload(),
      label: `并行测试 · ${connection.model}`,
      levels: parsed.length ? parsed : [1],
      stage_seconds: seconds,
      output_tokens: tokens,
      request_timeout: 60,
      authorized: consent,
    }, '并行测试');
  };

  return (
    <>
      <p className="view-sub">按你给的并发档位逐档压测，看吞吐随并发怎么变化。</p>
      <div className="split">
        <form className="card form-card" onSubmit={submit}>
          <h2 className="card-title">连接模型</h2>
          <p className="card-desc">地址、Key 与模型在所有测试页之间共享。</p>
          <ConnectionFields />
          <hr />
          <h2 className="card-title">并发档位</h2>
          <p className="card-desc">逗号分隔，最多 8 个档位。</p>
          <Field label="并发档位">
            <input type="text" value={levels} onChange={(e) => setLevels(e.target.value)} placeholder="例如 1, 2, 4, 8" />
          </Field>
          <div className="two">
            <Field label="每档秒数"><input type="number" min={5} max={300} value={seconds} onChange={(e) => setSeconds(Number(e.target.value))} /></Field>
            <Field label="输出上限（tokens）"><input type="number" min={8} max={4096} value={tokens} onChange={(e) => setTokens(Number(e.target.value))} /></Field>
          </div>
          <RunFooter kind="parallel" label="开始并行测试" consent={consent} onConsent={setConsent} />
        </form>

        <div className="stack">
          <Card title="最近一次并行测试" desc={run ? stateLabel(run.status) : '还没有记录。'}>
            <Sparkline rows={stages.map((stage) => ({ value: stage.output_tpm, label: `并发 ${stage.pressure} · ${fmt(stage.rpm)} RPM` }))} />
            {run && <Hint>{run.settings.levels?.join(' / ')} 并发 · 每档 {run.settings.stage_seconds} 秒</Hint>}
          </Card>
          <AiDiagnosePanel run={run} />
          <Card title="说明">
            <Notes items={[
              '每档发满设定秒数，请求数按并发自动估算。',
              '吞吐下降不等于站点上限，也可能命中客户端或网络瓶颈。',
              '本页不做达标判定，只给事实数字。',
            ]} />
          </Card>
        </div>
      </div>

      <Card title="逐档结果" desc={run ? run.message : '—'} className="table-card"
        actions={<button type="button" className="pill" disabled={!run} onClick={() => exportRun(run)}><Download size={13} />导出报告</button>}>
        <div className="scroll">
          <table>
            <thead><tr><th>并发</th><th>请求 / 成功</th><th>成功 RPM</th><th>输出 TPM</th><th>TTFT P50</th><th>TTFT P99</th><th>平均延迟</th></tr></thead>
            <tbody>
              {stages.length ? stages.map((stage) => (
                <tr key={`${stage.index}-${stage.pressure}`}>
                  <td className="num">{stage.pressure}</td>
                  <td className="num">{stage.requests} / {stage.succeeded}</td>
                  <td className="num">{fmt(stage.rpm)}</td>
                  <td className="num">{fmt(stage.output_tpm)}</td>
                  <td className="num">{fmt(stage.ttft_p50_ms, 0)}</td>
                  <td className="num">{fmt(stage.ttft_p99_ms, 0)}</td>
                  <td className="num">{fmt(stage.latency_avg_ms, 0)}</td>
                </tr>
              )) : <EmptyRow colSpan={7}>还没有并行测试数据。</EmptyRow>}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}

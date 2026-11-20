import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';

import { AiDiagnosePanel } from '../components/AiDiagnosePanel';
import { Card, ConnectionFields, Field, Hint, Notes, RunFooter, Stat } from '../components/ui';
import { fmt, stateLabel } from '../format';
import { useLab } from '../store';

export function Speed() {
  const { latest, startRun, basePayload, connection, pendingDraft, clearDraft, showNotice } = useLab();
  const [requests, setRequests] = useState(20);
  const [parallel, setParallel] = useState(1);
  const [tokens, setTokens] = useState(256);
  const [timeout, setTimeoutSeconds] = useState(60);
  const [consent, setConsent] = useState(false);

  const run = latest('speed');
  const stage = run?.stages.find((item) => item.phase === 'speed');

  useEffect(() => {
    if (!pendingDraft || pendingDraft.kind !== 'speed') return;
    const s = pendingDraft.settings;
    if (typeof s.requests === 'number') setRequests(s.requests);
    if (typeof s.max_parallel === 'number') setParallel(s.max_parallel);
    if (typeof s.output_tokens === 'number') setTokens(s.output_tokens);
    if (typeof s.request_timeout === 'number') setTimeoutSeconds(s.request_timeout);
    clearDraft();
    showNotice('已填入 AI 推荐的测速参数，请确认后启动。');
  }, [pendingDraft, clearDraft, showNotice]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void startRun('/api/speed', {
      ...basePayload(),
      label: `速度测试 · ${connection.model}`,
      requests,
      max_parallel: parallel,
      output_tokens: tokens,
      request_timeout: timeout,
      authorized: consent,
    }, '速度测试');
  };

  return (
    <>
      <p className="view-sub">固定请求数、固定并发，只测单请求延迟与出词速度，不做压力搜索。</p>
      <div className="split">
        <form className="card form-card" onSubmit={submit}>
          <h2 className="card-title">连接模型</h2>
          <p className="card-desc">地址、Key 与模型在所有测试页之间共享。</p>
          <ConnectionFields />
          <hr />
          <h2 className="card-title">测试参数</h2>
          <p className="card-desc">样本量等于请求数。</p>
          <div className="two">
            <Field label="请求数"><input type="number" min={1} max={500} value={requests} onChange={(e) => setRequests(Number(e.target.value))} /></Field>
            <Field label="并发"><input type="number" min={1} max={8} value={parallel} onChange={(e) => setParallel(Number(e.target.value))} /></Field>
          </div>
          <div className="two">
            <Field label="输出上限（tokens）"><input type="number" min={8} max={4096} value={tokens} onChange={(e) => setTokens(Number(e.target.value))} /></Field>
            <Field label="单请求超时（秒）"><input type="number" min={2} max={300} value={timeout} onChange={(e) => setTimeoutSeconds(Number(e.target.value))} /></Field>
          </div>
          <RunFooter kind="speed" label="开始测速" consent={consent} onConsent={setConsent} />
        </form>

        <div className="stack">
          <Card title="最近一次测速" desc={run ? stateLabel(run.status) : '还没有测速记录。'}>
            <div className="grid-2 compact">
              <Stat value={stage?.ttft_p50_ms != null ? fmt(stage.ttft_p50_ms, 0) : '—'} label="TTFT P50" sub="首字延迟 ms" />
              <Stat value={stage?.ttft_p99_ms != null ? fmt(stage.ttft_p99_ms, 0) : '—'} label="TTFT P99" sub="长尾 ms" />
              <Stat value={stage?.latency_avg_ms != null ? fmt(stage.latency_avg_ms, 0) : '—'} label="平均延迟" sub="整段耗时 ms" />
              <Stat value={stage?.output_tpm ? fmt(stage.output_tpm / 60, 1) : '—'} label="输出速度" sub="tok/s" />
            </div>
            {run && (
              <Hint>
                {run.settings.requests} 个请求 / 并发 {run.settings.max_parallel} · {stage ? `${stage.succeeded}/${stage.requests} 成功` : '等待结果'}
              </Hint>
            )}
          </Card>
          <AiDiagnosePanel run={run} />
          <Card title="说明">
            <Notes items={[
              '并发保持 1 时，测的是单路体验；并发大于 1 时，分位数会互相干扰。',
              '输出上限限制生成长度，实际 token 由模型决定。',
              '样本量等于请求数，P99 在 20 个样本下约等于最大值。',
            ]} />
          </Card>
        </div>
      </div>
    </>
  );
}

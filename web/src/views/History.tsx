import { useMemo, useState } from 'react';

import { AiDiagnosePanel } from '../components/AiDiagnosePanel';
import { LoadResults, loadCircuitStopped } from '../components/LoadResults';
import { Card, ContractResultList, Hint, Verdict } from '../components/ui';
import { KIND_LABEL, PROTOCOL_LABEL, dateTime, fmt, stateLabel } from '../format';
import { useLab } from '../store';
import type { ContractItem, ContractVerdict, LabRun } from '../types';

type ContractFilter = 'issues' | 'violated' | 'observation' | 'inconclusive' | 'all';

function RunMeta({ run }: { run: LabRun }) {
  return (
    <div className="detail-meta">
      <span>模型 {run.settings.model}</span>
      <span>协议 {PROTOCOL_LABEL[run.settings.protocol] || run.settings.protocol}</span>
      <span>指纹 {run.fingerprint.slice(0, 10)}</span>
      <span>{dateTime(run.created_at)}</span>
    </div>
  );
}

function toListItem(item: ContractItem) {
  return {
    index: item.index,
    check: item.check,
    title: item.title,
    verdict: item.verdict,
    expected: item.expected,
    observed: item.observed || item.error || '',
    detail: item.detail,
    protocol: item.protocol,
    request_headers: item.request_headers,
    response_headers: item.response_headers,
    request_body: item.request_body,
    response_body: item.response_body,
    missing_response_headers: item.missing_response_headers,
  };
}

function filterContractItems(items: ContractItem[], filter: ContractFilter): ContractItem[] {
  if (filter === 'all') return items;
  if (filter === 'issues') return items.filter((item) => item.verdict !== 'conformed');
  return items.filter((item) => item.verdict === (filter as ContractVerdict));
}

function ContractDetail({ run }: { run: LabRun }) {
  const summary = run.contract_summary;
  const issueCount = run.contract.filter((item) => item.verdict !== 'conformed').length;
  const [filter, setFilter] = useState<ContractFilter>(issueCount > 0 ? 'issues' : 'all');
  const shown = useMemo(() => filterContractItems(run.contract, filter), [run.contract, filter]);
  if (!summary) return null;
  const evidenced = summary.conformed + summary.violated;
  const filters: { id: ContractFilter; label: string; count: number }[] = [
    { id: 'issues', label: '问题项', count: issueCount },
    { id: 'violated', label: '被改写', count: summary.violated },
    { id: 'observation', label: '观察', count: summary.observation ?? 0 },
    { id: 'inconclusive', label: '无法判定', count: summary.inconclusive },
    { id: 'all', label: '全部', count: summary.total },
  ];
  return (
    <>
      <div className="history-score">
        <div className="score">{evidenced > 0 ? `${fmt(summary.fidelity)}%` : '—'}</div>
        <small>协议保真度 · 共 {summary.total} 项</small>
      </div>
      <div className="bars">
        <div className="bar-row">
          <span>符合</span>
          <div className="bar"><i style={{ width: `${summary.total ? (100 * summary.conformed) / summary.total : 0}%` }} /></div>
          <b>{summary.conformed}/{summary.total}</b>
        </div>
        <div className="bar-row">
          <span>被改写</span>
          <div className="bar"><i style={{ width: `${summary.total ? (100 * summary.violated) / summary.total : 0}%` }} /></div>
          <b>{summary.violated}</b>
        </div>
        <div className="bar-row">
          <span>无法判定</span>
          <div className="bar"><i style={{ width: `${summary.total ? (100 * summary.inconclusive) / summary.total : 0}%` }} /></div>
          <b>{summary.inconclusive}</b>
        </div>
        {(summary.observation ?? 0) > 0 && (
          <div className="bar-row">
            <span>观察</span>
            <div className="bar"><i style={{ width: `${summary.total ? (100 * summary.observation) / summary.total : 0}%` }} /></div>
            <b>{summary.observation}</b>
          </div>
        )}
      </div>
      {summary.high_severity > 0 && (
        <Hint error>高危违规 {summary.high_severity} 项</Hint>
      )}
      <div className="history-contract-tools">
        {filters.map((item) => (
          <button
            key={item.id}
            type="button"
            className="pill"
            aria-pressed={filter === item.id}
            onClick={() => setFilter(item.id)}
          >
            {item.label} {item.count}
          </button>
        ))}
      </div>
      {shown.length ? (
        <div className="history-contract-list">
          <ContractResultList items={shown.map(toListItem)} />
        </div>
      ) : (
        <Hint>{filter === 'issues' ? '没有问题项，全部符合。' : '这一类没有检查项。'}</Hint>
      )}
    </>
  );
}

function QualityDetail({ run }: { run: LabRun }) {
  const quality = run.quality;
  if (!quality) return null;
  const levels = Object.entries(quality.by_level ?? {});
  return (
    <>
      <div className="history-score">
        <div className="score">{fmt(quality.accuracy)}%</div>
        <small>准确率 · {quality.passed}/{quality.total} 题</small>
      </div>
      {levels.length > 0 && (
        <div className="history-kpis">
          {levels.map(([level, score]) => (
            <div key={level}>
              <b>{score.passed}/{score.total}</b>
              <span>{level}</span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

function FindingDetail({ run }: { run: LabRun }) {
  const rpm = run.findings.find((finding) => finding.phase === 'rpm');
  const tpm = run.findings.find((finding) => finding.phase === 'tpm');
  if (!rpm && !tpm) return null;
  return (
    <>
      <div className="history-score">
        <div className="score">{fmt(rpm?.value, 0)}</div>
        <small>RPM 结论</small>
      </div>
      <div className="history-kpis">
        {rpm && (
          <div>
            <b><Verdict status={rpm.status}>{rpm.status === 'bounded' ? '可用' : stateLabel(run.status)}</Verdict></b>
            <span>RPM</span>
          </div>
        )}
        {tpm && (
          <div>
            <b>{fmt(tpm.value, 0)}</b>
            <span>TPM · <Verdict status={tpm.status}>{tpm.status === 'bounded' ? '可用' : stateLabel(run.status)}</Verdict></span>
          </div>
        )}
      </div>
    </>
  );
}

function StagesDetail({ run }: { run: LabRun }) {
  if (!run.stages.length) return null;
  return (
    <div className="detail-stages">
      {run.stages.slice(0, 8).map((stage) => (
        <div key={stage.index}>
          <small>第 {stage.index} 档</small>
          <span>{stage.succeeded}/{stage.requests} 成功</span>
          <span>TTFT P95 {fmt(stage.ttft_p95_ms, 0)} ms</span>
          <Verdict status={stage.status}>{stage.status === 'passed' ? '达标' : stage.status === 'failed' ? '未达标' : '无数据'}</Verdict>
        </div>
      ))}
    </div>
  );
}

function SingleRun({ run }: { run: LabRun }) {
  const hasContract = Boolean(run.contract_summary);
  const hasLoad = run.kind === 'load';
  const hasQuality = Boolean(run.quality);
  const hasFindings = run.findings.some((finding) => finding.phase === 'rpm' || finding.phase === 'tpm');
  return (
    <div className="run-detail">
      <header className="run-detail-head">
        <div>
          <strong>{run.settings.label}</strong>
          <small>{KIND_LABEL[run.kind] || run.kind}</small>
        </div>
        <Verdict status={loadCircuitStopped(run) ? 'failed' : run.status}>{loadCircuitStopped(run) ? '熔断中止' : undefined}</Verdict>
      </header>
      <RunMeta run={run} />
      <AiDiagnosePanel run={run} embedded />
      {hasContract && <ContractDetail key={run.run_id} run={run} />}
      {hasLoad && <LoadResults run={run} />}
      {hasQuality && <QualityDetail run={run} />}
      {!hasContract && !hasLoad && !hasQuality && hasFindings && <FindingDetail run={run} />}
      {!hasContract && !hasLoad && !hasQuality && !hasFindings && (
        <>
          {run.message && <p className="run-detail-msg">{run.message}</p>}
          <StagesDetail run={run} />
        </>
      )}
    </div>
  );
}

function listHint(run: LabRun): string {
  if (run.contract_summary) {
    const s = run.contract_summary;
    const evidenced = s.conformed + s.violated;
    return evidenced > 0 ? `保真度 ${fmt(s.fidelity)}%` : stateLabel(run.status);
  }
  if (loadCircuitStopped(run)) return '熔断中止';
  if (run.load?.peak_rpm != null) return `${fmt(run.load.peak_rpm, 0)} RPM`;
  if (run.quality) return `准确率 ${fmt(run.quality.accuracy)}%`;
  return stateLabel(run.status);
}

export function History() {
  const { runs, selectedId, setSelectedId } = useLab();
  const detail = runs.find((run) => run.run_id === selectedId) ?? null;

  return (
    <>
      <p className="view-sub">点击左侧记录查看详情。</p>
      <div className="split history">
        <Card title="实验记录" desc={`共 ${runs.length} 份`}>
          <div className="history-list">
            {runs.length ? runs.map((run) => (
              <button
                key={run.run_id}
                type="button"
                className={`history-row ${run.run_id === selectedId ? 'selected' : ''}`}
                onClick={() => setSelectedId(run.run_id)}
              >
                <span className="history-row-main">
                  <strong>{run.settings.label}</strong>
                  <small>{KIND_LABEL[run.kind] || run.kind} · {dateTime(run.created_at)}</small>
                </span>
                <span className="history-row-side">{listHint(run)}</span>
              </button>
            )) : <Hint>还没有实验，首次结果会自动保存在这里。</Hint>}
          </div>
        </Card>

        <Card title="记录详情" desc={detail ? (loadCircuitStopped(detail) ? '熔断中止' : stateLabel(detail.status)) : '点击左侧任一记录查看详情。'}>
          {detail ? <SingleRun run={detail} /> : <Hint>还没有选中的记录。</Hint>}
        </Card>
      </div>
    </>
  );
}

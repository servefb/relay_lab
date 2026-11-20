import { useEffect, useMemo, useState } from 'react';

import { Select } from '../components/ui';
import { PROTOCOL_LABEL, PROTOCOL_SHORT, exportMatrixMarkdown, fmt, stateLabel } from '../format';
import { useLab } from '../store';
import type { CheckScore, ContractItem, LabRun, LabStage, Protocol } from '../types';

type Dim = 'speed' | 'parallel' | 'capacity' | 'quality' | 'contract';
type SortKey = 'recent' | Dim;
type SortDir = 'asc' | 'desc';
type Perspective = 'site' | 'model';

const DIMS: { id: Dim; label: string; hint: string }[] = [
  { id: 'speed', label: '速度', hint: 'TTFT P50，越低越好' },
  { id: 'parallel', label: '并行', hint: '峰值输出 TPM' },
  { id: 'capacity', label: '容量', hint: '合格 RPM / 吞吐峰值' },
  { id: 'quality', label: '质量', hint: '准确率' },
  { id: 'contract', label: '协议', hint: '保真度，无高危才算最优' },
];

const PROTOCOLS: Protocol[] = ['openai', 'openai_responses', 'anthropic', 'gemini'];
const SERIES = ['#141414', '#816df8', '#0f9d6e'];

interface Cell {
  run: LabRun | null;
  value: number | null;
  primary: string;
  secondary: string;
  delta: number | null;
  highSeverity: number;
  tags: string[];
}

interface MatrixRow {
  key: string;
  host: string;
  model: string;
  url: string;
  protocol: Protocol;
  protocols: string[];
  recency: string;
  cells: Record<Dim, Cell>;
  /** Oldest → newest protocol fidelity, for the tiny trend line. */
  contractTrend: number[];
}

const EMPTY: Cell = {
  run: null, value: null, primary: '—', secondary: '', delta: null, highSeverity: 0, tags: [],
};

function hostOf(url: string | undefined): string {
  const raw = (url || '').trim();
  if (!raw) return '未知站点';
  try {
    return new URL(raw).host || raw;
  } catch {
    return raw.replace(/^https?:\/\//, '').split('/')[0] || '未知站点';
  }
}

function protocolLabel(protocol: string): string {
  return PROTOCOL_SHORT[protocol] || PROTOCOL_LABEL[protocol] || protocol || '—';
}

function stageOf(run: LabRun, pick: (stage: LabStage) => boolean): LabStage | undefined {
  return run.stages.find(pick) ?? run.stages[0];
}

function cell(
  run: LabRun | null,
  value: number | null,
  primary: string,
  secondary: string,
  delta: number | null = null,
  highSeverity = 0,
  tags: string[] = [],
): Cell {
  if (!run || value == null || !Number.isFinite(value)) return EMPTY;
  return { run, value, primary, secondary, delta, highSeverity, tags };
}

function hotCheckNames(run: LabRun | null): string[] {
  if (!run?.contract?.length) return [];
  const names = new Set<string>();
  for (const item of run.contract) {
    if (item.severity === 'high' && item.verdict === 'violated') names.add(item.title || item.check);
  }
  return [...names];
}

function speedCell(latest: LabRun | null): Cell {
  if (!latest) return EMPTY;
  const stage = stageOf(latest, (item) => item.ttft_p50_ms != null);
  const p50 = stage?.ttft_p50_ms ?? null;
  const p95 = stage?.ttft_p95_ms;
  return cell(latest, p50, p50 == null ? '—' : fmt(p50, 0), p95 != null ? `P95 ${fmt(p95, 0)} ms` : 'TTFT P50');
}

function parallelCell(latest: LabRun | null): Cell {
  if (!latest) return EMPTY;
  let best: LabStage | null = null;
  for (const stage of latest.stages) {
    if (stage.output_tpm == null) continue;
    if (!best || stage.output_tpm > (best.output_tpm ?? -1)) best = stage;
  }
  const concurrency = best?.mean_concurrency ?? best?.peak_concurrency;
  return cell(
    latest,
    best?.output_tpm ?? null,
    best?.output_tpm != null ? fmt(best.output_tpm, 0) : '—',
    concurrency != null ? `达成并发 ${fmt(concurrency, 1)}` : '峰值输出 TPM',
  );
}

function capacityCell(latest: LabRun | null): Cell {
  if (!latest) return EMPTY;
  if (latest.kind === 'capacity') {
    const finding = latest.findings.find((item) => item.phase === 'rpm');
    const qualified = finding?.status === 'bounded' || finding?.status === 'lower_bound';
    return cell(
      latest,
      finding?.value ?? null,
      finding?.value != null ? fmt(finding.value, 0) : '—',
      qualified ? '合格容量' : '容量结论',
    );
  }
  const suggested = latest.load?.suggested_rpm;
  const peak = latest.load?.peak_rpm;
  const value = suggested ?? peak;
  if (value != null && value > 0) {
    return cell(latest, value, fmt(value, 0), suggested != null ? '吞吐折算' : '吞吐峰值');
  }
  return EMPTY;
}

function qualityCell(latest: LabRun | null): Cell {
  const quality = latest?.quality;
  if (!latest || !quality || quality.total <= 0) return EMPTY;
  return cell(latest, quality.accuracy, `${fmt(quality.accuracy)}%`, `${quality.passed}/${quality.total}`);
}

function contractCell(latest: LabRun | null, previous: LabRun | null): Cell {
  const summary = latest?.contract_summary;
  const evidenced = summary ? summary.conformed + summary.violated : 0;
  if (!latest || !summary || evidenced <= 0) return EMPTY;
  const prior = previous?.contract_summary;
  const priorEvidence = prior ? prior.conformed + prior.violated : 0;
  const delta = prior && priorEvidence > 0 ? summary.fidelity - prior.fidelity : null;
  return cell(
    latest,
    summary.fidelity,
    `${fmt(summary.fidelity)}%`,
    `${summary.conformed}/${evidenced}`,
    delta,
    summary.high_severity,
    hotCheckNames(latest),
  );
}

function latestPair(runs: LabRun[], accept: (run: LabRun) => boolean): [LabRun | null, LabRun | null] {
  const hits = runs.filter(accept);
  return [hits[0] ?? null, hits[1] ?? null];
}

function buildRows(runs: LabRun[], site: string, protocol: string, query: string): MatrixRow[] {
  const buckets = new Map<string, LabRun[]>();
  for (const run of runs) {
    if (protocol !== 'all' && run.settings.protocol !== protocol) continue;
    const model = run.settings.model?.trim() || '未记模型';
    if (query && !model.toLowerCase().includes(query.trim().toLowerCase())) continue;
    const host = hostOf(run.settings.url);
    if (site !== 'all' && host !== site) continue;
    const key = `${host}\n${model}`;
    const list = buckets.get(key) ?? [];
    list.push(run);
    buckets.set(key, list);
  }
  const rows: MatrixRow[] = [];
  for (const [key, scoped] of buckets) {
    const [host, model] = key.split('\n');
    const newest = scoped[0];
    const protocols = [...new Set(scoped.map((run) => run.settings.protocol).filter(Boolean))];
    const [speed] = latestPair(scoped, (run) => run.kind === 'speed' && speedCell(run).value != null);
    const [parallel] = latestPair(scoped, (run) => run.kind === 'parallel' && parallelCell(run).value != null);
    const [capacity] = latestPair(scoped, (run) => (
      (run.kind === 'capacity' || run.kind === 'load') && capacityCell(run).value != null
    ));
    const [quality] = latestPair(scoped, (run) => run.kind === 'quality' && qualityCell(run).value != null);
    const [contract, contractPrev] = latestPair(scoped, (run) => run.kind === 'contract' && contractCell(run, null).value != null);
    const cells: Record<Dim, Cell> = {
      speed: speedCell(speed),
      parallel: parallelCell(parallel),
      capacity: capacityCell(capacity),
      quality: qualityCell(quality),
      contract: contractCell(contract, contractPrev),
    };
    const recency = scoped.reduce((best, run) => (run.created_at > best ? run.created_at : best), '');
    const contractTrend = scoped
      .filter((run) => run.kind === 'contract')
      .map((run) => contractCell(run, null).value)
      .filter((value): value is number => value != null)
      .reverse();
    rows.push({
      key,
      host,
      model,
      url: newest?.settings.url || '',
      protocol: (newest?.settings.protocol || 'openai') as Protocol,
      protocols,
      recency,
      cells,
      contractTrend,
    });
  }
  return rows;
}

function dimensionScores(rows: MatrixRow[]): Map<string, Record<Dim, number | null>> {
  const ranges = {} as Record<Dim, { min: number; max: number }>;
  for (const dim of DIMS) {
    const values = rows.map((row) => row.cells[dim.id].value).filter((value): value is number => value != null);
    ranges[dim.id] = {
      min: values.length ? Math.min(...values) : 0,
      max: values.length ? Math.max(...values) : 0,
    };
  }
  const scores = new Map<string, Record<Dim, number | null>>();
  for (const row of rows) {
    const next = {} as Record<Dim, number | null>;
    for (const dim of DIMS) {
      const value = row.cells[dim.id].value;
      if (value == null) {
        next[dim.id] = null;
        continue;
      }
      // 百分比维度（质量准确率、协议保真度）单条或同值时直接用真实百分比，避免把低分渲染成外圈满分
      if (dim.id === 'quality' || dim.id === 'contract') {
        next[dim.id] = Math.max(0, Math.min(100, Math.round(value)));
        continue;
      }
      const { min, max } = ranges[dim.id];
      if (max === min) {
        next[dim.id] = 85;
        continue;
      }
      const ratio = (value - min) / (max - min);
      const norm = dim.id === 'speed' ? 1 - ratio : ratio;
      // 已测最低值保留 30% 可见半径，和落在中心的“未测”区分开
      next[dim.id] = Math.round(30 + norm * 70);
    }
    scores.set(row.key, next);
  }
  return scores;
}

function coverage(row: MatrixRow): number {
  return DIMS.filter((dim) => row.cells[dim.id].value != null).length;
}

function chartSubjects(rows: MatrixRow[], pickedRows: MatrixRow[]): MatrixRow[] {
  if (pickedRows.length) return pickedRows;
  return [...rows].sort((left, right) => coverage(right) - coverage(left) || right.recency.localeCompare(left.recency))
    .filter((row) => coverage(row) > 0)
    .slice(0, 3);
}

function compareValues(left: number | null, right: number | null, dir: SortDir): number {
  if (left == null && right == null) return 0;
  if (left == null) return 1;
  if (right == null) return -1;
  return dir === 'asc' ? left - right : right - left;
}

function sortRows(rows: MatrixRow[], key: SortKey, dir: SortDir): MatrixRow[] {
  const next = [...rows];
  next.sort((left, right) => {
    if (key === 'recent') return right.recency.localeCompare(left.recency);
    const delta = compareValues(left.cells[key].value, right.cells[key].value, dir);
    if (delta !== 0) return delta;
    return left.model.localeCompare(right.model, 'zh') || left.host.localeCompare(right.host);
  });
  return next;
}

function bestKeys(rows: MatrixRow[]): Record<Dim, Set<string>> {
  const marks = {} as Record<Dim, Set<string>>;
  for (const dim of DIMS) {
    const marksForDim = new Set<string>();
    const eligible = rows.filter((row) => {
      const value = row.cells[dim.id].value;
      if (value == null) return false;
      if (dim.id === 'contract' && row.cells.contract.highSeverity > 0) return false;
      return true;
    });
    if (eligible.length) {
      const winner = eligible.reduce((best, row) => {
        const value = row.cells[dim.id].value ?? 0;
        const lead = best.cells[dim.id].value ?? 0;
        if (dim.id === 'speed') return value < lead ? row : best;
        return value > lead ? row : best;
      });
      const target = winner.cells[dim.id].value;
      for (const row of eligible) {
        if (row.cells[dim.id].value === target) marksForDim.add(row.key);
      }
    }
    marks[dim.id] = marksForDim;
  }
  return marks;
}

function scoreText(score: CheckScore | undefined): string {
  if (!score || score.total <= 0) return '—';
  if (score.violated > 0) return '被改写';
  if (score.conformed > 0) return '通过';
  if ((score.observation ?? 0) > 0) return '观察';
  return '无法判定';
}

function checkVerdicts(run: LabRun): Map<string, string> {
  const verdicts = new Map<string, string>();
  if (run.contract?.length) {
    const grouped = new Map<string, ContractItem[]>();
    for (const item of run.contract) {
      const key = item.protocol ? `${item.protocol}/${item.check}` : item.check;
      const list = grouped.get(key) ?? [];
      list.push(item);
      grouped.set(key, list);
    }
    for (const [key, items] of grouped) {
      if (items.some((item) => item.verdict === 'violated')) verdicts.set(key, '被改写');
      else if (items.some((item) => item.verdict === 'conformed')) verdicts.set(key, '通过');
      else if (items.some((item) => item.verdict === 'observation')) verdicts.set(key, '观察');
      else verdicts.set(key, '无法判定');
    }
    return verdicts;
  }
  for (const [key, score] of Object.entries(run.contract_summary?.by_check ?? {})) {
    verdicts.set(key, scoreText(score));
  }
  return verdicts;
}

function checkTitle(key: string, runs: LabRun[]): string {
  const check = key.includes('/') ? key.slice(key.indexOf('/') + 1) : key;
  for (const run of runs) {
    const hit = run.contract?.find((item) => {
      const id = item.protocol ? `${item.protocol}/${item.check}` : item.check;
      return id === key;
    });
    if (hit?.title) return hit.title;
  }
  return check;
}

function signed(value: number, unit: string, digits = 0): string {
  const shown = fmt(Math.abs(value), digits);
  if (Math.abs(value) < (digits === 0 ? 0.5 : 0.05)) return `0 ${unit}`;
  return `${value > 0 ? '+' : '−'}${shown} ${unit}`;
}

function pageOf(run: LabRun): string {
  if (run.kind === 'load') return 'capacity';
  if (run.kind === 'speed' || run.kind === 'parallel' || run.kind === 'capacity' || run.kind === 'quality' || run.kind === 'contract') {
    return run.kind;
  }
  return 'history';
}

function isRisk(run: LabRun): boolean {
  if (run.status === 'error' || run.status === 'interrupted' || run.status === 'stopped') return true;
  return (run.contract_summary?.high_severity ?? 0) > 0;
}

export function Overview({ onNavigate }: { onNavigate: (view: string) => void }) {
  const { runs, schedules, active, setSelectedId, prefillConnection, stopActive } = useLab();
  const [site, setSite] = useState('all');
  const [protocol, setProtocol] = useState('all');
  const [query, setQuery] = useState('');
  const [perspective, setPerspective] = useState<Perspective>('site');
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDir }>({ key: 'recent', dir: 'desc' });
  const [picked, setPicked] = useState<string[]>([]);
  const [diffOnly, setDiffOnly] = useState(true);
  const [feed, setFeed] = useState<'all' | 'risk'>('all');
  const [measuredOnly, setMeasuredOnly] = useState(false);
  const [board, setBoard] = useState<'matrix' | 'heat' | 'recent'>('matrix');

  const sites = useMemo(() => {
    const names = new Set(runs.map((run) => hostOf(run.settings.url)));
    return [...names].sort((left, right) => left.localeCompare(right));
  }, [runs]);

  const rows = useMemo(
    () => sortRows(buildRows(runs, site, protocol, query), sort.key, sort.dir),
    [runs, site, protocol, query, sort],
  );
  const marks = useMemo(() => bestKeys(rows), [rows]);
  const pickedRows = picked.map((key) => rows.find((row) => row.key === key)).filter((row): row is MatrixRow => !!row);
  const plotted = useMemo(() => chartSubjects(rows, pickedRows), [rows, pickedRows]);
  const filledDims = DIMS.filter((dim) => rows.some((row) => row.cells[dim.id].value != null));
  const visibleDims = DIMS.filter((dim) => !measuredOnly || filledDims.some((item) => item.id === dim.id));

  const siteCount = new Set(rows.map((row) => row.host)).size;
  const modelCount = new Set(rows.map((row) => row.model)).size;
  const riskyObjects = rows.filter((row) => row.cells.contract.highSeverity > 0).length;
  const scopeLabel = site === 'all' && protocol === 'all' && !query.trim() ? '全站' : '当前筛选';
  const missingKeys = schedules.filter((item) => item.needs_key).length;
  const capped = runs.length >= 50;

  const groups = useMemo(() => {
    if (perspective !== 'model') return [{ model: '', rows }];
    const order: { model: string; rows: MatrixRow[] }[] = [];
    const index = new Map<string, number>();
    for (const row of rows) {
      const at = index.get(row.model);
      if (at == null) {
        index.set(row.model, order.length);
        order.push({ model: row.model, rows: [row] });
      } else {
        order[at].rows.push(row);
      }
    }
    return order;
  }, [perspective, rows]);

  const toggleSort = (key: SortKey) => {
    setSort((current) => {
      if (current.key !== key) return { key, dir: key === 'speed' ? 'asc' : 'desc' };
      return { key, dir: current.dir === 'asc' ? 'desc' : 'asc' };
    });
  };

  const togglePick = (key: string, on: boolean) => {
    setPicked((current) => {
      if (!on) return current.filter((item) => item !== key);
      if (current.includes(key) || current.length >= 3) return current;
      return [...current, key];
    });
  };

  const goTest = (row: MatrixRow, dim: Dim) => {
    prefillConnection({ url: row.url, protocol: row.protocol, model: row.model });
    onNavigate(dim);
  };

  const openRun = (run: LabRun) => {
    setSelectedId(run.run_id);
    onNavigate('history');
  };

  const exportTable = () => {
    exportMatrixMarkdown(rows.map((row) => ({
      model: row.model,
      host: row.host,
      protocol: row.protocols.map(protocolLabel).join(' · '),
      speed: row.cells.speed.value == null ? '—' : `${row.cells.speed.primary} ms`,
      parallel: row.cells.parallel.value == null ? '—' : row.cells.parallel.primary,
      capacity: row.cells.capacity.value == null ? '—' : row.cells.capacity.primary,
      quality: row.cells.quality.primary,
      contract: row.cells.contract.value == null
        ? '—'
        : `${row.cells.contract.primary}${row.cells.contract.highSeverity ? `（高危 ${row.cells.contract.highSeverity}）` : ''}`,
    })), capped ? '基于最近 50 次实验。空白表示这 50 次里没有该项的有效结果。' : '空白表示还没有该项的有效结果。');
  };

  const recent = runs.filter((run) => feed === 'all' || isRisk(run)).slice(0, 6);
  const compareNote = compareCaveat(pickedRows);

  return (
    <div className="overview">
      <section className="overview-status" data-live={active ? '1' : '0'}>
        <h1>概览</h1>
        {active ? (
          <>
            <span className="overview-live-dot" />
            <div className="overview-status-copy">
              <strong>{active.settings.label}</strong>
              <small>{active.settings.model || '未记模型'} · {active.message || stateLabel(active.status)}</small>
            </div>
            <button type="button" className="pill" onClick={() => onNavigate(pageOf(active))}>查看实时页</button>
            <button type="button" className="btn danger" onClick={() => { void stopActive(); }}>停止实验</button>
          </>
        ) : (
          <>
            <p>
              <span className="overview-scope">{scopeLabel}</span>
              站点 <b>{siteCount}</b>
              <span> · </span>
              模型 <b>{modelCount}</b>
              <span> · </span>
              存在高危 <b data-hot={riskyObjects > 0 ? '1' : '0'}>{riskyObjects}</b> 个对象
            </p>
            {missingKeys > 0 && <span className="overview-chip">定时任务缺密钥 {missingKeys}</span>}
            <span className="overview-status-gap" />
            <button type="button" className="pill" onClick={() => onNavigate('speed')}>速度</button>
            <button type="button" className="pill" onClick={() => onNavigate('capacity')}>容量</button>
            <button type="button" className="pill" onClick={() => onNavigate('contract')}>协议</button>
          </>
        )}
      </section>

      <section className="overview-toolbar">
        <Select
          size="compact"
          ariaLabel="站点"
          value={site}
          onChange={(next) => { setSite(next); setPicked([]); }}
          options={[
            { value: 'all', label: '全部站点' },
            ...sites.map((name) => ({ value: name, label: name })),
          ]}
        />
        <Select
          size="compact"
          ariaLabel="协议"
          value={protocol}
          onChange={(next) => { setProtocol(next); setPicked([]); }}
          options={[
            { value: 'all', label: '全部协议' },
            ...PROTOCOLS.map((name) => ({ value: name, label: protocolLabel(name) })),
          ]}
        />
        <input
          className="overview-search"
          type="text"
          aria-label="搜索模型"
          value={query}
          placeholder="搜索模型…"
          onChange={(event) => { setQuery(event.target.value); setPicked([]); }}
        />
        <div className="overview-perspective" role="group" aria-label="聚合视角">
          <button type="button" aria-pressed={perspective === 'site'} onClick={() => setPerspective('site')}>按站点 × 模型</button>
          <button type="button" aria-pressed={perspective === 'model'} onClick={() => setPerspective('model')}>同模型比站点</button>
        </div>
        <button type="button" className="pill" aria-pressed={measuredOnly} onClick={() => setMeasuredOnly((value) => !value)}>仅看已测维度</button>
        <button type="button" className="pill" disabled={!rows.length} onClick={exportTable}>导出 Markdown</button>
      </section>

      <div className={`overview-main ${board === 'matrix' ? 'has-radar' : ''}`}>
        <section className="overview-board">
          <header className="overview-board-head">
            <div className="overview-perspective" role="group" aria-label="主视图">
              <button type="button" aria-pressed={board === 'matrix'} onClick={() => setBoard('matrix')}>五维矩阵</button>
              <button type="button" aria-pressed={board === 'heat'} onClick={() => setBoard('heat')}>协议热力</button>
              <button type="button" aria-pressed={board === 'recent'} onClick={() => setBoard('recent')}>最近实验</button>
            </div>
            {capped && <small className="overview-cap">基于最近 50 次实验</small>}
          </header>
          {board === 'matrix' && (
            rows.length ? (
              <div className="overview-matrix-scroll">
                <table className="overview-matrix">
                  <thead>
                    <tr>
                      <th className="matrix-check" />
                      <th>模型 / 中转站</th>
                      {visibleDims.map((dim) => {
                        const emptyCol = !filledDims.some((item) => item.id === dim.id);
                        return (
                          <th key={dim.id} data-empty={emptyCol ? '1' : '0'} data-rich={dim.id === 'contract' && !emptyCol ? '1' : '0'}>
                            <button
                              type="button"
                              onClick={() => toggleSort(dim.id)}
                              aria-sort={sort.key === dim.id ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
                              title={dim.hint}
                            >
                              {dim.label}
                              {sort.key === dim.id ? (sort.dir === 'asc' ? ' ↑' : ' ↓') : ''}
                            </button>
                          </th>
                        );
                      })}
                    </tr>
                  </thead>
                  <tbody>
                    {groups.map((group) => (
                      <GroupRows
                        key={group.model || 'all'}
                        group={group}
                        showHeader={perspective === 'model'}
                        marks={marks}
                        dims={visibleDims}
                        picked={picked}
                        onPick={togglePick}
                        onOpen={openRun}
                        onTest={goTest}
                      />
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="overview-matrix-empty">
                {runs.length
                  ? '这组筛选下没有实验。换个站点或清掉搜索后再看。'
                  : '还没有实验。从速度、容量或协议页发出第一次请求后，横评表会出现在这里。'}
              </p>
            )
          )}
          {board === 'heat' && <CheckHeatmap rows={rows} onOpen={openRun} />}
          {board === 'recent' && (
            <>
              <div className="overview-feed-tools">
                <button type="button" className="pill" aria-pressed={feed === 'all'} onClick={() => setFeed('all')}>全部</button>
                <button type="button" className="pill" aria-pressed={feed === 'risk'} onClick={() => setFeed('risk')}>仅异常与高危</button>
                <button type="button" className="link" onClick={() => onNavigate('history')}>全部记录 →</button>
              </div>
              {recent.length ? (
                <ol className="overview-timeline">
                  {recent.map((run) => (
                    <li key={run.run_id}>
                      <button type="button" className="overview-row" onClick={() => openRun(run)}>
                        <span className="overview-dot" data-status={run.status} />
                        <span className="overview-row-main">
                          <strong>{run.settings.label}</strong>
                          <small>{hostOf(run.settings.url)}{run.settings.model ? ` · ${run.settings.model}` : ''}</small>
                        </span>
                        <span className="overview-row-metric">
                          <b>{stateLabel(run.status)}</b>
                          <small>{(run.contract_summary?.high_severity ?? 0) > 0 ? `高危 ${run.contract_summary?.high_severity}` : protocolLabel(run.settings.protocol)}</small>
                        </span>
                      </button>
                    </li>
                  ))}
                </ol>
              ) : <p className="overview-matrix-empty">没有符合这个过滤的实验。</p>}
            </>
          )}
        </section>

        {board === 'matrix' && (
          <aside className="overview-radar-aside">
            <RadarBoard rows={plotted} hinted={!pickedRows.length} />
          </aside>
        )}
      </div>

      {pickedRows.length >= 2 && (
        <ComparePanel rows={pickedRows} diffOnly={diffOnly} onDiffOnly={setDiffOnly} note={compareNote} />
      )}
    </div>
  );
}

function compareCaveat(rows: MatrixRow[]): string {
  if (rows.length < 2) return '';
  const notes: string[] = [];
  const modes = new Set(rows.map((row) => row.cells.contract.run?.settings.mode).filter(Boolean));
  if (modes.size > 1) notes.push('协议检查模式不一致（快速 / 完整），只比较两边都覆盖到的检查项。');
  const stamps = rows.flatMap((row) => DIMS.map((dim) => row.cells[dim.id].run?.created_at)).filter((stamp): stamp is string => !!stamp);
  if (stamps.length >= 2) {
    const oldest = stamps.reduce((best, stamp) => (stamp < best ? stamp : best));
    const newest = stamps.reduce((best, stamp) => (stamp > best ? stamp : best));
    const days = (Date.parse(newest) - Date.parse(oldest)) / 86_400_000;
    if (Number.isFinite(days) && days >= 7) notes.push(`记录前后相差约 ${Math.round(days)} 天，口径仅供参考。`);
  }
  return notes.join(' ');
}

function RadarBoard({ rows, hinted }: { rows: MatrixRow[]; hinted: boolean }) {
  const scores = dimensionScores(rows);
  const width = 340;
  const height = 276;
  const cx = 170;
  const cy = 146;
  const radius = 94;
  const rings = [0.25, 0.5, 0.75, 1];
  const angle = (index: number) => -Math.PI / 2 + (index * 2 * Math.PI) / DIMS.length;
  const at = (index: number, scale: number) => {
    const theta = angle(index);
    return [cx + Math.cos(theta) * radius * scale, cy + Math.sin(theta) * radius * scale];
  };
  const ring = (scale: number) => DIMS.map((_, index) => at(index, scale).map((value) => value.toFixed(1)).join(',')).join(' ');
  const hasData = rows.length > 0;
  return (
    <div className="radar-card overview-radar">
      <div className="radar-copy-head">
        <h3>五维轮廓</h3>
        <span>{hasData ? (hinted ? '覆盖最全前 3' : `对比 ${rows.length} 项`) : '待观测'}</span>
      </div>

      <svg className="radar" viewBox={`0 0 ${width} ${height}`} role="img" aria-label="五维轮廓">
        {rings.map((scale) => (
          <polygon key={scale} points={ring(scale)} className={scale === 1 ? 'radar-ring outer' : 'radar-ring'} />
        ))}
        {DIMS.map((dim, index) => {
          const [x, y] = at(index, 1);
          const [lx, ly] = at(index, 1.24);
          const anchor = lx < cx - 8 ? 'end' : lx > cx + 8 ? 'start' : 'middle';
          return (
            <g key={dim.id}>
              <line x1={cx} y1={cy} x2={x} y2={y} className="radar-axis" />
              <text x={lx} y={ly} textAnchor={anchor} dominantBaseline="middle" className="radar-label">{dim.label}</text>
            </g>
          );
        })}
        {rows.map((row, series) => {
          const color = SERIES[series % SERIES.length];
          const values = DIMS.map((dim) => scores.get(row.key)?.[dim.id] ?? 0);
          const points = values.map((score, index) => at(index, Math.max(score, 0) / 100));
          const path = points.map((point) => point.map((value) => value.toFixed(1)).join(',')).join(' ');
          return (
            <g key={row.key}>
              <polygon points={path} fill={color} fillOpacity={0.14} stroke={color} strokeWidth={1.8} />
              {points.map(([x, y], index) => {
                const measured = scores.get(row.key)?.[DIMS[index].id] != null;
                return (
                  <circle
                    key={DIMS[index].id}
                    cx={x}
                    cy={y}
                    r={measured ? 3.4 : 2.3}
                    fill={measured ? color : '#fff'}
                    stroke={color}
                    strokeWidth={1.35}
                  />
                );
              })}
            </g>
          );
        })}
      </svg>

      {hasData && (
        <ul className="radar-legend">
          {rows.map((row, index) => (
            <li key={row.key}>
              <i style={{ background: SERIES[index % SERIES.length] }} />
              <span className="radar-legend-meta">
                <strong>{row.model}</strong>
                <small>{row.host}</small>
              </span>
              <em className="radar-legend-cov">{coverage(row)}/5 维</em>
            </li>
          ))}
        </ul>
      )}

      <p className="radar-footnote">
        {hasData
          ? '越靠外越优；速度按延迟反转，未测落于中心。勾选左侧行可实时叠图对比。'
          : '完成任一维度实验后，这里将自动绘制模型渠道的五维轮廓。'}
      </p>
    </div>
  );
}

function GroupRows({
  group, showHeader, marks, dims, picked, onPick, onOpen, onTest,
}: {
  group: { model: string; rows: MatrixRow[] };
  showHeader: boolean;
  marks: Record<Dim, Set<string>>;
  dims: { id: Dim; label: string; hint: string }[];
  picked: string[];
  onPick: (key: string, on: boolean) => void;
  onOpen: (run: LabRun) => void;
  onTest: (row: MatrixRow, dim: Dim) => void;
}) {
  return (
    <>
      {showHeader && (
        <tr className="matrix-group">
          <td colSpan={dims.length + 2}>{group.model}</td>
        </tr>
      )}
      {group.rows.map((row) => {
        const checked = picked.includes(row.key);
        const baseline = picked[0] === row.key;
        return (
          <tr key={row.key} data-picked={checked ? '1' : '0'}>
            <td className="matrix-check">
              <input
                type="checkbox"
                aria-label={`对比 ${row.model} @ ${row.host}`}
                checked={checked}
                disabled={!checked && picked.length >= 3}
                onChange={(event) => onPick(row.key, event.target.checked)}
              />
            </td>
            <td className="matrix-who">
              <strong>{row.model}</strong>
              {baseline && <em>基准</em>}
              <small>
                <em className="matrix-host">{row.host}</em>
                {row.protocols.map(protocolLabel).join(' · ') || protocolLabel(row.protocol)}
              </small>
            </td>
            {dims.map((dim) => (
              <MatrixCell
                key={dim.id}
                dim={dim.id}
                cell={row.cells[dim.id]}
                trend={dim.id === 'contract' ? row.contractTrend : []}
                best={marks[dim.id].has(row.key)}
                onOpen={onOpen}
                onTest={() => onTest(row, dim.id)}
              />
            ))}
          </tr>
        );
      })}
    </>
  );
}

function Spark({ values }: { values: number[] }) {
  if (values.length < 2) return null;
  const width = 52;
  const height = 16;
  const min = Math.min(...values);
  const max = Math.max(...values);
  const x = (index: number) => (index / (values.length - 1)) * width;
  const y = (value: number) => (max === min ? height / 2 : height - 2 - ((value - min) / (max - min)) * (height - 4));
  const rising = values[values.length - 1] >= values[0];
  return (
    <svg className="matrix-spark" viewBox={`0 0 ${width} ${height}`} data-up={rising ? '1' : '0'} aria-hidden="true">
      <polyline fill="none" points={values.map((value, index) => `${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(' ')} />
    </svg>
  );
}

function MatrixCell({
  dim, cell: item, trend, best, onOpen, onTest,
}: {
  dim: Dim;
  cell: Cell;
  trend: number[];
  best: boolean;
  onOpen: (run: LabRun) => void;
  onTest: () => void;
}) {
  const empty = item.value == null || !item.run;
  if (empty) {
    return (
      <td className="matrix-metric" data-empty="1">
        <span className="matrix-dash" aria-hidden="true">—</span>
        <button type="button" className="matrix-go" onClick={onTest}>+ 去测</button>
      </td>
    );
  }
  const delta = dim === 'contract' && item.delta != null && Math.abs(item.delta) >= 0.05 ? item.delta : null;
  if (dim === 'contract') {
    return (
      <td className="matrix-metric" data-best={best ? '1' : '0'} data-rich="1">
        <button type="button" className="matrix-value" onClick={() => item.run && onOpen(item.run)}>
          <span className="matrix-proto">
            <span>
              <span className="matrix-line">
                <b>{item.primary}</b>
                {best && <i className="matrix-mark" title="当前最优" />}
                {delta != null && (
                  <small className="matrix-delta" data-down={delta < 0 ? '1' : '0'}>
                    {delta < 0 ? '↓' : '↑'}{fmt(Math.abs(delta), 1)}%
                  </small>
                )}
              </span>
              <span className="matrix-line">
                {item.secondary && <small>{item.secondary}</small>}
                {item.highSeverity > 0 && (
                  <span className="matrix-badge" title={item.tags.join('\n') || '高危检查'}>{item.highSeverity} 高危</span>
                )}
              </span>
            </span>
            <Spark values={trend} />
          </span>
        </button>
      </td>
    );
  }
  return (
    <td className="matrix-metric" data-best={best ? '1' : '0'}>
      <button type="button" className="matrix-value" onClick={() => item.run && onOpen(item.run)}>
        <span className="matrix-line">
          <b>
            {item.primary}
            {dim === 'speed' ? <i> ms</i> : null}
          </b>
          {best && <i className="matrix-mark" title="当前最优" />}
        </span>
        {item.secondary && <small>{item.secondary}</small>}
      </button>
    </td>
  );
}

const HEAT_PAGE_SIZE = 12;

function CheckHeatmap({ rows, onOpen }: { rows: MatrixRow[]; onOpen: (run: LabRun) => void }) {
  const [page, setPage] = useState(0);
  const present = rows.filter((row) => row.cells.contract.run);
  const runs = present.map((row) => row.cells.contract.run).filter((run): run is LabRun => !!run);
  const maps = runs.map((run) => checkVerdicts(run));
  const columns = heatmapColumns(runs, maps);
  const shown = present.flatMap((row, index) => {
    const useful = columns.some((column) => {
      const verdict = maps[index]?.get(column.key);
      return verdict === '通过' || verdict === '被改写';
    });
    return useful ? [{ row, verdicts: maps[index] }] : [];
  });
  const pageCount = Math.max(1, Math.ceil(columns.length / HEAT_PAGE_SIZE));
  useEffect(() => {
    setPage((current) => Math.min(current, pageCount - 1));
  }, [pageCount]);
  useEffect(() => {
    setPage(0);
  }, [columns.length]);
  if (!shown.length || !columns.length) {
    return <p className="overview-matrix-empty">当前筛选下的协议结论没有分歧，也没有高危项。</p>;
  }
  const safePage = Math.min(page, pageCount - 1);
  const from = safePage * HEAT_PAGE_SIZE;
  const pageColumns = columns.slice(from, from + HEAT_PAGE_SIZE);
  const to = from + pageColumns.length;
  return (
    <div className="heat-card">
      <header className="heat-card-head">
        <p>勾是通过，淡点是未测或未判定，只有被改写才用红标。同名检查按协议区分。</p>
        {pageCount > 1 && (
          <div className="heat-pager">
            <button type="button" className="pill" disabled={safePage <= 0} onClick={() => setPage(safePage - 1)}>
              上一页
            </button>
            <span>列 {from + 1}–{to} / 共 {columns.length}</span>
            <button type="button" className="pill" disabled={safePage >= pageCount - 1} onClick={() => setPage(safePage + 1)}>
              下一页
            </button>
          </div>
        )}
      </header>
      <div className="overview-matrix-scroll">
        <table className="heat-table">
          <thead>
            <tr>
              <th>模型 / 站点</th>
              {pageColumns.map((column) => <th key={column.key} title={column.key}>{column.title}</th>)}
            </tr>
          </thead>
          <tbody>
            {shown.map(({ row, verdicts }) => (
              <tr key={row.key}>
                <th>
                  <strong>{row.model}</strong>
                  <small>{row.host}</small>
                </th>
                {pageColumns.map((column) => {
                  const verdict = verdicts.get(column.key) ?? '—';
                  const tone = verdict === '通过' ? 'ok' : verdict === '被改写' ? 'bad' : 'muted';
                  return (
                    <td key={column.key}>
                      <button
                        type="button"
                        className={`heat-mark ${tone}`}
                        title={verdict}
                        aria-label={verdict}
                        onClick={() => row.cells.contract.run && onOpen(row.cells.contract.run)}
                      >
                        {tone === 'bad' ? '改写' : tone === 'ok' ? '✓' : '·'}
                      </button>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function heatmapColumns(runs: LabRun[], maps: Map<string, string>[]): { key: string; title: string }[] {
  const keys = [...new Set(maps.flatMap((verdicts) => [...verdicts.keys()]))];
  const ranked = keys.map((key) => {
    const verdicts = maps.map((verdicts) => verdicts.get(key) ?? '—');
    const hot = runs.some((run) => (run.contract ?? []).some((item) => {
      const id = item.protocol ? `${item.protocol}/${item.check}` : item.check;
      return id === key && item.severity === 'high' && item.verdict === 'violated';
    }));
    const failed = verdicts.some((verdict) => verdict === '被改写');
    const disagree = new Set(verdicts).size > 1;
    return { key, title: checkTitle(key, runs), hot, rank: (hot ? 4 : 0) + (disagree ? 2 : 0) + (failed ? 1 : 0) };
  }).filter((column) => column.rank > 0);
  ranked.sort((left, right) => right.rank - left.rank || left.title.localeCompare(right.title, 'zh'));
  const counts = new Map<string, number>();
  for (const column of ranked) counts.set(column.title, (counts.get(column.title) ?? 0) + 1);
  return ranked.map((column) => {
    if ((counts.get(column.title) ?? 0) < 2) return { key: column.key, title: column.title };
    const protocol = column.key.includes('/') ? protocolLabel(column.key.slice(0, column.key.indexOf('/'))) : '';
    const suffix = protocol || column.key;
    return { key: column.key, title: `${column.title} · ${suffix}` };
  });
}

function ComparePanel({
  rows, diffOnly, onDiffOnly, note,
}: {
  rows: MatrixRow[];
  diffOnly: boolean;
  onDiffOnly: (next: boolean) => void;
  note: string;
}) {
  const baseline = rows[0];
  const contracts = rows.map((row) => row.cells.contract.run).filter((run): run is LabRun => !!run);
  const maps = contracts.map((run) => checkVerdicts(run));
  const keys = [...new Set(maps.flatMap((verdicts) => [...verdicts.keys()]))].sort();
  const differing = keys.filter((key) => new Set(maps.map((verdicts) => verdicts.get(key) ?? '—')).size > 1);
  const shown = diffOnly ? differing : keys;
  const allHaveContract = contracts.length === rows.length;

  return (
    <section className="overview-compare">
      <header>
        <h3>勾选对比</h3>
        <p>以第一行为基准，后面每格是相对差值。延迟为负表示更快。</p>
      </header>
      {note && <p className="matrix-note">{note}</p>}
      <div className="overview-matrix-scroll">
        <table className="overview-matrix compare">
          <thead>
            <tr>
              <th>维度</th>
              {rows.map((row) => (
                <th key={row.key}>{row.model}<small>{row.host}</small></th>
              ))}
            </tr>
          </thead>
          <tbody>
            {DIMS.map((dim) => (
              <tr key={dim.id}>
                <th>{dim.label}</th>
                {rows.map((row, index) => {
                  const item = row.cells[dim.id];
                  const base = baseline.cells[dim.id].value;
                  const delta = index > 0 && item.value != null && base != null ? item.value - base : null;
                  const unit = dim.id === 'speed' ? 'ms' : dim.id === 'parallel' ? 'TPM' : dim.id === 'capacity' ? 'RPM' : '%';
                  const digits = dim.id === 'quality' || dim.id === 'contract' ? 1 : 0;
                  return (
                    <td key={row.key}>
                      <b>{item.value == null ? '—' : item.primary}{dim.id === 'speed' && item.value != null ? ' ms' : ''}</b>
                      {delta != null && <small>{signed(delta, unit, digits)}</small>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {allHaveContract ? (
        <div className="matrix-diff">
          <label>
            <input type="checkbox" checked={diffOnly} onChange={(event) => onDiffOnly(event.target.checked)} />
            只看差异项
          </label>
          {!keys.length ? <p className="overview-matrix-empty">这几次协议检查没有分项结论，无法对照具体检查项。</p> : shown.length ? (
            <div className="overview-matrix-scroll">
              <table className="overview-matrix compare">
                <thead>
                  <tr>
                    <th>协议检查</th>
                    {rows.map((row) => <th key={row.key}>{row.host}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {shown.map((key) => (
                    <tr key={key}>
                      <th>{checkTitle(key, contracts)}<small>{key}</small></th>
                      {maps.map((verdicts, index) => {
                        const text = verdicts.get(key) ?? '—';
                        return <td key={rows[index].key} data-verdict={text}>{text}</td>;
                      })}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : <p className="overview-matrix-empty">这几行的协议检查结论一致。</p>}
        </div>
      ) : (
        <p className="matrix-note">有的行还没有协议检查结果，先不对照具体检查项。</p>
      )}
    </section>
  );
}

import { Download } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { FormEvent } from 'react';

import { AiDiagnosePanel } from '../components/AiDiagnosePanel';
import { Card, ConnectionFields, EmptyRow, Field, Hint, Notes, RunFooter, Verdict, ms } from '../components/ui';
import { exportRun, fmt, stateLabel } from '../format';
import { useLab } from '../store';

const CATEGORIES_BY_PACK: Record<'standard' | 'smoke', string[]> = {
  standard: ['STEM', '人文', '社科', '其他'],
  smoke: ['算术', '指令', 'JSON', '中文理解', '代码', '逻辑推理', '多模态'],
};

const PACKS: { id: 'standard' | 'smoke'; label: string; desc: string }[] = [
  { id: 'standard', label: 'MMLU 精选', desc: 'MMLU 多科四选一精选子集（字母判分）' },
  { id: 'smoke', label: '内置冒烟', desc: '原中文手写题包' },
];

export function Quality() {
  const { latest, startRun, basePayload, connection, pendingDraft, clearDraft, showNotice } = useLab();
  const [pack, setPack] = useState<'standard' | 'smoke'>('standard');
  const [categories, setCategories] = useState<string[]>([]);
  const [perCategory, setPerCategory] = useState(8);
  const [parallel, setParallel] = useState(1);
  const [consent, setConsent] = useState(false);

  const run = latest('quality');
  const summary = run?.quality ?? null;

  useEffect(() => {
    if (!pendingDraft || pendingDraft.kind !== 'quality') return;
    const s = pendingDraft.settings;
    if (s.pack === 'standard' || s.pack === 'smoke') setPack(s.pack);
    if (Array.isArray(s.categories)) {
      setCategories(s.categories.filter((item): item is string => typeof item === 'string'));
    }
    if (typeof s.per_category === 'number') setPerCategory(s.per_category);
    if (typeof s.max_parallel === 'number') setParallel(s.max_parallel);
    clearDraft();
    showNotice('已填入 AI 推荐的质量抽查参数，请确认后启动。');
  }, [pendingDraft, clearDraft, showNotice]);
  const packMeta = PACKS.find((item) => item.id === pack) ?? PACKS[0];
  const categoriesForPack = CATEGORIES_BY_PACK[pack];

  const choosePack = (next: 'standard' | 'smoke') => {
    setPack(next);
    const allowed = new Set(CATEGORIES_BY_PACK[next]);
    setCategories((current) => current.filter((name) => allowed.has(name)));
  };

  const toggle = (name: string) => setCategories((current) => (
    current.includes(name) ? current.filter((item) => item !== name) : [...current, name]
  ));

  const submit = (event: FormEvent) => {
    event.preventDefault();
    void startRun('/api/quality', {
      ...basePayload(),
      label: `质量抽查 · ${connection.model}`,
      pack,
      categories,
      per_category: perCategory,
      max_parallel: parallel,
      request_timeout: 60,
      authorized: consent,
    }, '质量抽查');
  };

  return (
    <>
      <p className="view-sub">
        默认用 MMLU 多科四选一精选子集，本地按字母判分；这是冒烟质量检查，不是完整 MMLU 榜单跑分。
      </p>
      <div className="split">
        <form className="card form-card" onSubmit={submit}>
          <h2 className="card-title">连接模型</h2>
          <p className="card-desc">地址、Key 与模型在所有测试页之间共享。</p>
          <ConnectionFields />
          <hr />
          <h2 className="card-title">题包</h2>
          <p className="card-desc">{packMeta.desc}</p>
          <div className="chips">
            {PACKS.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-pressed={pack === item.id}
                onClick={() => choosePack(item.id)}
              >
                {item.label}
              </button>
            ))}
          </div>
          <h2 className="card-title">抽查能力</h2>
          <p className="card-desc">不选即全部类别。</p>
          <div className="chips">
            {categoriesForPack.map((name) => (
              <button key={name} type="button" aria-pressed={categories.includes(name)} onClick={() => toggle(name)}>{name}</button>
            ))}
          </div>
          <div className="two">
            <Field label="每类题数"><input type="number" min={1} max={20} value={perCategory} onChange={(e) => setPerCategory(Number(e.target.value))} /></Field>
            <Field label="并发"><input type="number" min={1} max={8} value={parallel} onChange={(e) => setParallel(Number(e.target.value))} /></Field>
          </div>
          <RunFooter kind="quality" label="开始质量抽查" consent={consent} onConsent={setConsent} />
        </form>

        <div className="stack">
          <Card title="最近一次抽查" desc={run ? stateLabel(run.status) : '还没有记录。'}>
            <div className="score">{summary ? `${fmt(summary.accuracy)}%` : '—'}</div>
            {summary && (
              <div className="bars">
                {Object.entries(summary.by_level).map(([name, score]) => (
                  <div key={name} className="bar-row">
                    <span>{name}</span>
                    <div className="bar"><i style={{ width: `${score.total ? (100 * score.passed) / score.total : 0}%` }} /></div>
                    <b>{score.passed}/{score.total}</b>
                  </div>
                ))}
              </div>
            )}
            {summary && (
              <div className="bars">
                {Object.entries(summary.by_category).map(([name, score]) => (
                  <div key={name} className="bar-row">
                    <span>{name}</span>
                    <div className="bar"><i style={{ width: `${score.total ? (100 * score.passed) / score.total : 0}%` }} /></div>
                    <b>{score.passed}/{score.total}</b>
                  </div>
                ))}
              </div>
            )}
            {run && (
              <Hint>
                {summary ? `${summary.passed}/${summary.total} 题通过` : '运行中'} · TTFT P50 {fmt(summary?.ttft_p50_ms, 0)} ms
              </Hint>
            )}
          </Card>
          <AiDiagnosePanel run={run} />
          <Card title="说明">
            <Notes items={[
              'MMLU 精选：从 Hendrycks et al. 公开测试集抽样，按 STEM / 人文 / 社科 / 其他分组，答案只认 A–D。',
              '冒烟题包仍为本地中文短题（含多模态）；完整榜单跑分需另接评测流水线。',
              '本检查验证模型在该中继上是否仍可用，不替代官方完整基准成绩。',
            ]} />
          </Card>
        </div>
      </div>

      <Card title="逐题结果" desc={run ? run.message : '—'} className="table-card"
        actions={<button type="button" className="pill" disabled={!run} onClick={() => exportRun(run)}><Download size={13} />导出报告</button>}>
        <div className="scroll">
          <table>
            <thead><tr><th>#</th><th>分类</th><th>来源</th><th>难度</th><th>题目</th><th>期望</th><th>回答</th><th>耗时</th><th>判定</th></tr></thead>
            <tbody>
              {run && run.items.length ? run.items.map((item) => (
                <tr key={item.index}>
                  <td className="num">{item.index}</td>
                  <td>{item.category}{item.media_count > 0 ? ` ·图${item.media_count}` : ''}</td>
                  <td>{item.source || '—'}</td>
                  <td>{item.level}</td>
                  <td className="text" title={item.prompt}>{item.prompt}</td>
                  <td className="text" title={item.expected}>{item.expected}</td>
                  <td className="text" title={item.answer || item.error || ''}>{item.answer || item.error || ''}</td>
                  <td className="num">{ms(item.latency_ms)}</td>
                  <td><Verdict status={item.passed ? 'passed' : 'failed'}>{item.passed ? '通过' : '未通过'}</Verdict></td>
                </tr>
              )) : <EmptyRow colSpan={9}>还没有质量抽查数据。</EmptyRow>}
            </tbody>
          </table>
        </div>
      </Card>
    </>
  );
}

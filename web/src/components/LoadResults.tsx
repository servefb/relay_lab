import { Hint, Sparkline, Tick, Verdict, ms } from './ui';
import { fmt } from '../format';
import type { LabRun, LabStage } from '../types';

function loadWindowRpm(stage: LabStage): number | null {
  if (stage.status === 'running') return stage.requests ? stage.rpm : null;
  return stage.window_requests != null ? stage.rpm : null;
}

/** 仅正数毫秒才展示，0 或空值统一按无样本处理。 */
function positiveMs(value: number | null | undefined): string {
  return value != null && value > 0 ? ms(value) : '—';
}

export function loadCircuitStopped(run: LabRun): boolean {
  return run.kind === 'load' && Boolean(run.load?.stop_reason?.startsWith('熔断'));
}

export function LoadResults({ run }: { run: LabRun }) {
  if (!run.stages.length) return <Hint>{run.message || '没有窗口记录'}</Hint>;
  const live = run.status === 'running' || run.status === 'stopping';
  const summary = run.load ?? {
    peak_concurrency: null,
    peak_rpm: null,
    suggested_rpm: null,
    success_total: run.stages.reduce((sum, stage) => sum + stage.succeeded, 0),
    request_total: run.stages.reduce((sum, stage) => sum + stage.requests, 0),
    windows: run.stages.length,
    saturated: false,
    stop_reason: live ? '实时统计，本窗结束后再判定' : null,
  };
  const stages = run.stages;
  const measured = stages.filter((stage) => stage.window_requests != null);
  const passedStages = measured.filter((stage) => stage.status === 'passed');
  const pool = passedStages.length > 0 ? passedStages : measured.length > 0 ? measured : stages;
  const primary = live ? stages[stages.length - 1] : pool.reduce((best, stage) => (
    stage.rpm > best.rpm ? stage : best
  ), pool[0]);
  const hasPeak = !live && passedStages.length > 0 && summary.peak_rpm != null;
  const heroRpm = hasPeak ? summary.peak_rpm : loadWindowRpm(primary);
  const concurrency = hasPeak ? summary.peak_concurrency ?? primary.pressure : primary.pressure;
  const singleWindow = stages.length === 1;
  const provisional = live && primary.status === 'running';
  const windowSuccess = provisional ? (primary.requests ? primary.success_percent : null) : primary.window_success_percent;
  const hasWindow = primary.window_requests != null;
  const showThroughput = live || hasWindow;
  const streaming = run.settings.stream !== false;
  const tokenNote = primary.token_source === 'estimated' ? '含客户端估算'
    : primary.token_source === 'reported' ? '上游返回用量'
      : primary.token_source === 'missing' ? '上游用量缺失' : '用量来源未记录';
  const rateNote = primary.window_rate_seconds != null
    ? `按 ${fmt(primary.window_rate_seconds, 2)} 秒${primary.partial_results ? '已观测时段' : '目标窗口'}折算`
    : primary.partial_results && hasWindow ? '按目标窗口时长折算（旧记录）' : null;
  const failureReasons = Object.entries(primary.failure_reasons || {})
    .sort((left, right) => right[1] - left[1])
    .slice(0, 3);
  const failed = Math.max(0, primary.requests - primary.succeeded);
  const failPercent = primary.requests ? (100 * failed) / primary.requests : 0;
  const outputSpeed = showThroughput && primary.output_tpm != null ? primary.output_tpm / 60 : null;
  const hasTokenData = showThroughput && (
    primary.input_tpm != null
    || primary.output_tpm != null
    || primary.total_tpm != null
    || outputSpeed != null
  );
  const suggestedRpm = hasPeak ? summary.suggested_rpm : null;
  const hasTtft = streaming && (
    (primary.ttft_p50_ms != null && primary.ttft_p50_ms > 0)
    || (primary.ttft_p95_ms != null && primary.ttft_p95_ms > 0)
    || (primary.ttft_p99_ms != null && primary.ttft_p99_ms > 0)
  );
  const hasTpot = streaming && (
    (primary.tpot_p50_ms != null && primary.tpot_p50_ms > 0)
    || (primary.tpot_p95_ms != null && primary.tpot_p95_ms > 0)
    || (primary.tpot_p99_ms != null && primary.tpot_p99_ms > 0)
  );

  // 延迟分位矩阵行：避免多张卡片重复展示且彻底消除 ms 换行
  const latencyRows: {
    label: string;
    tag?: string;
    avg: string;
    p50: string;
    p90: string;
    p95: string;
    p99: string;
  }[] = [
    {
      label: '端到端',
      avg: ms(primary.latency_avg_ms),
      p50: ms(primary.latency_p50_ms),
      p90: ms(primary.latency_p90_ms),
      p95: ms(primary.latency_p95_ms),
      p99: ms(primary.latency_p99_ms),
    },
  ];
  if (streaming) {
    latencyRows.push({
      label: '首字节 TTFT',
      tag: !hasTtft ? (live ? '等待样本' : '无流式首包') : undefined,
      avg: '—',
      p50: positiveMs(primary.ttft_p50_ms),
      p90: '—',
      p95: positiveMs(primary.ttft_p95_ms),
      p99: positiveMs(primary.ttft_p99_ms),
    });
    latencyRows.push({
      label: '每 Token TPOT',
      tag: hasTpot && primary.token_source === 'estimated'
        ? '含估算'
        : !hasTpot ? '无有效样本' : undefined,
      avg: '—',
      p50: positiveMs(primary.tpot_p50_ms),
      p90: '—',
      p95: positiveMs(primary.tpot_p95_ms),
      p99: positiveMs(primary.tpot_p99_ms),
    });
  }

  return (
    <div className="load-result">
      <div className="load-hero" data-pass={hasPeak ? '1' : '0'}>
        <div className="load-hero-main">
          <span>{live ? '实时完成 RPM' : hasPeak ? '峰值完成 RPM' : primary.partial_results ? '本窗实测 RPM（部分结果）' : '本窗实测 RPM'}</span>
          <Tick
            text={fmt(heroRpm, 0)}
            className={heroRpm == null ? 'live-figure is-empty' : 'live-figure'}
            suffix={heroRpm != null ? <i>RPM</i> : undefined}
          />
          <em>
            {live
              ? `并发 ${concurrency} · 运行中`
              : hasPeak
                ? `并发 ${concurrency} · 建议容量 ${fmt(summary.suggested_rpm, 0)} RPM`
                : `并发 ${concurrency} · ${hasWindow ? '未计入峰值' : '无法核实窗口数据'}`}
          </em>
        </div>
        <div className="load-hero-side">
          <Verdict status={primary.status}>{loadCircuitStopped(run) ? '熔断中止' : undefined}</Verdict>
          <small>
            {singleWindow
              ? `${run.settings.window_seconds ?? 60} 秒目标窗口`
              : `${stages.length} 个窗口`}
          </small>
          {rateNote && <small>{rateNote}</small>}
        </div>
      </div>

      {/* 核心摘要：4 格紧凑概览，不与下方分位矩阵重复 */}
      <div className="load-summary">
        <div className="load-tile">
          <Tick
            text={fmt(windowSuccess)}
            className={windowSuccess == null ? 'is-empty' : undefined}
            suffix={windowSuccess != null ? <i>%</i> : undefined}
          />
          <span>{provisional ? '实时成功率' : '窗口成功率'}</span>
          <em>{windowSuccess != null
            ? `${fmt(provisional ? primary.succeeded : primary.window_succeeded, 0)}/${fmt(provisional ? primary.requests : primary.window_requests, 0)} 成功`
            : hasWindow ? '窗口内无完成请求' : live ? '结束后核实' : '无法核实窗口数据'}</em>
        </div>

        <div className="load-tile">
          <Tick text={fmt(primary.requests, 0)} className={!primary.requests ? 'is-empty' : undefined} />
          <span>全程请求</span>
          <em>
            {primary.requests ? (
              <>
                {fmt(primary.succeeded, 0)} 成功 ·{' '}
                {failed > 0 ? (
                  <strong className="is-bad">{fmt(failed, 0)} 失败 ({fmt(failPercent, 1)}%)</strong>
                ) : (
                  '全部成功'
                )}
              </>
            ) : '暂无完成请求'}
          </em>
        </div>

        <div className="load-tile">
          <Tick
            text={fmt(suggestedRpm, 0)}
            className={suggestedRpm == null ? 'is-empty' : undefined}
            suffix={suggestedRpm != null ? <i>RPM</i> : undefined}
          />
          <span>建议容量</span>
          <em>{hasPeak ? '实测 × 折算比例' : live ? '结束后给出' : primary.partial_results ? '部分结果不推算' : '需达标窗口才给出'}</em>
        </div>

        <div className="load-tile">
          <Tick text={fmt(primary.wall_seconds ?? primary.seconds, 1)} suffix={<i>s</i>} />
          <span>{primary.wall_seconds == null ? '记录耗时' : '本档总耗时'}</span>
          <em>{live ? '含启动与在途收尾'
            : primary.wall_seconds == null ? '包含范围见原报告' : `请求观测 ${fmt(primary.seconds, 2)}s`}</em>
        </div>
      </div>

      {/* 延迟分位矩阵 */}
      <div className="load-section">
        <div className="load-section-head">
          <h3 className="load-kicker">{provisional ? '已完成请求延迟' : primary.window_rate_seconds != null ? '窗口内延迟' : '延迟分布'}</h3>
          {!streaming && <small>非流式 · 不统计 TTFT / TPOT</small>}
        </div>
        <div className="load-matrix-wrap">
          <table className="load-matrix">
            <thead>
              <tr>
                <th>指标</th>
                <th>均值</th>
                <th>P50</th>
                <th>P90</th>
                <th>P95</th>
                <th>P99</th>
              </tr>
            </thead>
            <tbody>
              {latencyRows.map((row) => (
                <tr key={row.label}>
                  <td className="load-matrix-label">
                    <span>{row.label}</span>
                    {row.tag && <small>{row.tag}</small>}
                  </td>
                  <td className={row.avg === '—' ? 'is-empty' : undefined}>{row.avg}</td>
                  <td className={row.p50 === '—' ? 'is-empty' : undefined}>{row.p50}</td>
                  <td className={row.p90 === '—' ? 'is-empty' : undefined}>{row.p90}</td>
                  <td className={row.p95 === '—' ? 'is-empty' : 'is-key'}>{row.p95}</td>
                  <td className={row.p99 === '—' ? 'is-empty' : undefined}>{row.p99}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      {/* Token 吞吐：有数据时紧凑平铺，全空时折叠为单行提示 */}
      {hasTokenData ? (
        <div className="load-section">
          <div className="load-section-head">
            <h3 className="load-kicker">Token 吞吐</h3>
            <small>{tokenNote}</small>
          </div>
          <div className="load-tokens">
            <div className="load-token-cell">
              <strong className={primary.input_tpm == null ? 'is-empty' : undefined}>{fmt(primary.input_tpm, 0)}</strong>
              <span>输入 TPM</span>
            </div>
            <div className="load-token-cell">
              <strong className={primary.output_tpm == null ? 'is-empty' : undefined}>{fmt(primary.output_tpm, 0)}</strong>
              <span>输出 TPM</span>
            </div>
            <div className="load-token-cell">
              <strong className={primary.total_tpm == null ? 'is-empty' : undefined}>{fmt(primary.total_tpm, 0)}</strong>
              <span>总 TPM</span>
            </div>
            <div className="load-token-cell">
              <strong className={outputSpeed == null ? 'is-empty' : undefined}>
                {outputSpeed == null ? '—' : <>{fmt(outputSpeed, 1)}<i>tok/s</i></>}
              </strong>
              <span>合计输出速率</span>
            </div>
          </div>
        </div>
      ) : (
        <div className="load-token-empty">
          <span>Token 吞吐</span>
          <small>暂无数据 · {tokenNote}</small>
        </div>
      )}

      {(() => {
        // Hero already covers pass/fail; only keep diagnostic notes that add new detail.
        const lines: string[] = [];
        const reason = primary.reason?.trim();
        const stop = summary.stop_reason?.trim();
        if (reason) lines.push(reason);
        if (stop && stop !== reason) {
          const genericFail = /^并发\s*\d+\s*未达标/.test(stop);
          if (!(genericFail && !hasPeak)) lines.push(stop);
        }
        return lines.map((line) => <p key={line} className="load-stop">{line}</p>);
      })()}
      {failureReasons.length > 0 && (
        <div className="load-failures">
          {failureReasons.map(([reason, count]) => <span key={reason}>{reason} · {fmt(count, 0)}</span>)}
        </div>
      )}
      {summary.saturated && <div className="callout">吞吐曲线已饱和，继续加压收益很低。</div>}
      {!singleWindow && (
        <>
          <div className="load-curve">
            <Sparkline rows={stages.map((stage) => ({
              value: loadWindowRpm(stage), label: `并发 ${stage.pressure} · ${fmt(loadWindowRpm(stage), 0)} RPM`,
            }))} />
            <div className="curve-axis">
              {stages.map((stage) => <span key={stage.index}>{stage.pressure}</span>)}
            </div>
            <small>横轴并发，纵轴每分钟完成请求数</small>
          </div>
          <div className="scroll">
            <table className="stage-table">
              <thead><tr><th>窗口</th><th>并发</th><th>完成 RPM</th><th>窗口成功率</th><th>TTFT P95</th><th>判定</th></tr></thead>
              <tbody>
                {stages.map((stage) => (
                  <tr key={stage.index}>
                    <td className="num">#{stage.index}{stage.partial_results && <small>部分结果</small>}</td>
                    <td className="num">{stage.pressure}</td>
                    <td className="num">{fmt(loadWindowRpm(stage))}</td>
                    <td className="num">{stage.window_success_percent == null ? '—' : `${fmt(stage.window_success_percent)}%`}</td>
                    <td className="num">{ms(stage.ttft_p95_ms)}</td>
                    <td title={stage.reason}><Verdict status={stage.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

/* Relay Lab console. No build step: plain DOM, no framework. */

const DEFAULTS = {
  label: '',
  url: '',
  model: '',
  protocol: 'openai',
  goal: 'both',
  stage_seconds: 10,
  repeats: 1,
  max_steps: 4,
  refine_steps: 2,
  start_rpm: 60,
  max_rpm: 480,
  max_parallel: 8,
  max_requests: 240,
  token_reserve: 1000000,
  max_wall_seconds: 600,
  request_timeout: 30,
  cooldown_seconds: 5,
  success_percent: 99,
  ttft_p99_ms: 3000,
  tpot_p99_ms: 100,
  short_output_tokens: 32,
  long_output_tokens: 512,
  seed: 42,
};

const ADVANCED = [
  ['start_rpm', '起始 RPM', 1, 6000, 1],
  ['max_rpm', '目标 RPM 安全上限', 1, 6000, 1],
  ['max_parallel', '最大并发', 1, 128, 1],
  ['max_requests', '请求预算（两轮均分）', 3, 20000, 1],
  ['stage_seconds', '每档秒数', 5, 600, 1],
  ['repeats', '每档重复次数', 1, 3, 1],
  ['max_steps', '倍增档数上限', 1, 8, 1],
  ['refine_steps', '二分复测次数', 0, 5, 1],
  ['max_wall_seconds', '总时长上限（秒）', 30, 7200, 1],
  ['request_timeout', '单请求超时（秒）', 2, 120, 1],
  ['cooldown_seconds', '档间冷却（秒）', 0, 120, 1],
  ['success_percent', '成功率要求（%）', 90, 100, 0.1],
  ['ttft_p99_ms', 'TTFT P99 上限（ms）', 1, 120000, 1],
  ['tpot_p99_ms', 'TPOT P99 上限（ms）', 1, 10000, 1],
  ['short_output_tokens', 'RPM 测试输出上限', 8, 256, 1],
  ['long_output_tokens', 'TPM 测试输出上限', 64, 4096, 1],
  ['token_reserve', '估算 Token 预留预算', 10000, 100000000, 1000],
  ['seed', '数据集种子', 0, 2147483647, 1],
];

const PRESETS = {
  quick: { stage_seconds: 10, repeats: 1, max_requests: 240, token_reserve: 1000000, max_wall_seconds: 600, cooldown_seconds: 5 },
  steady: { stage_seconds: 60, repeats: 3, max_requests: 3000, token_reserve: 20000000, max_wall_seconds: 3600, cooldown_seconds: 15 },
};

const CATEGORIES = ['算术', '指令', 'JSON', '中文理解', '代码'];

const STATE_LABEL = {
  running: '测试中', stopping: '正在停止', stopped: '已停止', completed: '已完成',
  error: '执行异常', interrupted: '已中断', passed: '达标', failed: '不达标', unknown: '未判定',
};
const FINDING_LABEL = { pending: '等待测试', bounded: '已发现不达标边界', lower_bound: '仅测到下界', inconclusive: '证据不足' };
const SCHEDULE_LABEL = {
  starting: '启动中', busy: '被占用', needs_key: '缺少密钥', invalid: '配置无效',
  completed: '已完成', error: '失败', stopped: '已停止', running: '运行中',
};
const TITLES = {
  overview: '概览', speed: '速度测试', parallel: '并行测试', new: '容量探测',
  quality: '模型质量', schedules: '定时任务', history: '实验记录', method: '测试口径',
};

const state = {
  view: 'overview',
  preset: 'quick',
  settings: { ...DEFAULTS },
  apiKey: '',
  models: [],
  modelsLoading: false,
  modelError: '',
  modelHint: '填写地址和 Key，然后点击“获取模型”。',
  discovery: null,
  runs: [],
  schedules: [],
  selectedId: '',
  compare: [],
  categories: [],
  busy: false,
  activeKind: '',
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => Array.from(document.querySelectorAll(selector));

function fmt(value, decimals = 1) {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return value.toLocaleString('zh-CN', { maximumFractionDigits: decimals });
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (character) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[character]
  ));
}

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
}

const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body) });

function notify(message) {
  $('#alert-text').textContent = message;
  $('#alert').hidden = false;
}
function clearAlert() { $('#alert').hidden = true; }
function notice(message) {
  if (!message) { $('#notice').hidden = true; return; }
  $('#notice-text').textContent = message;
  $('#notice').hidden = false;
}

/* ---------- views ---------- */

function showView(view) {
  state.view = view;
  $('#page-title').textContent = TITLES[view] || '概览';
  $$('#nav button').forEach((button) => {
    if (button.dataset.view === view) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  $$('[data-view-panel]').forEach((panel) => { panel.hidden = panel.dataset.viewPanel !== view; });
  if (window.location.hash !== `#${view}`) window.history.replaceState(null, '', `#${view}`);
  if (view === 'schedules') loadSchedules().catch(() => {});
}

/* ---------- connection blocks (shared across every test page) ---------- */

const CONNECTION_MARKUP = (prefix) => `
  <label>API 地址
    <input type="url" data-conn="url" data-prefix="${prefix}" placeholder="https://example.com 或完整接口地址" autocomplete="off" />
  </label>
  <div class="two">
    <label>接口协议
      <select data-conn="protocol" data-prefix="${prefix}">
        <option value="openai">Chat Completions</option>
        <option value="openai_responses">Responses</option>
      </select>
    </label>
    <label>API Key
      <input type="password" data-conn="key" data-prefix="${prefix}" placeholder="仅本次使用" autocomplete="off" />
    </label>
  </div>
  <div class="model-head">
    <label>模型 ID</label>
    <button type="button" data-conn="fetch" data-prefix="${prefix}">获取模型</button>
  </div>
  <select data-conn="model" data-prefix="${prefix}" required>
    <option value="">请先获取模型列表</option>
  </select>
  <p class="hint" data-conn="hint" data-prefix="${prefix}" role="status">填写地址和 Key，然后点击“获取模型”。</p>
`;

function mountConnections() {
  $$('[data-connection]').forEach((host) => { host.innerHTML = CONNECTION_MARKUP(host.dataset.connection); });
}

function refreshConnections() {
  $$('[data-conn="url"]').forEach((input) => { input.value = state.settings.url; });
  $$('[data-conn="protocol"]').forEach((input) => { input.value = state.settings.protocol; });
  $$('[data-conn="key"]').forEach((input) => { if (input.value !== state.apiKey) input.value = state.apiKey; });
  $$('[data-conn="model"]').forEach((select) => {
    const wanted = state.settings.model;
    select.innerHTML = `<option value="">${state.models.length ? '请选择模型' : '请先获取模型列表'}</option>`
      + state.models.map((id) => `<option value="${escapeHtml(id)}">${escapeHtml(id)}</option>`).join('');
    select.value = state.models.includes(wanted) ? wanted : '';
    select.disabled = state.modelsLoading || state.models.length === 0;
  });
  $$('[data-conn="fetch"]').forEach((button) => {
    button.textContent = state.modelsLoading ? '获取中…' : (state.models.length ? '刷新模型' : '获取模型');
    button.disabled = state.modelsLoading || !state.settings.url.trim();
  });
  $$('[data-conn="hint"]').forEach((hint) => {
    hint.textContent = state.modelError || state.modelHint;
    hint.className = state.modelError ? 'hint error' : 'hint';
  });
}

function invalidateModels(hint = '连接信息已变化，请重新获取模型。') {
  if (state.discovery) { state.discovery.abort(); state.discovery = null; }
  state.models = [];
  state.modelsLoading = false;
  state.modelError = '';
  state.modelHint = hint;
  state.settings.model = '';
  refreshConnections();
  refreshButtons();
}

async function fetchModels() {
  const controller = new AbortController();
  if (state.discovery) state.discovery.abort();
  state.discovery = controller;
  state.modelsLoading = true;
  state.modelError = '';
  state.modelHint = '正在读取模型列表，不会开始压测…';
  refreshConnections();
  try {
    const payload = await api('/api/models', {
      method: 'POST',
      body: JSON.stringify({ url: state.settings.url.trim(), api_key: state.apiKey || undefined }),
      signal: controller.signal,
    });
    if (state.discovery !== controller) return;
    state.models = payload.models;
    state.modelHint = payload.models.length
      ? `已获取 ${payload.models.length} 个模型，请从下拉框选择。`
      : '该 Key 没有返回可用模型，请检查权限或联系站点管理员。';
  } catch (error) {
    if (state.discovery !== controller) return;
    state.models = [];
    state.modelError = error.message || '模型列表获取失败，请稍后重试。';
  } finally {
    if (state.discovery === controller) {
      state.discovery = null;
      state.modelsLoading = false;
      refreshConnections();
      refreshButtons();
    }
  }
}

/* ---------- capacity form ---------- */

function buildAdvanced() {
  $('#advanced').innerHTML = ADVANCED.map(([field, label, min, max, step]) => `
    <label>${label}
      <input type="number" data-field="${field}" min="${min}" max="${max}" step="${step}" value="${state.settings[field]}" />
    </label>
  `).join('');
  $('#advanced').addEventListener('input', (event) => {
    const field = event.target.dataset.field;
    if (!field) return;
    const value = Number(event.target.value);
    if (!Number.isFinite(value)) return;
    state.settings[field] = value;
    renderBudget();
  });
}

function renderBudget() {
  const s = state.settings;
  $('#budget').textContent = fmt(s.max_requests, 0);
  $('#parallel').textContent = s.max_parallel;
  $('#wall').textContent = fmt(s.max_wall_seconds / 60);
  $('#side-budget').textContent = fmt(s.max_requests, 0);
  $('#side-token').textContent = fmt(s.token_reserve, 0);
  $('#side-wall').textContent = `${fmt(s.max_wall_seconds / 60)} 分钟`;
  $('#side-stage').textContent = `${s.stage_seconds} 秒 × ${s.repeats} 次`;
}

function renderSelection() {
  $$('[data-goal]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.goal === state.settings.goal));
  });
  $$('[data-preset]').forEach((button) => {
    button.setAttribute('aria-pressed', String(button.dataset.preset === state.preset));
  });
}

function activeRun() {
  return state.runs.find((run) => run.status === 'running' || run.status === 'stopping');
}

function refreshButtons() {
  const active = activeRun();
  state.activeKind = active ? active.kind : '';
  const ready = !state.busy && !active && state.models.includes(state.settings.model) && !state.modelsLoading;
  const pairs = [
    ['#speed-start', '#speed-consent', '#speed-stop', 'speed', '开始测速'],
    ['#parallel-start', '#parallel-consent', '#parallel-stop', 'parallel', '开始并行测试'],
    ['#quality-start', '#quality-consent', '#quality-stop', 'quality', '开始质量抽查'],
    ['#schedule-create', '#schedule-consent', null, null, '创建定时任务'],
  ];
  pairs.forEach(([startId, consentId, stopId, kind, label]) => {
    const button = $(startId);
    if (!button) return;
    const consented = $(consentId).checked;
    const mine = kind === null || state.activeKind === kind;
    button.disabled = !(ready && consented);
    button.textContent = state.busy && mine ? '正在提交…' : (state.activeKind === kind ? '运行中' : label);
    if (stopId) $(stopId).hidden = state.activeKind !== kind;
  });
  $('#start').disabled = !(ready && $('#consent').checked);
  $('#start').textContent = state.busy ? '正在提交…' : (state.activeKind === 'capacity' ? '实验正在运行' : '开始探测');
  $('#stop').hidden = state.activeKind !== 'capacity';
  $('#demo').disabled = state.busy || !!active;
}

/* ---------- charts ---------- */

function sparkline(rows) {
  const usable = rows.filter((row) => Number.isFinite(row.value));
  const values = usable.map((row) => row.value);
  const width = 460;
  const height = 54;
  const max = Math.max(1, ...values) * 1.2;
  const x = (index) => (values.length <= 1 ? width / 2 : (index / (values.length - 1)) * width);
  const y = (value) => height - (value / max) * (height - 6);
  const baseline = `<line x1="0" y1="${height - 0.5}" x2="${width}" y2="${height - 0.5}" stroke="rgba(20,22,26,0.1)" stroke-dasharray="3 4" />`;
  if (!values.length) {
    return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="暂无数据">${baseline}</svg>`;
  }
  return `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="随档位变化">
    ${baseline}
    <polyline points="${values.map((value, index) => `${x(index).toFixed(1)},${y(value).toFixed(1)}`).join(' ')}"
      fill="none" stroke="#816df8" stroke-width="1.6" />
    ${values.map((value, index) => `<circle cx="${x(index).toFixed(1)}" cy="${y(value).toFixed(1)}" r="2.6" fill="#816df8"><title>${escapeHtml(usable[index].label || '')}</title></circle>`).join('')}
  </svg>`;
}

/* ---------- overview ---------- */

function latest(kind) {
  return state.runs.find((run) => run.kind === kind);
}

function selectedRun() {
  return state.runs.find((run) => run.run_id === state.selectedId);
}

function renderOverview() {
  const run = latest('capacity');
  const stages = run ? run.stages : [];
  const done = stages.filter((stage) => stage.status !== 'running');
  const last = done[done.length - 1];
  const rpm = run && run.findings.find((finding) => finding.phase === 'rpm');
  const tpm = run && run.findings.find((finding) => finding.phase === 'tpm');

  $('#overview-sub').textContent = run
    ? `${run.settings.label} · ${STATE_LABEL[run.status] || run.status} · ${new Date(run.created_at).toLocaleString('zh-CN')}`
    : '尚未开始 · 结果由真实请求产生';

  $('#wide-rpm').innerHTML = `${fmt(rpm ? rpm.value : last && last.phase === 'rpm' ? last.rpm : null, 0)}<i>/ ${run ? run.settings.max_rpm : DEFAULTS.max_rpm}</i>`;
  $('#wide-tpm').textContent = fmt(tpm ? tpm.value : last && last.phase === 'tpm' ? last.output_tpm : null, 0);
  const phaseRows = (phase) => stages
    .filter((stage) => stage.phase === phase && stage.status !== 'running')
    .map((stage) => ({
      value: phase === 'rpm' ? stage.rpm : stage.output_tpm,
      label: `第 ${stage.index} 档 · ${STATE_LABEL[stage.status] || stage.status}`,
    }));
  $('#spark-rpm').innerHTML = sparkline(phaseRows('rpm'));
  $('#spark-tpm').innerHTML = sparkline(phaseRows('tpm'));

  const total = done.reduce((sum, stage) => sum + stage.succeeded, 0);
  $('#k-requests').textContent = fmt(total, 0);
  $('#k-requests-sub').textContent = run ? `预留 ${fmt(run.reserved_requests, 0)} / ${fmt(run.settings.max_requests, 0)}` : '未开始';
  $('#k-success').textContent = last ? `${fmt(last.success_percent)}%` : '—';
  $('#k-success-sub').textContent = last ? `${last.succeeded} / ${last.requests} 成功` : '最近一档';
  $('#k-tokens').textContent = last && last.mean_output_tokens ? fmt(last.mean_output_tokens, 0) : '—';
  $('#k-ttft').textContent = last && last.ttft_p99_ms !== null ? `${fmt(last.ttft_p99_ms, 0)} ms` : '—';

  $('#findings').innerHTML = (run ? run.findings : []).map((finding) => `
    <article class="finding ${finding.status}">
      <div class="finding-head"><b>${finding.phase === 'rpm' ? 'RPM' : '输出 TPM'} 结论</b>
        <span>${FINDING_LABEL[finding.status] || finding.status}</span></div>
      <p>${escapeHtml(finding.message)}</p>
    </article>`).join('');

  $('#run-message').textContent = run ? run.message : '连接模型后，选择目标并开始测试';
  $('#bar').style.width = run
    ? `${Math.max(2, Math.min(100, (run.reserved_requests / Math.max(1, run.settings.max_requests)) * 100))}%` : '0%';
  $('#rows').innerHTML = stages.length ? stages.map((stage) => `
    <tr>
      <td class="num">#${stage.index}<small>${stage.phase.toUpperCase()} · 第 ${stage.repeat} 次</small></td>
      <td class="num">${stage.phase === 'rpm' ? `${fmt(stage.pressure, 0)} RPM` : `${stage.pressure} 并发`}</td>
      <td class="num">${stage.status === 'running' ? '—' : fmt(stage.rpm)}</td>
      <td class="num">${fmt(stage.output_tpm)}</td>
      <td class="num">${stage.status === 'running' ? '—' : `${fmt(stage.success_percent)}%`}</td>
      <td class="num">${stage.ttft_p99_ms === null ? '—' : `${fmt(stage.ttft_p99_ms, 0)} ms`}</td>
      <td><span class="verdict ${stage.status}">${STATE_LABEL[stage.status] || stage.status}</span><small>${escapeHtml(stage.reason)}</small></td>
    </tr>`).join('') : '<tr><td colspan="7" class="empty">还没有容量测试数据。可以先跑一次本地演示，或填写自己的站点。</td></tr>';
  $('#export').disabled = !run;
}

/* ---------- speed ---------- */

function renderSpeed() {
  const run = latest('speed');
  const stage = run ? run.stages.find((item) => item.phase === 'speed') : null;
  $('#speed-ttft50').textContent = stage && stage.ttft_p50_ms !== null ? `${fmt(stage.ttft_p50_ms, 0)}` : '—';
  $('#speed-ttft99').textContent = stage && stage.ttft_p99_ms !== null ? `${fmt(stage.ttft_p99_ms, 0)}` : '—';
  $('#speed-latency').textContent = stage && stage.latency_avg_ms !== null ? `${fmt(stage.latency_avg_ms, 0)}` : '—';
  $('#speed-tps').textContent = stage && stage.output_tpm ? fmt(stage.output_tpm / 60, 1) : '—';
  $('#speed-note').textContent = !run
    ? '还没有测速记录。'
    : `${run.settings.requests} 个请求 / 并发 ${run.settings.max_parallel} · ${stage ? `${stage.succeeded}/${stage.requests} 成功` : '等待结果'} · ${STATE_LABEL[run.status] || run.status}`;
}

/* ---------- parallel ---------- */

function renderParallel() {
  const run = latest('parallel');
  const stages = run ? run.stages : [];
  $('#parallel-spark').innerHTML = sparkline(stages.map((stage) => ({
    value: stage.output_tpm,
    label: `并发 ${stage.pressure} · ${fmt(stage.rpm)} RPM`,
  })));
  $('#parallel-note').textContent = !run ? '还没有记录。'
    : `${run.settings.levels.join(' / ')} 并发 · 每档 ${run.settings.stage_seconds} 秒 · ${STATE_LABEL[run.status] || run.status}`;
  $('#parallel-message').textContent = run ? run.message : '—';
  $('#parallel-rows').innerHTML = stages.length ? stages.map((stage) => `
    <tr>
      <td class="num">${stage.pressure}</td>
      <td class="num">${stage.requests} / ${stage.succeeded}</td>
      <td class="num">${fmt(stage.rpm)}</td>
      <td class="num">${fmt(stage.output_tpm)}</td>
      <td class="num">${fmt(stage.ttft_p50_ms, 0)}</td>
      <td class="num">${fmt(stage.ttft_p99_ms, 0)}</td>
      <td class="num">${fmt(stage.latency_avg_ms, 0)}</td>
    </tr>`).join('') : '<tr><td colspan="7" class="empty">还没有并行测试数据。</td></tr>';
  $('#parallel-export').disabled = !run;
}

/* ---------- quality ---------- */

function renderQuality() {
  const run = latest('quality');
  const summary = run ? run.quality : null;
  $('#quality-score').textContent = summary ? `${fmt(summary.accuracy)}%` : '—';
  $('#quality-categories-result').innerHTML = summary ? Object.entries(summary.by_category).map(([name, score]) => {
    const percent = score.total ? (100 * score.passed) / score.total : 0;
    return `<div class="bar-row"><span>${escapeHtml(name)}</span>
      <div class="bar"><i style="width:${percent}%"></i></div>
      <b>${score.passed}/${score.total}</b></div>`;
  }).join('') : '';
  $('#quality-note').textContent = !run ? '还没有记录。'
    : `${summary ? `${summary.passed}/${summary.total} 题通过` : '运行中'} · TTFT P50 ${fmt(summary && summary.ttft_p50_ms, 0)} ms · ${STATE_LABEL[run.status] || run.status}`;
  $('#quality-message').textContent = run ? run.message : '—';
  $('#quality-rows').innerHTML = run && run.items.length ? run.items.map((item) => `
    <tr>
      <td class="num">${item.index}</td>
      <td>${escapeHtml(item.category)}</td>
      <td class="text">${escapeHtml(item.prompt)}</td>
      <td class="text">${escapeHtml(item.expected)}</td>
      <td class="text">${escapeHtml(item.answer || item.error || '')}</td>
      <td class="num">${item.latency_ms === null ? '—' : `${fmt(item.latency_ms, 0)} ms`}</td>
      <td><span class="verdict ${item.passed ? 'passed' : 'failed'}">${item.passed ? '通过' : '未通过'}</span></td>
    </tr>`).join('') : '<tr><td colspan="7" class="empty">还没有质量抽查数据。</td></tr>';
  $('#quality-export').disabled = !run;
}

/* ---------- schedules ---------- */

function scheduleSummary(item) {
  if (item.kind === 'speed') return `速度 · ${item.speed.requests} 个请求`;
  if (item.kind === 'parallel') return `并行 · ${item.parallel.levels.join('/')} 并发`;
  return `容量 · 起始 ${item.capacity.start_rpm} RPM`;
}

function renderSchedules() {
  $('#schedule-list').innerHTML = state.schedules.length ? state.schedules.map((item) => `
    <div class="schedule-row">
      <div class="schedule-main">
        <strong>${escapeHtml(item.label)}</strong>
        <small>${scheduleSummary(item)} · 每 ${item.interval_minutes} 分钟</small>
        <small>下次 ${new Date(item.next_run_at).toLocaleString('zh-CN')} · ${SCHEDULE_LABEL[item.last_status] || item.last_status || '未运行'}</small>
      </div>
      <div class="schedule-actions">
        ${item.needs_key ? `<input type="password" placeholder="补填 Key" data-key-for="${item.schedule_id}" />` : ''}
        <button type="button" data-schedule-run="${item.schedule_id}">立即执行</button>
        <button type="button" data-schedule-toggle="${item.schedule_id}" data-enabled="${item.enabled}">${item.enabled ? '停用' : '启用'}</button>
        <button type="button" class="danger-link" data-schedule-delete="${item.schedule_id}">删除</button>
      </div>
    </div>`).join('') : '<p class="hint">还没有定时任务。</p>';
}

async function loadSchedules() {
  const payload = await api('/api/schedules');
  state.schedules = payload.schedules;
  renderSchedules();
}

/* ---------- history / method ---------- */

function renderHistory() {
  $('#count').textContent = state.runs.length;
  $('#history').innerHTML = state.runs.length ? state.runs.map((run) => `
    <div class="history-row ${run.run_id === state.selectedId ? 'selected' : ''}">
      <input type="checkbox" aria-label="对比 ${escapeHtml(run.settings.label)}"
        ${state.compare.includes(run.run_id) ? 'checked' : ''}
        ${!state.compare.includes(run.run_id) && state.compare.length >= 2 ? 'disabled' : ''}
        data-compare="${run.run_id}" />
      <button type="button" data-select="${run.run_id}">
        <strong>${escapeHtml(run.settings.label)}</strong>
        <small>${run.kind} · ${new Date(run.created_at).toLocaleString('zh-CN')} · ${STATE_LABEL[run.status] || run.status}</small>
      </button>
    </div>`).join('') : '<p class="hint">还没有实验，首次结果会自动保存在这里。</p>';

  const picked = state.compare.map((id) => state.runs.find((run) => run.run_id === id)).filter(Boolean);
  const box = $('#compare');
  if (picked.length !== 2) {
    box.hidden = false;
    box.innerHTML = `<h2>并排对照</h2>
      <p>在左侧勾选两份记录，这里会并排显示 RPM、输出 TPM 与配置指纹差异。当前已选 ${picked.length} 份。</p>`;
    return;
  }
  box.hidden = false;
  const same = picked[0].fingerprint === picked[1].fingerprint;
  box.innerHTML = `
    <h2>并排对照</h2>
    <p class="${same ? '' : 'warn'}">${same
      ? '配置指纹一致；仍需确认运行时段、缓存与外部网络条件相近。'
      : '配置指纹不同：负载、模型、SLA 或预算不一致，只可并排查看，不能直接比较谁更强。'}</p>
    <div class="compare-grid">
      ${picked.map((run) => {
        const rpm = run.findings.find((finding) => finding.phase === 'rpm');
        const tpm = run.findings.find((finding) => finding.phase === 'tpm');
        return `<div>
          <span>${escapeHtml(run.settings.label)}</span>
          <p>RPM <strong>${fmt(rpm ? rpm.value : null, 0)}</strong></p>
          <p>输出 TPM <strong>${fmt(tpm ? tpm.value : null, 0)}</strong></p>
          <small>${STATE_LABEL[run.status] || run.status} · ${run.kind}</small>
        </div>`;
      }).join('')}
    </div>`;
}

function renderMethod() {
  const run = selectedRun();
  const list = run ? run.warnings : [
    '这是基于 EvalScope 压测引擎的容量探索，不是 MLPerf 认证测试。',
    'RPM 逐档提高目标速率；TPM 逐档提高并发。缺少指标时不判为达标。',
    '达到配置或预算上限时，只能报告已观测下界，不虚构最大值。',
  ];
  $('#warnings').innerHTML = list.map((text) => `<li>${escapeHtml(text)}</li>`).join('');
  $('#fingerprint').textContent = run ? `配置指纹：${run.fingerprint} · 报告不包含 API Key` : '';
}

function render() {
  renderBudget();
  renderSelection();
  refreshConnections();
  refreshButtons();
  refreshCategoryChips();
  renderOverview();
  renderSpeed();
  renderParallel();
  renderQuality();
  renderSchedules();
  renderHistory();
  renderMethod();
}

/* ---------- actions ---------- */

async function loadRuns() {
  const payload = await api('/api/runs');
  state.runs = payload.runs;
  if (!state.selectedId && payload.runs.length) state.selectedId = payload.runs[0].run_id;
  render();
}

function basePayload() {
  return {
    label: '',
    url: state.settings.url,
    model: state.settings.model,
    protocol: state.settings.protocol,
    api_key: state.apiKey || undefined,
  };
}

async function startRun(path, body, label) {
  state.busy = true;
  clearAlert();
  refreshButtons();
  try {
    const run = await post(path, body);
    state.runs = [run, ...state.runs.filter((item) => item.run_id !== run.run_id)];
    state.selectedId = run.run_id;
    state.apiKey = '';
    refreshConnections();
    notice(`${label}已启动：${run.settings.label}。密钥已从页面清除。`);
    render();
  } catch (error) {
    notify(error.message || '启动失败');
  } finally {
    state.busy = false;
    refreshButtons();
  }
}

function startSpeed(event) {
  event.preventDefault();
  return startRun('/api/speed', {
    ...basePayload(),
    label: `速度测试 · ${state.settings.model}`,
    requests: Number($('#speed-requests').value),
    max_parallel: Number($('#speed-parallel').value),
    output_tokens: Number($('#speed-tokens').value),
    request_timeout: Number($('#speed-timeout').value),
    authorized: $('#speed-consent').checked,
  }, '速度测试');
}

function startParallel(event) {
  event.preventDefault();
  const levels = $('#parallel-levels').value.split(',')
    .map((value) => Number(value.trim()))
    .filter((value) => Number.isFinite(value) && value >= 1 && value <= 128);
  return startRun('/api/parallel', {
    ...basePayload(),
    label: `并行测试 · ${state.settings.model}`,
    levels: levels.length ? levels : [1],
    stage_seconds: Number($('#parallel-seconds').value),
    output_tokens: Number($('#parallel-tokens').value),
    request_timeout: 60,
    authorized: $('#parallel-consent').checked,
  }, '并行测试');
}

function startQuality(event) {
  event.preventDefault();
  return startRun('/api/quality', {
    ...basePayload(),
    label: `质量抽查 · ${state.settings.model}`,
    categories: state.categories,
    per_category: Number($('#quality-per').value),
    max_parallel: Number($('#quality-parallel').value),
    request_timeout: 60,
    authorized: $('#quality-consent').checked,
  }, '质量抽查');
}

async function createSchedule(event) {
  event.preventDefault();
  const kind = $('#schedule-kind').value;
  const base = {
    label: $('#schedule-label').value.trim() || `${state.settings.model} 定时任务`,
    kind,
    interval_minutes: Number($('#schedule-interval').value),
    api_key: state.apiKey || undefined,
  };
  const connection = {
    url: state.settings.url,
    model: state.settings.model,
    protocol: state.settings.protocol,
  };
  if (kind === 'speed') {
    base.speed = { ...connection, requests: Number($('#schedule-speed-requests').value) };
  } else if (kind === 'parallel') {
    base.parallel = {
      ...connection,
      levels: $('#schedule-parallel-levels').value.split(',').map((value) => Number(value.trim())).filter((value) => Number.isFinite(value)),
    };
  } else {
    base.capacity = { ...connection, start_rpm: Number($('#schedule-start-rpm').value) };
  }
  try {
    await post('/api/schedules', base);
    await loadSchedules();
    notice('定时任务已创建。密钥只保存在内存。');
  } catch (error) {
    notify(error.message || '创建失败');
  }
}

async function stopActive() {
  const active = activeRun();
  if (!active) return;
  state.busy = true;
  refreshButtons();
  try {
    await post(`/api/runs/${active.run_id}/stop`, {});
    await loadRuns();
  } catch (error) {
    notify(error.message || '停止失败，请重试');
  } finally {
    state.busy = false;
    refreshButtons();
  }
}

async function runDemo() {
  state.busy = true;
  clearAlert();
  refreshButtons();
  try {
    const run = await post('/api/demo', { protocol: state.settings.protocol });
    state.runs = [run, ...state.runs.filter((item) => item.run_id !== run.run_id)];
    state.selectedId = run.run_id;
    showView('overview');
    notice('本地演示已启动：只访问 127.0.0.1 的模拟接口。');
    render();
  } catch (error) {
    notify(error.message || '本地演示启动失败');
  } finally {
    state.busy = false;
    refreshButtons();
  }
}

function exportRun(run) {
  if (!run) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(run, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = `${run.run_id}.json`;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/* ---------- category chips ---------- */

function refreshCategoryChips() {
  const host = $('#quality-categories');
  if (!host) return;
  host.innerHTML = CATEGORIES.map((name) => `
    <button type="button" data-category="${name}" aria-pressed="${state.categories.includes(name)}">${name}</button>
  `).join('') + '<span class="chip-hint">不选＝全部</span>';
}

/* ---------- wiring ---------- */

function bind() {
  $$('#nav button').forEach((button) => button.addEventListener('click', () => showView(button.dataset.view)));
  $('#goto-new').addEventListener('click', () => showView('new'));
  $('#alert-close').addEventListener('click', clearAlert);
  $('#refresh').addEventListener('click', () => {
    loadRuns().catch((error) => notify(error.message));
    loadSchedules().catch(() => {});
  });
  $('#demo').addEventListener('click', runDemo);
  $('#form').addEventListener('submit', (event) => {
    event.preventDefault();
    startRun('/api/runs', {
      ...state.settings,
      label: state.settings.label.trim() || `${state.settings.model} · ${state.preset === 'quick' ? '快速探索' : '稳定复核'}`,
      api_key: state.apiKey || undefined,
      authorized: $('#consent').checked,
    }, '容量探测');
  });
  $('#stop').addEventListener('click', stopActive);
  $('#speed-form').addEventListener('submit', startSpeed);
  $('#parallel-form').addEventListener('submit', startParallel);
  $('#quality-form').addEventListener('submit', startQuality);
  $('#schedule-form').addEventListener('submit', createSchedule);
  ['#speed-stop', '#parallel-stop', '#quality-stop'].forEach((id) => $(id).addEventListener('click', stopActive));
  $('#speed-consent').addEventListener('change', refreshButtons);
  $('#parallel-consent').addEventListener('change', refreshButtons);
  $('#quality-consent').addEventListener('change', refreshButtons);
  $('#schedule-consent').addEventListener('change', refreshButtons);
  $('#consent').addEventListener('change', refreshButtons);
  $('#export').addEventListener('click', () => exportRun(latest('capacity')));
  $('#parallel-export').addEventListener('click', () => exportRun(latest('parallel')));
  $('#quality-export').addEventListener('click', () => exportRun(latest('quality')));

  document.addEventListener('input', (event) => {
    const field = event.target.dataset.conn;
    if (!field) return;
    if (field === 'url') {
      state.settings.url = event.target.value;
      invalidateModels();
    } else if (field === 'key') {
      state.apiKey = event.target.value;
      invalidateModels();
    }
  });
  document.addEventListener('change', (event) => {
    const field = event.target.dataset.conn;
    if (field === 'protocol') {
      state.settings.protocol = event.target.value;
      invalidateModels();
    } else if (field === 'model') {
      state.settings.model = event.target.value;
      refreshButtons();
    }
  });
  document.addEventListener('click', (event) => {
    const fetchButton = event.target.closest('[data-conn="fetch"]');
    if (fetchButton) { fetchModels(); return; }
    const category = event.target.closest('[data-category]');
    if (category) {
      const name = category.dataset.category;
      state.categories = state.categories.includes(name)
        ? state.categories.filter((item) => item !== name)
        : [...state.categories, name];
      refreshCategoryChips();
      return;
    }
    const select = event.target.closest('[data-select]');
    if (select) {
      state.selectedId = select.dataset.select;
      render();
      return;
    }
    const box = event.target.closest('[data-compare]');
    if (box) {
      const id = box.dataset.compare;
      state.compare = box.checked ? [...state.compare, id] : state.compare.filter((item) => item !== id);
      renderHistory();
    }
  });

  $$('[data-goal]').forEach((button) => {
    button.addEventListener('click', () => {
      state.settings.goal = button.dataset.goal;
      renderSelection();
    });
  });
  $$('[data-preset]').forEach((button) => {
    button.addEventListener('click', () => {
      state.preset = button.dataset.preset;
      Object.assign(state.settings, PRESETS[state.preset]);
      $$('#advanced [data-field]').forEach((input) => { input.value = state.settings[input.dataset.field]; });
      renderSelection();
      renderBudget();
    });
  });

  $('#schedule-kind').addEventListener('change', () => {
    const kind = $('#schedule-kind').value;
    $$('[data-extra]').forEach((field) => { field.hidden = field.dataset.extra !== kind; });
  });

  $('#schedule-list').addEventListener('click', async (event) => {
    const runButton = event.target.closest('[data-schedule-run]');
    if (runButton) {
      const input = document.querySelector(`[data-key-for="${runButton.dataset.scheduleRun}"]`);
      try {
        await post(`/api/schedules/${runButton.dataset.scheduleRun}/run`, { api_key: input && input.value ? input.value : undefined });
        await loadSchedules();
        notice('已触发一次执行；结果会出现在实验记录里。');
      } catch (error) {
        notify(error.message || '立即执行失败');
      }
      return;
    }
    const toggle = event.target.closest('[data-schedule-toggle]');
    if (toggle) {
      const next = toggle.dataset.enabled !== 'true';
      try {
        await post(`/api/schedules/${toggle.dataset.scheduleToggle}/toggle`, { enabled: next });
        await loadSchedules();
      } catch (error) {
        notify(error.message || '操作失败');
      }
      return;
    }
    const remove = event.target.closest('[data-schedule-delete]');
    if (remove) {
      try {
        await api(`/api/schedules/${remove.dataset.scheduleDelete}`, { method: 'DELETE' });
        await loadSchedules();
      } catch (error) {
        notify(error.message || '删除失败');
      }
    }
  });

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) loadRuns().catch(() => {});
  });
}

function boot() {
  mountConnections();
  buildAdvanced();
  bind();
  const initialView = window.location.hash.replace('#', '');
  showView(TITLES[initialView] ? initialView : 'overview');
  render();
  loadRuns().catch(() => notify('无法读取实验服务，请确认 relay_lab 仍在运行。'));
  loadSchedules().catch(() => {});
  window.setInterval(() => {
    if (!document.hidden) loadRuns().catch(() => {});
  }, 2000);
  window.setInterval(() => {
    if (!document.hidden && state.view === 'schedules') loadSchedules().catch(() => {});
  }, 10000);
}

boot();

import {
  ArrowRight, BookOpen, ChartNoAxesColumn, Check, CircleCheck, Clock, FileSearch,
  Gauge, LayoutGrid, List, Orbit, Play, ShieldCheck, Sparkles,
} from 'lucide-react';
import { useEffect, useState } from 'react';

import { DotField } from '../components/DotField';

const TOP_NAV = [
  { id: 'top', label: '主页' },
  { id: 'experiments', label: '核心能力' },
  { id: 'console-preview', label: '控制台预览' },
  { id: 'fidelity', label: '协议保真度' },
  { id: 'ai-method', label: '测试口径' },
] as const;

/** 进入控制台之前的产品首页：未登录时跳转至独立登录页，已登录时直达控制台。 */
export function Landing({ user, onEnterConsole }: { user: string | null; onEnterConsole: () => void }) {
  const [previewTab, setPreviewTab] = useState<'matrix' | 'heat' | 'recent'>('matrix');
  const [activeNav, setActiveNav] = useState<string>('top');
  const [scrolled, setScrolled] = useState<boolean>(false);
  const signedIn = Boolean(user);

  useEffect(() => {
    document.title = 'Relay Lab · 模型渠道容量与协议保真度实验室';
  }, []);

  // 随页面滚动控制 New API 风格顶栏收缩变胶囊，并同步激活项
  useEffect(() => {
    const onScroll = () => {
      const y = window.scrollY;
      setScrolled(y > 24);
      if (y < 180) {
        setActiveNav('top');
        return;
      }
      const ids = ['console-preview', 'experiments', 'fidelity', 'ai-method'];
      let current = 'top';
      for (const id of ids) {
        const el = document.getElementById(id);
        if (el && el.getBoundingClientRect().top <= 140) current = id;
      }
      setActiveNav(current);
    };
    onScroll();
    window.addEventListener('scroll', onScroll, { passive: true });
    return () => window.removeEventListener('scroll', onScroll);
  }, []);

  const scrollToSection = (id: string) => {
    setActiveNav(id);
    if (id === 'top') {
      window.scrollTo({ top: 0, behavior: 'smooth' });
      return;
    }
    const target = document.getElementById(id);
    if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  return (
    <>
      <DotField />
      <div className="landing-page">
        {/* New API 风格：顶部通透展开，向下滚动平滑收缩为居中毛玻璃胶囊 */}
        <header className={`landing-header-wrap ${scrolled ? 'is-scrolled' : ''}`}>
          <div className="landing-header-pill">
            <a
              className="landing-header-brand"
              href="#top"
              onClick={(event) => {
                event.preventDefault();
                scrollToSection('top');
              }}
            >
              <img src="/logo.png" alt="Relay Lab" width={28} height={28} />
              <strong>Relay Lab</strong>
            </a>

            <div className="landing-header-right">
              <nav className="landing-header-nav" aria-label="页面导航">
                {TOP_NAV.map(({ id, label }) => (
                  <button
                    key={id}
                    type="button"
                    className={activeNav === id ? 'is-active' : ''}
                    onClick={() => scrollToSection(id)}
                  >
                    {label}
                  </button>
                ))}
                <button type="button" onClick={onEnterConsole}>控制台</button>
                <a href="/api/docs" target="_blank" rel="noreferrer">文档</a>
              </nav>

              <span className="landing-header-divider" aria-hidden="true" />

              <button type="button" className="landing-header-cta" onClick={onEnterConsole}>
                <span>{signedIn ? '进入控制台' : '登录'}</span>
                <ArrowRight size={14} strokeWidth={2.2} />
              </button>
            </div>
          </div>
        </header>

        {/* Hero 首屏与高保真控制台预览 */}
        <section id="top" className="landing-hero">
          <div className="landing-hero-copy">
            <div className="landing-eyebrow">
              <i className="landing-eyebrow-dot" aria-hidden="true" />
              <span>Relay Lab · LLM 模型渠道评测与协议保真度实验室</span>
            </div>

            <h1 className="landing-h1">
              看清模型渠道的真实容量
              <br />
              与请求层协议保真度
            </h1>

            <p className="landing-subtitle">
              不只测「答得多快」，更验证「答的是不是你问的」。统一覆盖四大线上模型协议，集五维横评矩阵、
              22+ 项请求层协议探针与只读 AI 诊断助手于一体，真实观测并保留不确定性。
            </p>

            <div className="landing-cta-row">
              <button type="button" className="landing-btn primary" onClick={onEnterConsole}>
                <span>{signedIn ? '回到控制台' : '登录进入控制台'}</span>
                <ArrowRight size={16} strokeWidth={2.2} />
              </button>
              <button type="button" className="landing-btn secondary" onClick={() => scrollToSection('fidelity')}>
                查看协议保真度能力
              </button>
            </div>

            <div className="landing-proto-strip" aria-label="支持的线上协议">
              {PROTOCOLS.map((name) => (
                <span key={name} className="landing-proto-pill">
                  <i aria-hidden="true" />
                  {name}
                </span>
              ))}
            </div>
          </div>

          {/* 控制台概览高保真预览窗口 */}
          <div id="console-preview" className="landing-mockup">
            <div className="landing-mockup-bar">
              <div className="landing-traffic" aria-hidden="true">
                <i className="r" />
                <i className="y" />
                <i className="g" />
              </div>
              <span className="landing-url-pill">Relay Lab Console · /#overview</span>
              <span className="landing-live-pill">
                <i aria-hidden="true" />
                CONSOLE PREVIEW
              </span>
            </div>

            <div className="landing-mockup-shell">
              {/* 左侧悬浮侧边栏 */}
              <aside className="landing-mock-sidebar" aria-hidden="true">
                <div className="landing-mock-brand">
                  <span className="mark"><img src="/logo.png" alt="" width={28} height={28} /></span>
                  <strong>Relay Lab</strong>
                  <small>CONSOLE</small>
                </div>

                <div className="landing-mock-nav">
                  {MOCK_NAV.map(({ title, icon: Icon, active }) => (
                    <div key={title} className={`landing-mock-nav-item ${active ? 'is-active' : ''}`}>
                      <Icon size={15} strokeWidth={1.8} />
                      <span>{title}</span>
                    </div>
                  ))}
                  <div className="landing-mock-nav-item is-ai">
                    <Orbit size={15} strokeWidth={1.8} />
                    <span>Orbit</span>
                    <em>⌘K</em>
                  </div>
                </div>

                <div className="landing-mock-foot">
                  <span className="landing-mock-avatar">AD</span>
                  <span className="landing-mock-who">
                    <strong>admin</strong>
                    <small>已登录</small>
                  </span>
                  <BookOpen size={14} strokeWidth={1.8} />
                </div>
              </aside>

              {/* 右侧主工作台 */}
              <div className="landing-mock-workspace">
                <div className="landing-mock-header">
                  <div className="landing-mock-title-row">
                    <strong>概览</strong>
                    <span className="landing-mock-scope">全站</span>
                    <span className="landing-mock-summary">
                      站点 <b>4</b> · 模型 <b>6</b> · 存在高危 <b className="is-hot">2</b> 个对象
                    </span>
                  </div>
                  <div className="landing-mock-shortcuts" aria-hidden="true">
                    <span>速度</span>
                    <span>容量</span>
                    <span>协议</span>
                  </div>
                </div>

                <div className="landing-mock-toolbar" aria-hidden="true">
                  <span className="landing-mock-select">全部站点 ▾</span>
                  <span className="landing-mock-select">全部协议 ▾</span>
                  <span className="landing-mock-search">搜索模型…</span>
                  <div className="landing-mock-seg">
                    <span className="is-on">按站点 × 模型</span>
                    <span>同模型比站点</span>
                  </div>
                  <span className="landing-mock-export">导出 Markdown</span>
                </div>

                <div className={`landing-mock-main ${previewTab === 'matrix' ? 'has-radar' : ''}`}>
                  <div className="landing-mock-board">
                    <div className="landing-mock-board-head">
                      <div className="landing-mock-tabs" role="tablist" aria-label="控制台预览视图">
                        <button
                          type="button"
                          role="tab"
                          aria-selected={previewTab === 'matrix'}
                          className={previewTab === 'matrix' ? 'is-on' : ''}
                          onClick={() => setPreviewTab('matrix')}
                        >
                          五维矩阵
                        </button>
                        <button
                          type="button"
                          role="tab"
                          aria-selected={previewTab === 'heat'}
                          className={previewTab === 'heat' ? 'is-on' : ''}
                          onClick={() => setPreviewTab('heat')}
                        >
                          协议热力
                        </button>
                        <button
                          type="button"
                          role="tab"
                          aria-selected={previewTab === 'recent'}
                          className={previewTab === 'recent' ? 'is-on' : ''}
                          onClick={() => setPreviewTab('recent')}
                        >
                          最近实验
                        </button>
                      </div>
                      <small>基于最近 50 次实验</small>
                    </div>

                    {previewTab === 'matrix' && (
                      <div className="landing-mock-table-wrap">
                        <table className="landing-mock-table">
                          <thead>
                            <tr>
                              <th>模型 / 上游渠道</th>
                              <th>速度</th>
                              <th>并行</th>
                              <th>容量</th>
                              <th>质量</th>
                              <th>协议</th>
                            </tr>
                          </thead>
                          <tbody>
                            {MOCK_MATRIX.map((row) => (
                              <tr key={`${row.host}-${row.model}`} className={row.picked ? 'is-picked' : ''}>
                                <td>
                                  <strong className="m-model">{row.model}</strong>
                                  <span className="m-sub">
                                    <em className="m-host">{row.host}</em>
                                    {row.proto}
                                  </span>
                                </td>
                                <td>
                                  <span className="m-val">
                                    <b>{row.speed[0]}</b>
                                    {row.speed[2] && <i className="m-best" />}
                                  </span>
                                  <small>{row.speed[1]}</small>
                                </td>
                                <td>
                                  <span className="m-val">
                                    <b>{row.parallel[0]}</b>
                                    {row.parallel[2] && <i className="m-best" />}
                                  </span>
                                  <small>{row.parallel[1]}</small>
                                </td>
                                <td>
                                  <span className="m-val">
                                    <b>{row.capacity[0]}</b>
                                    {row.capacity[2] && <i className="m-best" />}
                                  </span>
                                  <small>{row.capacity[1]}</small>
                                </td>
                                <td>
                                  <span className="m-val">
                                    <b>{row.quality[0]}</b>
                                    {row.quality[2] && <i className="m-best" />}
                                  </span>
                                  <small>{row.quality[1]}</small>
                                </td>
                                <td>
                                  <span className="m-val">
                                    <b className={row.alert ? 'is-bad' : ''}>{row.fidelity}</b>
                                    {row.alert ? (
                                      <span className="m-alert">{row.alert}</span>
                                    ) : (
                                      <i className="m-best" />
                                    )}
                                  </span>
                                  <small>有效项 {row.passed}</small>
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}

                    {previewTab === 'heat' && (
                      <div className="landing-mock-heat">
                        {MOCK_HEAT.map((item) => (
                          <div key={item.name} className="landing-mock-heat-row">
                            <div className="landing-mock-heat-who">
                              <strong>{item.name}</strong>
                              <small>{item.host}</small>
                            </div>
                            <div className="landing-mock-heat-cells">
                              {item.checks.map(([label, state]) => (
                                <span key={label} className={`heat-pill is-${state}`}>
                                  {label}
                                </span>
                              ))}
                            </div>
                          </div>
                        ))}
                      </div>
                    )}

                    {previewTab === 'recent' && (
                      <div className="landing-mock-recent">
                        {MOCK_RECENT.map((run) => (
                          <div key={run.title} className="landing-mock-recent-row">
                            <i className={`recent-dot is-${run.status}`} />
                            <div className="recent-main">
                              <strong>{run.title}</strong>
                              <small>{run.meta}</small>
                            </div>
                            <div className="recent-right">
                              <b>{run.state}</b>
                              <small>{run.note}</small>
                            </div>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  {previewTab === 'matrix' && (
                    <div className="landing-mock-radar-card">
                      <div className="landing-mock-radar-head">
                        <strong>五维轮廓</strong>
                        <span>覆盖最全前 3</span>
                      </div>
                      <svg
                        className="landing-radar-svg"
                        viewBox="0 0 250 176"
                        role="img"
                        aria-label="五维轮廓雷达图示例"
                      >
                        <g transform="translate(125,92)">
                          <polygon points="0,-58 55.2,-17.9 34.1,46.9 -34.1,46.9 -55.2,-17.9" fill="none" stroke="#d2d2da" strokeWidth="1" />
                          <polygon points="0,-40 38.0,-12.4 23.5,32.4 -23.5,32.4 -38.0,-12.4" fill="none" stroke="#e4e4ea" strokeWidth="1" />
                          <polygon points="0,-22 20.9,-6.8 12.9,17.8 -12.9,17.8 -20.9,-6.8" fill="none" stroke="#e4e4ea" strokeWidth="1" />
                          <line x1="0" y1="0" x2="0" y2="-58" stroke="#e4e4ea" strokeWidth="1" />
                          <line x1="0" y1="0" x2="55.2" y2="-17.9" stroke="#e4e4ea" strokeWidth="1" />
                          <line x1="0" y1="0" x2="34.1" y2="46.9" stroke="#e4e4ea" strokeWidth="1" />
                          <line x1="0" y1="0" x2="-34.1" y2="46.9" stroke="#e4e4ea" strokeWidth="1" />
                          <line x1="0" y1="0" x2="-55.2" y2="-17.9" stroke="#e4e4ea" strokeWidth="1" />
                          <polygon points="0,-54 44,-14 27,37 -30,41 -53,-17" fill="#141414" fillOpacity="0.10" stroke="#141414" strokeWidth="1.8" />
                          <polygon points="0,-38 54,-17 33,45 -32,44 -36,-12" fill="#816df8" fillOpacity="0.18" stroke="#816df8" strokeWidth="1.8" />
                          <polygon points="0,-44 37,-12 22,31 -27,37 -49,-16" fill="#0f9d6e" fillOpacity="0.12" stroke="#0f9d6e" strokeWidth="1.6" />
                          <circle cx="0" cy="-54" r="2.8" fill="#141414" />
                          <circle cx="54" cy="-17" r="2.8" fill="#816df8" />
                          <circle cx="33" cy="45" r="2.8" fill="#816df8" />
                          <circle cx="-53" cy="-17" r="2.8" fill="#141414" />
                        </g>
                        <text x="125" y="18" textAnchor="middle" className="landing-radar-axis">速度</text>
                        <text x="194" y="76" textAnchor="start" className="landing-radar-axis">并行</text>
                        <text x="169" y="158" textAnchor="middle" className="landing-radar-axis">容量</text>
                        <text x="81" y="158" textAnchor="middle" className="landing-radar-axis">质量</text>
                        <text x="56" y="76" textAnchor="end" className="landing-radar-axis">协议</text>
                      </svg>
                      <ul className="landing-mock-radar-legend">
                        <li><i style={{ background: '#141414' }} /><span>claude-sonnet-4</span><em>5/5 维</em></li>
                        <li><i style={{ background: '#816df8' }} /><span>gpt-5</span><em>5/5 维</em></li>
                        <li><i style={{ background: '#0f9d6e' }} /><span>kimi-k2.5</span><em>5/5 维</em></li>
                      </ul>
                    </div>
                  )}
                </div>
              </div>
            </div>
          </div>
        </section>

        {/* 4 列核心 KPI 条 */}
        <section className="landing-kpi-grid" aria-label="核心能力指标">
          {KPIS.map((item) => (
            <article key={item.title} className="landing-kpi-card">
              <b>{item.value}</b>
              <h3>{item.title}</h3>
              <p>{item.desc}</p>
            </article>
          ))}
        </section>

        {/* 五类核心实验能力矩阵 */}
        <section id="experiments" className="landing-section">
          <div className="landing-section-head">
            <span className="landing-kicker">FIVE CORE EXPERIMENTS</span>
            <h2>五类核心实验，全面刻画模型渠道真实表现</h2>
            <p>
              所有实验共用同一套多协议适配层与预算熔断机制，从轻量测速、质量抽查到规模容量搜索与请求层协议审计，一站式完成闭环。
            </p>
          </div>

          <div className="landing-bento-row-2">
            {/* 容量探测 */}
            <article className="landing-exp-card is-large">
              <div className="landing-exp-top">
                <span className="landing-exp-icon"><Play size={19} strokeWidth={2} /></span>
                <div className="landing-exp-badges">
                  <span className="landing-mono-tag">POST /api/runs</span>
                </div>
              </div>
              <h3>容量探测 · Capacity</h3>
              <p>
                回答「这个站点的 RPM / TPM 上限在哪」。自动执行倍增探测与二分复测，结合成功率下限、
                TTFT/TPOT P99 约束与预算封顶，达上限时仅报已观测下界。
              </p>
              <div className="landing-exp-viz">
                <div>
                  <small>SEARCH CONVERGENCE · RPM / TPM</small>
                  <strong>320 RPM · 合格上界</strong>
                  <span>倍增 5 档 → 二分复测 3 轮 · 70% 安全折算</span>
                </div>
                <svg viewBox="0 0 210 62" className="landing-exp-svg" aria-hidden="true">
                  <line x1="6" y1="22" x2="204" y2="22" stroke="#0f9d6e" strokeWidth="1" strokeDasharray="4 4" />
                  <polyline
                    points="8,54 46,44 86,28 128,12 162,22 198,22"
                    stroke="#141414"
                    strokeWidth="2.2"
                    fill="none"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                  <circle cx="46" cy="44" r="3.2" fill="#141414" />
                  <circle cx="86" cy="28" r="3.2" fill="#141414" />
                  <circle cx="128" cy="12" r="3.8" fill="#c0262d" />
                  <circle cx="162" cy="22" r="3.8" fill="#0f9d6e" />
                  <circle cx="198" cy="22" r="4.2" fill="#0f9d6e" stroke="#fff" strokeWidth="1.5" />
                </svg>
              </div>
              <div className="landing-tag-row">
                <span>倍增 + 二分复测</span>
                <span>滑动窗口 TPM</span>
                <span>请求与时长预算封顶</span>
              </div>
            </article>

            {/* 协议保真度 */}
            <article className="landing-exp-card is-large is-accent">
              <div className="landing-exp-top">
                <span className="landing-exp-icon is-accent"><FileSearch size={19} strokeWidth={2} /></span>
                <div className="landing-exp-badges">
                  <span className="landing-accent-tag">核心亮点</span>
                  <span className="landing-mono-tag">POST /api/contract</span>
                </div>
              </div>
              <h3>协议保真度 · Contract</h3>
              <p>
                回答「上游服务有没有暗改我的请求」。直接探测请求层 22+ 项高信号能力，揪出丢 tools、
                假流式缓冲、篡改 max_tokens 与缓存写档作假。
              </p>
              <div className="landing-exp-viz">
                <div>
                  <small>WIRE AUDIT MATRIX · 22 CHECKS</small>
                  <strong>95.5% · 21/22 通过</strong>
                  <span>支持快速 / 全量双模式，每项可重复 1–3 次</span>
                </div>
                <svg viewBox="0 0 210 62" className="landing-exp-svg" aria-hidden="true">
                  <rect x="6" y="8" width="24" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                  <rect x="36" y="8" width="24" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                  <rect x="66" y="8" width="24" height="20" rx="6" fill="#fff3f2" stroke="#c0262d" strokeWidth="1.4" />
                  <rect x="96" y="8" width="24" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                  <rect x="126" y="8" width="24" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                  <rect x="156" y="8" width="24" height="20" rx="6" fill="#f1f1f3" stroke="#d8d8de" strokeWidth="1.2" />
                  <rect x="186" y="8" width="18" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                  <rect x="6" y="34" width="24" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                  <rect x="36" y="34" width="24" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                  <rect x="66" y="34" width="24" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                  <rect x="96" y="34" width="24" height="20" rx="6" fill="#f3f0ff" stroke="#816df8" strokeWidth="1.4" />
                  <rect x="126" y="34" width="24" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                  <rect x="156" y="34" width="24" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                  <rect x="186" y="34" width="18" height="20" rx="6" fill="#ecfdf5" stroke="#0f9d6e" strokeWidth="1.2" />
                </svg>
              </div>
              <div className="landing-tag-row">
                <span>高危项分级标红</span>
                <span>多协议一键矩阵</span>
                <span>1h/5m 缓存闭环</span>
              </div>
            </article>
          </div>

          <div className="landing-bento-row-3">
            {SECONDARY_EXPS.map(({ title, endpoint, desc, tags, icon: Icon }) => (
              <article key={title} className="landing-exp-card">
                <div className="landing-exp-top">
                  <span className="landing-exp-icon"><Icon size={18} strokeWidth={2} /></span>
                  <span className="landing-mono-tag">{endpoint}</span>
                </div>
                <h3>{title}</h3>
                <p>{desc}</p>
                <div className="landing-tag-row">
                  {tags.map((tag) => <span key={tag}>{tag}</span>)}
                </div>
              </article>
            ))}
          </div>
        </section>

        {/* 协议保真度亮点专区 */}
        <section id="fidelity" className="landing-section">
          <div className="landing-panel landing-fidelity-grid">
            <div className="landing-fidelity-left">
              <span className="landing-kicker">PROTOCOL FIDELITY AUDIT</span>
              <h2>
                容量只说明答得多快，
                <br />
                协议检查才证明答的是你问的
              </h2>
              <p className="landing-lead">
                上游服务常见暗改——丢弃 tools、忽略 stop、篡改 max_tokens、把流式缓冲成一次性、只转发最后一条消息——每一条都会让表面吞吐虚高。
              </p>

              <div className="landing-principles">
                {PRINCIPLES.map(([num, title, body]) => (
                  <div key={num} className="landing-principle-item">
                    <span className="landing-num-badge">{num}</span>
                    <div>
                      <strong>{title}</strong>
                      <p>{body}</p>
                    </div>
                  </div>
                ))}
              </div>
            </div>

            <div className="landing-audit-panel">
              <div className="landing-audit-head">
                <div>
                  <strong>协议保真度审计 · relay-beta.dev</strong>
                  <small>模型 gpt-5 · 全量检查模式 · 重复 2 次</small>
                </div>
                <div className="landing-audit-badges">
                  <span className="landing-mono-tag is-white">保真度 86.4%</span>
                  <span className="landing-bad-tag">高危违规 2</span>
                </div>
              </div>

              <div className="landing-audit-rows">
                {AUDIT_CHECKS.map((item) => (
                  <div key={item.name} className={`landing-audit-row ${item.bad ? 'is-bad' : ''}`}>
                    <span className="landing-audit-proto">{item.proto}</span>
                    <div className="landing-audit-info">
                      <strong>{item.name}</strong>
                      <small>{item.evidence}</small>
                    </div>
                    <span className={`landing-audit-verdict ${item.bad ? 'is-bad' : 'is-ok'}`}>
                      {item.verdict}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </section>

        {/* AI 诊断助手 & 测试口径双栏 */}
        <section id="ai-method" className="landing-section">
          <div className="landing-dual-grid">
            <article className="landing-dual-card is-accent">
              <div className="landing-dual-top">
                <span className="landing-dual-kicker is-accent">
                  <Sparkles size={16} strokeWidth={2} />
                  AI COPILOT · AGENTS SDK
                </span>
                <span className="landing-accent-tag">Ctrl + K</span>
              </div>
              <h3>基于实测报告的只读 AI 诊断助手</h3>
              <p className="landing-dual-desc">
                采用 OpenAI Agents SDK 流式编排，不改写实验引擎、不触发压测流量。支持六类终态报告缓存解读、跨报告对比与安全参数草稿生成。
              </p>

              <div className="landing-chat-preview">
                <div className="landing-chat-user">
                  对比最近两次 gpt-5 协议检查，为什么保真度下降了？
                </div>
                <div className="landing-chat-tools">
                  <span className="is-ok">✓ list_runs (2)</span>
                  <span className="is-ok">✓ diff_contract_checks</span>
                  <span className="is-dim">SSE · 只读工具</span>
                </div>
                <div className="landing-chat-reply">
                  <p>
                    与上周基准相比，本次新增 2 项高危违规：stream 仅返回单块缓冲（跨度 4 ms），且 thinking_roundtrip
                    在二轮回传时丢失签名块。建议开启 per_check=2 针对这两项定向复测。
                  </p>
                  <div className="landing-chat-draft">
                    <span>已生成「协议复测」参数草稿（不含 Key）</span>
                    <button type="button" onClick={onEnterConsole}>回填确认 →</button>
                  </div>
                </div>
              </div>

              <div className="landing-suggest-row" aria-hidden="true">
                <span>对比最近两次同类型实验</span>
                <span>推荐压测参数</span>
                <span>列出高危违规</span>
              </div>
            </article>

            <article className="landing-dual-card">
              <div className="landing-dual-top">
                <span className="landing-dual-kicker is-ok">
                  <ShieldCheck size={16} strokeWidth={2} />
                  HONEST METHODOLOGY
                </span>
              </div>
              <h3>真实观测，保留不确定性</h3>
              <p className="landing-dual-desc">
                不粉饰均值、不画假零线、不在数据缺失时判为达标。每一项工程口径都写在代码与报告边界里。
              </p>

              <div className="landing-method-list">
                {METHOD_ITEMS.map(([num, title, desc]) => (
                  <div key={num} className="landing-method-item">
                    <span className="landing-num-badge is-dark">{num}</span>
                    <div>
                      <strong>{title}</strong>
                      <p>{desc}</p>
                    </div>
                  </div>
                ))}
              </div>
            </article>
          </div>
        </section>

        {/* 底部控制台跳转入口 */}
        <section className="landing-section">
          <div className="landing-panel landing-cta-panel">
            <div className="landing-entry-left">
              <span className="landing-kicker">CONSOLE ACCESS · READY TO MEASURE</span>
              <h2>
                准备好开始第一轮真实观测了吗？
              </h2>
              <p className="landing-lead">
                一次填写上游接口地址、协议与模型 ID，即可在速度、并行、容量、质量与协议检查五个实验页间自动同步；随时通过
                Ctrl+K 呼出只读 AI 诊断助手分析报告。
              </p>

              <ul className="landing-entry-bullets">
                {ENTRY_BULLETS.map((text) => (
                  <li key={text}>
                    <span className="landing-check-dot"><Check size={12} strokeWidth={2.8} /></span>
                    <span>{text}</span>
                  </li>
                ))}
              </ul>
            </div>

            <div className="landing-cta-side">
              <div className="landing-cta-actions">
                <button type="button" className="landing-btn primary" onClick={onEnterConsole}>
                  <span>{signedIn ? '进入控制台' : '前往登录控制台'}</span>
                  <ArrowRight size={16} strokeWidth={2.2} />
                </button>
                <a className="landing-btn secondary" href="/api/docs" target="_blank" rel="noreferrer">
                  浏览 API 文档
                </a>
              </div>
              <small className="landing-cta-note">
                {signedIn
                  ? `当前已登录账号 ${user}，点击即可直接回到控制台工作台。`
                  : '使用管理员账号登录后即可管理实验、查看历史报告与定时任务。'}
              </small>
            </div>
          </div>
        </section>

        {/* 页脚 */}
        <footer className="landing-footer">
          <div className="landing-footer-inner">
            <div className="landing-footer-left">
              <span className="landing-brand is-footer">
                <span className="mark"><img src="/logo.png" alt="" width={32} height={32} /></span>
                <strong>Relay Lab</strong>
                <small>CONSOLE</small>
              </span>
              <span className="landing-footer-note">
                自有控制器 + EvalScope 压测引擎 · 真实观测，保留不确定性
              </span>
            </div>

            <div className="landing-footer-links">
              <a href="/api/docs" target="_blank" rel="noreferrer">API 文档</a>
              <button type="button" onClick={() => scrollToSection('fidelity')}>协议探针清单</button>
              <button type="button" onClick={() => scrollToSection('ai-method')}>测试口径</button>
              <button type="button" onClick={onEnterConsole}>{signedIn ? '进入控制台' : '登录控制台'}</button>
            </div>
          </div>
        </footer>
      </div>
    </>
  );
}

const PROTOCOLS = [
  'OpenAI Chat Completions',
  'OpenAI Responses',
  'Anthropic Messages',
  'Google Gemini',
];

const MOCK_NAV = [
  { title: '概览', icon: LayoutGrid, active: true },
  { title: '速度测试', icon: Gauge, active: false },
  { title: '并行测试', icon: ChartNoAxesColumn, active: false },
  { title: '容量探测', icon: Play, active: false },
  { title: '模型质量', icon: CircleCheck, active: false },
  { title: '协议检查', icon: FileSearch, active: false },
  { title: '定时任务', icon: Clock, active: false },
  { title: '实验记录', icon: List, active: false },
  { title: '测试口径', icon: ShieldCheck, active: false },
];

const MOCK_MATRIX = [
  {
    model: 'claude-sonnet-4',
    host: 'relay-alpha.io',
    proto: 'Anthropic',
    picked: true,
    speed: ['412 ms', 'P95 620 ms', true] as const,
    parallel: ['28,400', '并发 14.2', false] as const,
    capacity: ['320', '合格容量', false] as const,
    quality: ['93.8%', '30/32', false] as const,
    fidelity: '100%',
    passed: '22/22',
    alert: '',
  },
  {
    model: 'gpt-5',
    host: 'relay-beta.dev',
    proto: 'Responses',
    picked: false,
    speed: ['538 ms', 'P95 940 ms', false] as const,
    parallel: ['34,120', '并发 18.6', true] as const,
    capacity: ['410', '合格容量', true] as const,
    quality: ['95.2%', '31/32', true] as const,
    fidelity: '86.4%',
    passed: '19/22',
    alert: '高危 2 · 假流式',
  },
  {
    model: 'kimi-k2.5',
    host: 'relay-gamma.cn',
    proto: 'Chat · Ant',
    picked: false,
    speed: ['485 ms', 'P95 710 ms', false] as const,
    parallel: ['22,950', '并发 11.4', false] as const,
    capacity: ['260', '吞吐折算', false] as const,
    quality: ['90.6%', '29/32', false] as const,
    fidelity: '95.5%',
    passed: '21/22',
    alert: '',
  },
  {
    model: 'gemini-2.5-pro',
    host: 'relay-alpha.io',
    proto: 'Gemini',
    picked: false,
    speed: ['610 ms', 'P95 1020 ms', false] as const,
    parallel: ['19,800', '并发 9.8', false] as const,
    capacity: ['240', '合格容量', false] as const,
    quality: ['92.3%', '24/26', false] as const,
    fidelity: '90.9%',
    passed: '20/22',
    alert: '高危 1 · 思考丢签',
  },
];

const MOCK_HEAT = [
  {
    name: 'claude-sonnet-4',
    host: 'relay-alpha.io',
    checks: [
      ['tools ✓', 'ok'], ['stream ✓', 'ok'], ['thinking ✓', 'ok'], ['json_schema ✓', 'ok'], ['cache_1h ✓', 'ok'],
    ] as const,
  },
  {
    name: 'gpt-5',
    host: 'relay-beta.dev',
    checks: [
      ['tools ✓', 'ok'], ['stream ✕', 'bad'], ['thinking ✕', 'bad'], ['json_schema ✓', 'ok'], ['cache_1h —', 'dim'],
    ] as const,
  },
  {
    name: 'kimi-k2.5',
    host: 'relay-gamma.cn',
    checks: [
      ['tools ✓', 'ok'], ['stream ✓', 'ok'], ['thinking ✓', 'ok'], ['json_schema ✓', 'ok'], ['cache_1h ✓', 'ok'],
    ] as const,
  },
];

const MOCK_RECENT = [
  { title: '协议检查 · gpt-5', meta: 'relay-beta.dev · Responses', state: '已完成', note: '高危 2', status: 'bad' },
  { title: '容量探测 · claude-sonnet-4', meta: 'relay-alpha.io · Anthropic', state: '已完成', note: '320 RPM', status: 'ok' },
  { title: '质量抽查 · kimi-k2.5', meta: 'relay-gamma.cn · Chat Completions', state: '已完成', note: '90.6%', status: 'ok' },
];

const KPIS = [
  { value: '5 类', title: '核心实验维度', desc: '速度、并行、容量、质量与协议保真度一站覆盖' },
  { value: '4 种', title: '线上协议统一适配', desc: 'Chat Completions · Responses · Anthropic · Gemini' },
  { value: '22+', title: '项请求层协议探针', desc: '直查工具链路、思考续传、真流式与缓存写档头' },
  { value: '全留痕', title: 'Wire 证据与凭据隔离', desc: '多轮请求与响应原文逐项可查，API Key 绝不落盘' },
];

const SECONDARY_EXPS = [
  {
    title: '速度测试 · Speed',
    endpoint: 'POST /api/speed',
    icon: Gauge,
    desc: '少量请求快速采样 TTFT 与 TPOT 的 P50 / P95 / P99 分位延迟，看清首字响应抖动与尾延迟。',
    tags: ['P50 / P95 / P99', '少量流量'],
  },
  {
    title: '并行测试 · Parallel',
    endpoint: 'POST /api/parallel',
    icon: ChartNoAxesColumn,
    desc: '并发逐档攀升，结合 Little\'s Law 核算「实测达成并发」与峰值输出 TPM，识别限流排队拐点。',
    tags: ['Little\'s Law 校验', '吞吐拐点'],
  },
  {
    title: '模型质量 · Quality',
    endpoint: 'POST /api/quality',
    icon: CircleCheck,
    desc: '内置 MMLU 多科精选子集与 52 道固定冒烟题，本地确定性规则判分，快速发现偷换低配模型。',
    tags: ['52 道内置题', '本地确定性判分'],
  },
];

const PRINCIPLES: [string, string, string][] = [
  ['01', '有证据才判 violated', '站点不支持某能力（如未实现 json_schema）记为 inconclusive，不摊进保真度分母。'],
  ['02', '缓存只认服务端计数', '严格核对 cached / 1h 写入响应头与 ≥15 秒复发闭环，绝不凭响应耗时猜命中。'],
  ['03', '完整 Wire 请求/响应留证', '每项探针自动留存请求头、多轮请求体、响应头与缺失头列表，一键展开复核。'],
];

const AUDIT_CHECKS = [
  { proto: 'openai', name: '工具调用 · tools', evidence: '恰好 1 次 lookup_ticket(T-1024)，call_id 与 finish_reason 合规', verdict: '✓ 通过', bad: false },
  { proto: 'openai', name: '流式真实性 · stream', evidence: '仅 1 块缓冲返回（跨度 4 ms），疑似假流式包装', verdict: '✕ 高危违规', bad: true },
  { proto: 'anthropic', name: '思考续传 · thinking_roundtrip', evidence: '二轮回传签名后丢弃 thinking 块，未能答出 n×2', verdict: '✕ 高危违规', bad: true },
  { proto: 'responses', name: '结构化输出 · json_schema', evidence: '严格匹配字段类型，无代码围栏与尾随解释文本', verdict: '✓ 通过', bad: false },
  { proto: 'anthropic', name: '1h 缓存闭环 · cache_roundtrip', evidence: '冷写 >0 且等待 15s 重发命中 cache_read = 2,480', verdict: '✓ 通过', bad: false },
  { proto: 'openai', name: '输出上限 · max_tokens', evidence: 'completion_tokens = 24，finish_reason = length', verdict: '✓ 通过', bad: false },
];

const METHOD_ITEMS: [string, string, string][] = [
  ['01', '尾随滑动窗口 TPM', '按每请求完成时间重建尾随窗口，一眼识破「前猛后塌」的均值假象。'],
  ['02', 'Little\'s Law 实测并发', '以吞吐 × 平均延迟核算真实达成并发，与请求并发档位分开记录。'],
  ['03', '代理环境自动自愈', '自动清理导致 httpx 崩溃的方括号 IPv6 NO_PROXY 条目并记录降级。'],
  ['04', '凭据隔离与权限边界', 'API Key 仅用于当前测试会话，持久化的实验报告与 AI 对话均不含任何凭据。'],
];

const ENTRY_BULLETS = [
  '五类实验共享连接配置，支持一键拉取上游模型列表',
  '自动汇总五维横评矩阵、协议热力图与 Markdown 报告导出',
  '支持定时任务巡检，API Key 仅留存在当前会话内存',
];

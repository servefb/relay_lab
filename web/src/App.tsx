import {
  BookOpen, ChartNoAxesColumn, CircleCheck, Clock, FileSearch, Gauge, Home, LayoutGrid, List, LogOut, Orbit, Play, RefreshCw, ShieldCheck, X,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ComponentType } from 'react';

import { AiConfigModal } from './components/AiConfigModal';
import { AiDrawer } from './components/AiDrawer';
import { DotField } from './components/DotField';
import { useLab } from './store';
import { Capacity } from './views/Capacity';
import { Contract } from './views/Contract';
import { History } from './views/History';
import { Method } from './views/Method';
import { Overview } from './views/Overview';
import { Parallel } from './views/Parallel';
import { Quality } from './views/Quality';
import { Schedules } from './views/Schedules';
import { Speed } from './views/Speed';

interface NavItem {
  id: string;
  title: string;
  icon: ComponentType<{ size?: number; strokeWidth?: number }>;
}

const NAV: NavItem[] = [
  { id: 'overview', title: '概览', icon: LayoutGrid },
  { id: 'speed', title: '速度测试', icon: Gauge },
  { id: 'parallel', title: '并行测试', icon: ChartNoAxesColumn },
  { id: 'capacity', title: '容量探测', icon: Play },
  { id: 'quality', title: '模型质量', icon: CircleCheck },
  { id: 'contract', title: '协议检查', icon: FileSearch },
  { id: 'schedules', title: '定时任务', icon: Clock },
  { id: 'history', title: '实验记录', icon: List },
  { id: 'method', title: '测试口径', icon: ShieldCheck },
];

/** Old bookmarks used `#new` for the capacity page. */
const ALIASES: Record<string, string> = { new: 'capacity' };

/**
 * Wraps a nav icon so hover can redraw it stroke by stroke. Setting
 * pathLength=1 normalises every shape to one unit, which lets a single CSS
 * stroke-dashoffset animation draw strokes of any real length identically.
 */
function NavIcon({ icon: Icon }: { icon: NavItem['icon'] }) {
  const host = useRef<HTMLSpanElement | null>(null);
  useEffect(() => {
    host.current
      ?.querySelectorAll('path, line, circle, rect, polyline, polygon, ellipse')
      .forEach((shape) => shape.setAttribute('pathLength', '1'));
  }, []);
  return (
    <span className="nav-icon" ref={host}>
      <Icon size={18} strokeWidth={1.7} />
    </span>
  );
}

function viewFromHash(): string {
  const raw = window.location.hash.replace('#', '');
  const view = ALIASES[raw] ?? raw;
  return NAV.some((item) => item.id === view) ? view : 'overview';
}

function Logo() {
  return <img src="/logo.png" alt="" width={36} height={36} />;
}

export function App({ user, onHome, onLogout }: { user: string; onHome: () => void; onLogout: () => void }) {
  const [view, setView] = useState(viewFromHash);
  const {
    alert, notice, clearAlert, showNotice, loadRuns, loadSchedules, showAlert,
    drawerOpen, setDrawerOpen, openAiDrawer, configModalOpen, closeAiConfig, aiConfig, ensureAiConfig,
  } = useLab();

  useEffect(() => {
    const onHash = () => setView(viewFromHash());
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, []);

  useEffect(() => {
    void ensureAiConfig();
  }, [ensureAiConfig]);

  // Toast auto-dismisses: a launch confirmation does not need a click to go away.
  useEffect(() => {
    if (!notice) return;
    const timer = window.setTimeout(() => showNotice(''), 4000);
    return () => window.clearTimeout(timer);
  }, [notice, showNotice]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault();
        if (drawerOpen) setDrawerOpen(false);
        else openAiDrawer();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [drawerOpen, openAiDrawer, setDrawerOpen]);

  const navigate = (next: string) => {
    setView(next);
    if (window.location.hash !== `#${next}`) window.history.replaceState(null, '', `#${next}`);
  };

  const refresh = () => {
    loadRuns().catch((error: Error) => showAlert(error.message));
    loadSchedules().catch(() => {});
  };

  const current = NAV.find((item) => item.id === view) ?? NAV[0];
  const aiReady = Boolean(aiConfig?.configured && aiConfig.has_key);

  useEffect(() => {
    document.title = `${current.title} · Relay Lab`;
  }, [current.title]);

  return (
    <>
    <DotField />
    <div className="shell">
      <aside className="sidebar">
        <button type="button" className="brand brand-link" title="返回首页" onClick={onHome}>
          <span className="mark"><Logo /></span>
          <strong>Relay Lab</strong>
          <small>CONSOLE</small>
        </button>

        <nav className="nav">
          {NAV.map(({ id, title, icon }) => (
            <button key={id} type="button" aria-current={id === view ? 'page' : undefined} onClick={() => navigate(id)}>
              <NavIcon icon={icon} />
              <span>{title}</span>
            </button>
          ))}
          <button
            type="button"
            className="ai-nav-btn"
            aria-pressed={drawerOpen}
            onClick={() => (drawerOpen ? setDrawerOpen(false) : openAiDrawer())}
            title={aiReady ? 'Orbit 已就绪（Ctrl+K）' : 'Orbit 未配置（Ctrl+K）'}
          >
            <NavIcon icon={Orbit} />
            <span>Orbit</span>
            <i className={`ai-nav-pip ${aiReady ? 'on' : ''}`} aria-hidden />
          </button>
        </nav>

        <div className="side-foot">
          <span className="avatar">{user.slice(0, 2).toUpperCase()}</span>
          <span className="who"><strong>{user}</strong><small>已登录</small></span>
          <button type="button" className="icon-btn" title="返回首页" aria-label="返回首页" onClick={onHome}><Home size={16} /></button>
          <button type="button" className="icon-btn" title="刷新" aria-label="刷新" onClick={refresh}><RefreshCw size={16} /></button>
          <a className="icon-btn" title="API 文档" aria-label="API 文档" href="/api/docs" target="_blank" rel="noreferrer">
            <BookOpen size={16} />
          </a>
          <button type="button" className="icon-btn" title="退出登录" aria-label="退出登录" onClick={onLogout}>
            <LogOut size={16} />
          </button>
        </div>
      </aside>

      <main className="main">
        {view !== 'overview' && (
          <header className="topbar">
            <h1>{current.title}</h1>
          </header>
        )}

        {alert && (
          <div className="toast error" role="alert">
            <span>{alert}</span>
            <button type="button" aria-label="关闭" onClick={clearAlert}><X size={14} /></button>
          </div>
        )}
        {notice && !alert && (
          <div className="toast" role="status">
            <CircleCheck size={16} />
            <span>{notice}</span>
            <button type="button" aria-label="关闭" onClick={() => showNotice('')}><X size={14} /></button>
          </div>
        )}

        {view === 'overview' && <Overview onNavigate={navigate} />}
        {view === 'speed' && <Speed />}
        {view === 'parallel' && <Parallel />}
        {view === 'capacity' && <Capacity />}
        {view === 'quality' && <Quality />}
        {view === 'contract' && <Contract />}
        {view === 'schedules' && <Schedules />}
        {view === 'history' && <History />}
        {view === 'method' && <Method />}

        <footer className="foot">relay_lab · 自有控制器 + EvalScope 压测引擎 · 真实观测，保留不确定性</footer>
      </main>
    </div>
    <AiDrawer />
    <AiConfigModal open={configModalOpen} onClose={closeAiConfig} />
    </>
  );
}

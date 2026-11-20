import { useEffect, useState } from 'react';

import { api } from './api';
import { App } from './App';
import { DotField } from './components/DotField';
import { LabProvider } from './store';
import { Landing } from './views/Landing';
import { Login } from './views/Login';

type RouteMode = 'landing' | 'login' | 'console';

function routeFromHash(loggedIn: boolean): RouteMode {
  const hash = window.location.hash;
  if (hash === '#home') return 'landing';
  if (hash === '#login') return loggedIn ? 'console' : 'login';
  return loggedIn ? 'console' : 'landing';
}

/**
 * Resolve the session cookie before mounting the live console, so WebSocket
 * traffic never starts while the JWT is missing or expired.
 */
export function AuthGate() {
  const [user, setUser] = useState<string | null | undefined>(undefined);
  const [route, setRoute] = useState<RouteMode>(() => routeFromHash(false));

  useEffect(() => {
    let cancelled = false;
    api.me()
      .then((payload) => {
        if (cancelled) return;
        setUser(payload.user);
        setRoute(routeFromHash(true));
      })
      .catch(() => {
        if (cancelled) return;
        setUser(null);
        setRoute(routeFromHash(false));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (user === undefined) return;
    const onHash = () => setRoute(routeFromHash(user !== null));
    window.addEventListener('hashchange', onHash);
    return () => window.removeEventListener('hashchange', onHash);
  }, [user]);

  useEffect(() => {
    const onUnauthorized = () => {
      setUser(null);
      setRoute('login');
      if (window.location.hash !== '#login') {
        window.history.replaceState(null, '', '#login');
      }
    };
    window.addEventListener('relay-lab:unauthorized', onUnauthorized);
    return () => window.removeEventListener('relay-lab:unauthorized', onUnauthorized);
  }, []);

  const openLogin = () => {
    setRoute('login');
    if (window.location.hash !== '#login') {
      window.history.pushState(null, '', '#login');
    }
    window.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior });
  };

  const openHome = () => {
    setRoute('landing');
    if (user !== null) {
      if (window.location.hash !== '#home') {
        window.history.pushState(null, '', '#home');
      }
    } else if (window.location.hash === '#login' || window.location.hash === '#home') {
      window.history.pushState(null, '', window.location.pathname + window.location.search);
    }
    window.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior });
  };

  const openConsole = () => {
    if (user === null) {
      openLogin();
      return;
    }
    setRoute('console');
    if (!window.location.hash || window.location.hash === '#home' || window.location.hash === '#login') {
      window.history.pushState(null, '', '#overview');
    }
    window.scrollTo({ top: 0, behavior: 'instant' as ScrollBehavior });
  };

  const handleLoginSuccess = (nextUser: string) => {
    // 登录成功后清理 #login，默认进入控制台概览页
    if (window.location.hash === '#login' || window.location.hash === '#home' || !window.location.hash) {
      window.history.replaceState(null, '', '#overview');
    }
    setUser(nextUser);
    setRoute('console');
  };

  if (user === undefined) {
    return (
      <>
        <DotField />
        <div className="login-shell">
          <div className="login-card card">
            <p className="card-desc">正在检查登录状态…</p>
          </div>
        </div>
      </>
    );
  }

  if (user === null) {
    if (route === 'login') {
      return <Login onSuccess={handleLoginSuccess} onBack={openHome} />;
    }
    return <Landing user={null} onEnterConsole={openLogin} />;
  }

  if (route === 'landing') {
    return <Landing user={user} onEnterConsole={openConsole} />;
  }

  const logout = async () => {
    try {
      await api.logout();
    } catch {
      // Cookie clear may still have happened server-side; drop local session either way.
    }
    setRoute('landing');
    window.history.replaceState(null, '', window.location.pathname + window.location.search);
    setUser(null);
  };

  return (
    <LabProvider>
      <App user={user} onHome={openHome} onLogout={() => { void logout(); }} />
    </LabProvider>
  );
}

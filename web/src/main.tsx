import '@fontsource-variable/inter';
import '@fontsource-variable/jetbrains-mono';
import '@fontsource-variable/noto-sans-sc';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { AuthGate } from './auth';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AuthGate />
  </StrictMode>,
);

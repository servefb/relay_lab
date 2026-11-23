import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

const API_TARGET = process.env.RELAY_LAB_API ?? 'http://127.0.0.1:9011';

export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: API_TARGET,
        changeOrigin: true,
        // /api/ws upgrades to a WebSocket; the dev proxy must forward the upgrade.
        ws: true,
        // The backend rejects cross-origin /api calls; the dev proxy is same-machine, so drop Origin.
        configure: (proxy) => {
          proxy.on('proxyReq', (request) => request.removeHeader('origin'));
        },
      },
    },
  },
});

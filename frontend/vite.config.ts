import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * The "@/..." alias must exist in BOTH places: TypeScript resolves it from
 * tsconfig.json's `paths` (editor + `npm run typecheck`), Vite resolves it here
 * (dev server + `npm run build`). Drop either half and one of the two breaks.
 *
 * Resolved without `node:path` / `node:url` on purpose: the dependency list is
 * fixed and does not include @types/node, so the config stays typecheckable with
 * standard-library URL only. The regex strips the leading slash that a file: URL
 * pathname carries in front of a Windows drive letter ("/C:/x" -> "C:/x").
 */
const srcPath = decodeURIComponent(
  new URL('./src', import.meta.url).pathname,
).replace(/^\/([A-Za-z]:)/, '$1');

/** The FastAPI backend in dev. Everything the SPA talks to lives under /api. */
const BACKEND_ORIGIN = 'http://localhost:8000';

export default defineConfig({
  base: '/',
  plugins: [react()],
  resolve: {
    alias: {
      '@': srcPath,
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    target: 'es2022',
    sourcemap: false,
    assetsDir: 'assets',
    chunkSizeWarningLimit: 900,
  },
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      // ws:true is what makes GET /api/stream (the WebSocket of CONTRACTS §19)
      // work through `npm run dev`; without it the upgrade request 404s and the
      // dashboard silently falls back to reconnect-forever.
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin: false, // Preserve the browser Host for backend Origin checks.
        ws: true,
      },
    },
  },
  preview: {
    port: 4173,
    strictPort: false,
    proxy: {
      '/api': {
        target: BACKEND_ORIGIN,
        changeOrigin: false, // Preserve the browser Host for backend Origin checks.
        ws: true,
      },
    },
  },
});

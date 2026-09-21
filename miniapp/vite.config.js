import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Output directory is configurable:
//   * Vercel   → default "dist" (see vercel.json outputDirectory)
//   * Backend  → VITE_OUT_DIR=../app/webapp/static  (Docker multi-stage)
// The Mini-App talks to its API on the same origin, so all calls are relative.
export default defineConfig({
  plugins: [react()],
  build: {
    outDir: process.env.VITE_OUT_DIR || 'dist',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/api': 'http://localhost:8000',
      '/ws': { target: 'ws://localhost:8000', ws: true },
      '/healthz': 'http://localhost:8000',
    },
  },
})

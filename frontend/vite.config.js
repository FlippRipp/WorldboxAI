import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/postcss'

export default defineConfig(({ mode }) => {
  // Backend origin is overridable (e.g. WB_BACKEND=http://localhost:8001 in
  // .env.<mode>) so a second dev stack can run beside the default one.
  const env = loadEnv(mode, __dirname, '')
  const backend = env.WB_BACKEND || 'http://localhost:8000'
  const backendWs = backend.replace(/^http/, 'ws')

  return {
    plugins: [react()],
    css: {
      postcss: {
        plugins: [tailwindcss()]
      }
    },
    server: {
      // Listen on all network interfaces (0.0.0.0) so other devices on the same
      // LAN can reach the dev server at http://<this-machine-ip>:5173. All API/WS
      // calls are relative, so Vite proxies them to the backend.
      host: true,
      proxy: {
        '/api': {
          target: backend,
          changeOrigin: true
        },
        '/ws': {
          target: backendWs,
          ws: true
        },
        '/widgets': {
          target: backend,
          changeOrigin: true
        },
        '/assets': {
          target: backend,
          changeOrigin: true
        }
      }
    }
  }
})

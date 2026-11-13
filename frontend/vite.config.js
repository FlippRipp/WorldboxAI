import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/postcss'

export default defineConfig(({ mode }) => {
  // Backend origin follows WB_PORT (default kept in sync with main.py).
  // WB_BACKEND overrides the whole origin (e.g. in .env.<mode>) so a second
  // dev stack can run beside the default one.
  const env = loadEnv(mode, __dirname, '')
  const backend = env.WB_BACKEND || `http://localhost:${env.WB_PORT || 8321}`
  const backendWs = backend.replace(/^http/, 'ws')

  return {
    plugins: [react()],
    css: {
      postcss: {
        plugins: [tailwindcss()]
      }
    },
    server: {
      // Bind strictly to loopback by default so Vite's /api proxy does not
      // expose unauthenticated backend routes to other devices on the Wi-Fi.
      // Set WB_LAN_UI=1 to opt into LAN UI hosting.
      host: env.WB_LAN_UI === '1' ? true : '127.0.0.1',
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

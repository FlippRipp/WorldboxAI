import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'
import { initStorageNamespace } from './lib/storage'

// Ask the browser not to evict this origin's storage under disk pressure —
// the persisted UI state (see App.jsx) is what lets an Android-killed PWA
// relaunch back into the screen it was on. Best-effort; browsers may ignore it.
navigator.storage?.persist?.().catch(() => {})

// Resolve the storage namespace (see lib/storage.js) before anything renders:
// components read drafts and UI state during their first render, so the
// profile must be known up front. If the backend can't be reached the app
// mounts with bare keys, same as the default profile.
async function boot() {
  try {
    const res = await fetch('/api/health')
    const health = await res.json()
    initStorageNamespace(health.profile_id)
  } catch {
    /* backend down or mid-restart — default namespace */
  }

  createRoot(document.getElementById('root')).render(
    <StrictMode>
      <App />
    </StrictMode>,
  )
}

boot()

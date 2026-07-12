import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.jsx'

// Ask the browser not to evict this origin's storage under disk pressure —
// the persisted UI state (see App.jsx) is what lets an Android-killed PWA
// relaunch back into the screen it was on. Best-effort; browsers may ignore it.
navigator.storage?.persist?.().catch(() => {})

createRoot(document.getElementById('root')).render(
  <StrictMode>
    <App />
  </StrictMode>,
)

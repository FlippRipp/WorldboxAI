# UI System Overhaul — Implementation Plan

This document covers the full frontend rearchitecture: component decomposition, security hardening, mobile support, performance, accessibility, and UX polish.

---

## 1. Component Decomposition (Split App.jsx)

**Current**: `App.jsx` is 444 lines with 19 state variables, 12 inline functions, WebSocket management, save CRUD, module configs, prompt pipeline, and all layout in one component.

**Target**: App.jsx becomes a thin orchestrator. Domain logic moves to hooks. Rendering moves to separate components.

### 1.1 New File Structure

```
frontend/src/
├── main.jsx
├── App.jsx                    # Thin orchestrator (<100 lines)
├── App.css                    # DELETE (dead code)
├── index.css                  # Tailwind import + custom utilities
│
├── hooks/
│   ├── useWebSocket.js        # Connection, reconnect, token streaming
│   ├── useSession.js          # Save CRUD, undo, session refresh
│   ├── useModules.js          # Module list + config fetching
│   ├── usePromptPipeline.js   # Prompt pipeline CRUD + preview
│   └── useModuleEventBus.js   # Cross-module React Context for communication
│
├── components/
│   ├── Chat/
│   │   ├── ChatFeed.jsx       # Message list + streaming message
│   │   ├── ChatMessage.jsx    # Single message (user or AI)
│   │   ├── StreamingMessage.jsx  # Token-by-token display with cursor
│   │   └── ChatInput.jsx      # Textarea + send button
│   │
│   ├── Sidebar/
│   │   ├── Sidebar.jsx        # Sidebar container + mobile drawer
│   │   ├── SaveManager.jsx    # Save dropdown, create/load/undo controls
│   │   ├── SessionInfo.jsx    # Save name + turn indicator
│   │   └── SidebarSlot.jsx    # Renders widgets for slot_sidebar
│   │
│   ├── Header/
│   │   ├── Header.jsx         # Header bar container
│   │   ├── HeaderSlot.jsx     # Renders widgets for slot_header
│   │   └── ConnectionStatus.jsx  # WebSocket connected/reconnecting indicator
│   │
│   ├── Slots/
│   │   ├── SlotRenderer.jsx   # Generic slot wrapper (error boundary + loading)
│   │   ├── ChatFeedSlot.jsx   # Renders widgets for slot_chat_feed
│   │   └── ModalSlot.jsx      # Renders widgets for slot_modal + modal management
│   │
│   ├── Modals/
│   │   ├── SettingsModal.jsx  # (existing, moved)
│   │   ├── PromptStudio.jsx   # (existing, moved)
│   │   ├── SaveBrowser.jsx    # Full save browser with metadata
│   │   └── HealthPanel.jsx    # Backend health display
│   │
│   └── shared/
│       ├── WidgetErrorBoundary.jsx  # Error boundary for DynamicWidget
│       ├── DynamicWidget.jsx        # (existing, improved)
│       ├── SkeletonLoader.jsx       # Animated skeleton placeholder
│       └── MarkdownRenderer.jsx     # Markdown-to-HTML for AI messages
│
├── lib/
│   ├── api.js                 # All API call functions (typed)
│   ├── wsClient.js            # WebSocket factory + message dispatch
│   └── constants.js           # API_BASE, WS_URL, timing constants
│
└── types/
    ├── state.d.ts             # WorldState TypeScript types
    ├── api.d.ts               # API response types
    └── widget.d.ts            # Widget contract types
```

### 1.2 useWebSocket Hook

```jsx
// hooks/useWebSocket.js
import { useState, useRef, useEffect, useCallback } from 'react';
import { WS_URL } from '../lib/constants';

export function useWebSocket() {
    const [isConnected, setIsConnected] = useState(false);
    const [isReconnecting, setIsReconnecting] = useState(false);
    const [currentStream, setCurrentStream] = useState(null);
    const [messages, setMessages] = useState([]);
    const wsRef = useRef(null);
    const reconnectTimeoutRef = useRef(null);
    const activeRef = useRef(true);

    const connect = useCallback(() => {
        if (wsRef.current?.readyState === WebSocket.OPEN) return;
        
        const ws = new WebSocket(WS_URL);
        wsRef.current = ws;
        
        ws.onopen = () => {
            if (!activeRef.current) return;
            setIsConnected(true);
            setIsReconnecting(false);
        };
        
        ws.onmessage = (event) => {
            if (!activeRef.current) return;
            const data = JSON.parse(event.data);
            
            switch (data.type) {
                case 'token':
                    setCurrentStream(prev => (prev || '') + data.content);
                    break;
                case 'done':
                    setMessages(prev => [...prev, {
                        role: 'assistant',
                        content: currentStream || '',
                        turn: data.turn
                    }]);
                    setCurrentStream(null);
                    break;
                case 'error':
                    setMessages(prev => [...prev, {
                        role: 'system',
                        content: `Error: ${data.message}`,
                        error: true
                    }]);
                    setCurrentStream(null);
                    break;
                case 'state_update':
                    // State updates handled by the orchestrator
                    break;
            }
        };
        
        ws.onclose = () => {
            if (!activeRef.current) return;
            setIsConnected(false);
            setIsReconnecting(true);
            reconnectTimeoutRef.current = setTimeout(connect, 3000);
        };
        
        ws.onerror = () => {
            ws.close();
        };
    }, [currentStream]);

    const sendMessage = useCallback((text) => {
        if (wsRef.current?.readyState === WebSocket.OPEN) {
            setMessages(prev => [...prev, { role: 'user', content: text }]);
            setCurrentStream('');
            wsRef.current.send(JSON.stringify({ action: 'turn', text }));
        }
    }, []);

    useEffect(() => {
        activeRef.current = true;
        connect();
        return () => {
            activeRef.current = false;
            clearTimeout(reconnectTimeoutRef.current);
            if (wsRef.current) {
                wsRef.current.onclose = null;
                wsRef.current.close();
            }
        };
    }, [connect]);

    return {
        isConnected, isReconnecting, messages, currentStream,
        sendMessage, setMessages
    };
}
```

### 1.3 useSession Hook

```jsx
// hooks/useSession.js
import { useState, useCallback, useRef } from 'react';

export function useSession() {
    const [sessionState, setSessionState] = useState(null);
    const [saves, setSaves] = useState([]);
    const [loading, setLoading] = useState(false);
    const abortRef = useRef(null);

    const refreshSession = useCallback(async () => {
        abortRef.current?.abort();
        abortRef.current = new AbortController();
        const { signal } = abortRef.current;

        setLoading(true);
        try {
            const [sessionRes, savesRes, configsRes] = await Promise.all([
                fetch('/api/session', { signal }),
                fetch('/api/saves', { signal }),
                fetch('/api/session/module-configs', { signal })
            ]);
            
            const session = await sessionRes.json();
            const savesData = (await savesRes.json()).saves || [];
            const configs = await configsRes.json();
            
            if (!signal.aborted) {
                setSessionState(session);
                setSaves(savesData);
                // configs propagated separately if needed
            }
        } catch (e) {
            if (e.name !== 'AbortError') {
                console.error('Session refresh failed:', e);
            }
        } finally {
            if (!signal.aborted) setLoading(false);
        }
    }, []);

    const createSave = useCallback(async (saveId) => {
        await fetch('/api/saves', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ save_id: saveId })
        });
        await refreshSession();
    }, [refreshSession]);

    const loadSave = useCallback(async (saveId) => {
        await fetch(`/api/saves/${saveId}/load`, { method: 'POST' });
        await refreshSession();
    }, [refreshSession]);

    const undoTurn = useCallback(async (targetTurn) => {
        await fetch(`/api/saves/${sessionState?.active_save_id}/undo`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ target_turn: targetTurn })
        });
        await refreshSession();
    }, [sessionState, refreshSession]);

    return {
        sessionState, saves, loading,
        refreshSession, createSave, loadSave, undoTurn
    };
}
```

### 1.4 Thin App.jsx

```jsx
// App.jsx (<100 lines)
import { useWebSocket } from './hooks/useWebSocket';
import { useSession } from './hooks/useSession';
import { useModules } from './hooks/useModules';
import { usePromptPipeline } from './hooks/usePromptPipeline';
import { ModuleEventProvider } from './hooks/useModuleEventBus';
import Header from './components/Header/Header';
import Sidebar from './components/Sidebar/Sidebar';
import ChatFeed from './components/Chat/ChatFeed';
import ChatInput from './components/Chat/ChatInput';
import ModalSlot from './components/Slots/ModalSlot';

export default function App() {
    const ws = useWebSocket();
    const session = useSession();
    const modules = useModules();
    const prompts = usePromptPipeline(session.sessionState);

    const handleSend = (text) => {
        ws.sendMessage(text);
        // Refresh session on next turn done
    };

    const handleUndo = (targetTurn) => {
        session.undoTurn(targetTurn);
    };

    return (
        <ModuleEventProvider>
            <div className="flex h-screen bg-gray-900 text-gray-100">
                <Sidebar
                    session={session}
                    modules={modules}
                    onNewSave={session.createSave}
                    onLoadSave={session.loadSave}
                    onUndo={handleUndo}
                    gameState={modules.gameState}
                />
                
                <div className="flex-1 flex flex-col min-w-0">
                    <Header
                        modules={modules}
                        session={session}
                        ws={ws}
                        prompts={prompts}
                        gameState={modules.gameState}
                    />
                    
                    <ChatFeed
                        messages={ws.messages}
                        currentStream={ws.currentStream}
                        modules={modules}
                        gameState={modules.gameState}
                    />
                    
                    <ChatInput
                        onSend={handleSend}
                        disabled={!ws.isConnected || ws.isReconnecting}
                    />
                </div>
                
                <ModalSlot
                    modules={modules}
                    gameState={modules.gameState}
                    settingsModal={settingsModal}
                    prompts={prompts}
                />
            </div>
        </ModuleEventProvider>
    );
}
```

### 1.5 ChatMessage Component

```jsx
// components/Chat/ChatMessage.jsx
import MarkdownRenderer from '../shared/MarkdownRenderer';

export default function ChatMessage({ message }) {
    const isUser = message.role === 'user';
    const isError = message.error;

    return (
        <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-4`}>
            <div className={`max-w-[80%] px-4 py-2 rounded-lg ${
                isError
                    ? 'bg-red-900/50 border border-red-700 text-red-200'
                    : isUser
                        ? 'bg-blue-600 text-white'
                        : 'bg-gray-800 text-gray-100'
            }`}>
                {isUser ? (
                    <p className="whitespace-pre-wrap">{message.content}</p>
                ) : (
                    <MarkdownRenderer content={message.content} />
                )}
                {message.turn && (
                    <div className="text-xs text-gray-500 mt-1">
                        Turn {message.turn}
                    </div>
                )}
            </div>
        </div>
    );
}
```

---

## 2. Security: `new Function()` Mitigation

### 2.1 Widget Error Boundary

```jsx
// components/shared/WidgetErrorBoundary.jsx
import React from 'react';

export default class WidgetErrorBoundary extends React.Component {
    constructor(props) {
        super(props);
        this.state = { hasError: false, error: null };
    }

    static getDerivedStateFromError(error) {
        return { hasError: true, error };
    }

    componentDidCatch(error, info) {
        console.error(`Widget "${this.props.modId}" crashed:`, error, info);
    }

    render() {
        if (this.state.hasError) {
            return (
                <div className="p-3 m-2 bg-red-900/40 border border-red-700/50 rounded-lg">
                    <div className="flex items-center gap-2 text-red-300 text-sm">
                        <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 20 20">
                            <path fillRule="evenodd" d="M18 10a8 8 0 11-16 0 8 8 0 0116 0zm-7 4a1 1 0 11-2 0 1 1 0 012 0zm-1-9a1 1 0 00-1 1v4a1 1 0 102 0V6a1 1 0 00-1-1z" />
                        </svg>
                        <span className="font-medium">Widget Error</span>
                    </div>
                    <p className="text-red-400 text-xs mt-1">
                        {this.props.modId}: {this.state.error?.message || 'Unknown error'}
                    </p>
                </div>
            );
        }
        return this.props.children;
    }
}
```

### 2.2 Improved DynamicWidget with Caching

```jsx
// components/shared/DynamicWidget.jsx
import { useState, useEffect, useRef } from 'react';
import { transform } from '@babel/standalone';
import WidgetErrorBoundary from './WidgetErrorBoundary';
import SkeletonLoader from './SkeletonLoader';

// Module-level cache: compiles each widget once per session
const widgetCache = new Map();

function requireMock(moduleName) {
    if (moduleName === 'react') return require('react');
    throw new Error(`Module "${moduleName}" not available to widgets`);
}

export default function DynamicWidget({ modId, state, config, slotName, assetsBaseUrl }) {
    const [Component, setComponent] = useState(() => widgetCache.get(modId) || null);
    const [error, setError] = useState(null);
    const [loading, setLoading] = useState(!widgetCache.has(modId));
    const mountedRef = useRef(true);

    useEffect(() => {
        mountedRef.current = true;

        if (widgetCache.has(modId)) {
            setComponent(widgetCache.get(modId));
            setLoading(false);
            return;
        }

        let cancelled = false;

        fetch(`/widgets/${modId}/widget.jsx`)
            .then(res => {
                if (!res.ok) throw new Error(`HTTP ${res.status}`);
                return res.text();
            })
            .then(source => {
                if (cancelled || !mountedRef.current) return;

                const result = transform(source, {
                    presets: ['env', 'react']
                });

                const factory = new Function(
                    'require', 'module', 'exports', 'React',
                    result.code
                );
                const mod = { exports: {} };
                factory(requireMock, mod, mod.exports, require('react'));

                const Comp = mod.exports?.default || mod.exports;
                if (typeof Comp !== 'function') {
                    throw new Error('Widget must export a React component as default');
                }

                widgetCache.set(modId, Comp);
                if (mountedRef.current) {
                    setComponent(() => Comp);
                    setLoading(false);
                }
            })
            .catch(err => {
                if (!cancelled && mountedRef.current) {
                    setError(err);
                    setLoading(false);
                }
            });

        return () => { cancelled = true; mountedRef.current = false; };
    }, [modId]);

    if (loading) return <SkeletonLoader />;
    if (error) throw error;  // Caught by WidgetErrorBoundary
    if (!Component) return null;

    const Comp = Component;
    return <Comp state={state} config={config} slotName={slotName} assetsBaseUrl={assetsBaseUrl} />;
}
```

### 2.3 SlotRenderer (generic wrapper)

```jsx
// components/Slots/SlotRenderer.jsx
import WidgetErrorBoundary from '../shared/WidgetErrorBoundary';
import DynamicWidget from '../shared/DynamicWidget';

export default function SlotRenderer({ slotName, modules, state, config, className }) {
    const slotModules = modules.filter(mod =>
        mod.ui_slots?.includes(slotName)
    );

    if (slotModules.length === 0) return null;

    return (
        <div className={className}>
            {slotModules.map(mod => (
                <WidgetErrorBoundary key={mod.id} modId={mod.id}>
                    <DynamicWidget
                        modId={mod.id}
                        state={state}
                        config={config?.[mod.id] || {}}
                        slotName={slotName}
                    />
                </WidgetErrorBoundary>
            ))}
        </div>
    );
}
```

---

## 3. Vite Proxy + API Abstraction

### 3.1 Vite Config

```js
// vite.config.js
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/postcss';

export default defineConfig({
    plugins: [react()],
    css: {
        postcss: {
            plugins: [tailwindcss()]
        }
    },
    server: {
        proxy: {
            '/api': {
                target: 'http://localhost:8000',
                changeOrigin: true
            },
            '/ws': {
                target: 'ws://localhost:8000',
                ws: true
            },
            '/widgets': {
                target: 'http://localhost:8000',
                changeOrigin: true
            },
            '/assets': {
                target: 'http://localhost:8000',
                changeOrigin: true
            }
        }
    }
});
```

### 3.2 Constants + API Module

```js
// lib/constants.js
export const API_BASE = '';
export const WS_URL = `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/chat`;
```

```js
// lib/api.js
const API = '/api';

async function request(path, options = {}) {
    const res = await fetch(`${API}${path}`, {
        headers: { 'Content-Type': 'application/json', ...options.headers },
        ...options
    });
    if (!res.ok) {
        const error = await res.json().catch(() => ({}));
        throw new ApiError(res.status, error.detail || res.statusText);
    }
    return res.json();
}

export class ApiError extends Error {
    constructor(status, detail) {
        super(detail);
        this.status = status;
    }
}

export const api = {
    getSession:                () => request('/session'),
    getSaves:                  () => request('/saves'),
    createSave:                (saveId) => request('/saves', { method: 'POST', body: JSON.stringify({ save_id: saveId }) }),
    loadSave:                  (saveId) => request(`/saves/${saveId}/load`, { method: 'POST' }),
    undoSave:                  (saveId, targetTurn) => request(`/saves/${saveId}/undo`, { method: 'POST', body: JSON.stringify({ target_turn: targetTurn }) }),
    deleteSave:                (saveId) => request(`/saves/${saveId}`, { method: 'DELETE' }),
    getModules:                () => request('/modules'),
    getModuleConfigs:          () => request('/session/module-configs'),
    updateModuleConfigs:       (configs) => request('/session/module-configs', { method: 'PUT', body: JSON.stringify({ module_configs: configs }) }),
    getPromptPipeline:         () => request('/session/prompt-pipeline'),
    updatePromptPipeline:      (pipeline) => request('/session/prompt-pipeline', { method: 'PUT', body: JSON.stringify({ prompt_pipeline: pipeline }) }),
    previewPromptPipeline:     (pipeline) => request('/session/prompt-pipeline/preview', { method: 'POST', body: JSON.stringify({ prompt_pipeline: pipeline }) }),
    getHealth:                 () => request('/health'),
};
```

---

## 4. Mobile Support

### 4.1 Sidebar Drawer

```jsx
// components/Sidebar/Sidebar.jsx
import { useState } from 'react';
import SaveManager from './SaveManager';
import SessionInfo from './SessionInfo';
import SidebarSlot from './SidebarSlot';

export default function Sidebar({ session, modules, gameState, onNewSave, onLoadSave, onUndo }) {
    const [drawerOpen, setDrawerOpen] = useState(false);

    // Reusable sidebar content
    const sidebarContent = (
        <div className="flex flex-col h-full p-4 gap-4 overflow-y-auto">
            <SessionInfo
                saveId={session.sessionState?.active_save_id}
                turn={session.sessionState?.turn}
            />
            <SaveManager
                saves={session.saves}
                activeSaveId={session.sessionState?.active_save_id}
                onNewSave={onNewSave}
                onLoadSave={onLoadSave}
                onUndo={onUndo}
                loading={session.loading}
            />
            <SidebarSlot
                modules={modules}
                state={gameState}
                config={gameState?.module_configs}
            />
        </div>
    );

    return (
        <>
            {/* Desktop sidebar: always visible */}
            <aside className="hidden lg:flex lg:flex-col lg:w-72 bg-gray-850 border-r border-gray-700">
                {sidebarContent}
            </aside>

            {/* Mobile hamburger button */}
            <button
                className="lg:hidden fixed top-3 left-3 z-50 p-2 bg-gray-800 rounded-lg border border-gray-700 shadow-lg"
                onClick={() => setDrawerOpen(true)}
                aria-label="Open sidebar"
            >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
                </svg>
            </button>

            {/* Mobile drawer overlay */}
            {drawerOpen && (
                <div className="lg:hidden fixed inset-0 z-40">
                    {/* Backdrop */}
                    <div
                        className="absolute inset-0 bg-black/60 backdrop-blur-sm"
                        onClick={() => setDrawerOpen(false)}
                    />
                    {/* Drawer */}
                    <div className="absolute left-0 top-0 bottom-0 w-72 bg-gray-850 border-r border-gray-700 shadow-2xl animate-slide-in">
                        <div className="flex justify-end p-2">
                            <button
                                onClick={() => setDrawerOpen(false)}
                                className="p-2 hover:bg-gray-700 rounded-lg"
                                aria-label="Close sidebar"
                            >
                                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                                </svg>
                            </button>
                        </div>
                        {sidebarContent}
                    </div>
                </div>
            )}
        </>
    );
}
```

### 4.2 Mobile Animation CSS

```css
/* index.css — add */
@keyframes slide-in {
    from { transform: translateX(-100%); }
    to   { transform: translateX(0); }
}
.animate-slide-in {
    animation: slide-in 0.2s ease-out;
}
```

---

## 5. Cross-Module Communication

Replace `window.dispatchEvent` / `window.addEventListener` with a React Context.

### 5.1 Module Event Bus Context

```jsx
// hooks/useModuleEventBus.jsx
import { createContext, useContext, useState, useCallback } from 'react';

const ModuleEventContext = createContext(null);

export function ModuleEventProvider({ children }) {
    const [modalStates, setModalStates] = useState({});
    const [events, setEvents] = useState({});

    const openModal = useCallback((modId) => {
        setModalStates(prev => ({ ...prev, [modId]: true }));
    }, []);

    const closeModal = useCallback((modId) => {
        setModalStates(prev => ({ ...prev, [modId]: false }));
    }, []);

    const isModalOpen = useCallback((modId) => {
        return modalStates[modId] || false;
    }, [modalStates]);

    const emitEvent = useCallback((eventName, payload) => {
        setEvents(prev => ({ ...prev, [eventName]: { payload, ts: Date.now() } }));
    }, []);

    const value = {
        modalStates, openModal, closeModal, isModalOpen,
        events, emitEvent
    };

    return (
        <ModuleEventContext.Provider value={value}>
            {children}
        </ModuleEventContext.Provider>
    );
}

export function useModuleEvents() {
    const ctx = useContext(ModuleEventContext);
    if (!ctx) throw new Error('useModuleEvents must be used within ModuleEventProvider');
    return ctx;
}
```

### 5.2 Widget Usage

```jsx
// modules/core_inventory/widget.jsx (refactored)
import React, { useState, useContext } from 'react';

const ModuleEventContext = React.createContext(null); 
// Widgets import this from a shared SDK or get it via props

export default function InventoryWidget({ state, config, slotName, eventBus }) {
    const { openModal, isModalOpen } = eventBus;
    const items = state?.module_data?.wb_core_inventory?.items || [];

    if (slotName === 'slot_header') {
        return (
            <button
                onClick={() => openModal('wb_core_inventory')}
                className="flex items-center gap-1 px-2 py-1 rounded hover:bg-gray-700 text-sm"
                title={`Inventory (${items.length} items)`}
            >
                <span>🎒</span>
                {items.length > 0 && (
                    <span className="bg-blue-600 text-xs px-1.5 py-0.5 rounded-full">
                        {items.length}
                    </span>
                )}
            </button>
        );
    }

    if (slotName === 'slot_modal' && isModalOpen('wb_core_inventory')) {
        return (
            <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
                <div className="bg-gray-800 rounded-xl p-6 max-w-md w-full mx-4 max-h-[80vh] overflow-y-auto">
                    <div className="flex justify-between items-center mb-4">
                        <h2 className="text-lg font-semibold">Inventory</h2>
                        <button onClick={() => openModal('wb_core_inventory', false)} className="p-1 hover:bg-gray-700 rounded">
                            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                            </svg>
                        </button>
                    </div>
                    {items.length === 0 ? (
                        <p className="text-gray-400 text-center py-4">Empty</p>
                    ) : (
                        <ul className="space-y-2">
                            {items.map((item, i) => (
                                <li key={i} className="flex justify-between items-center p-2 bg-gray-750 rounded">
                                    <span>{item.name}</span>
                                    <span className="text-sm text-gray-400">x{item.quantity}</span>
                                </li>
                            ))}
                        </ul>
                    )}
                </div>
            </div>
        );
    }

    return null;
}
```

### 5.3 Pass eventBus to widgets

Update `SlotRenderer` to pass `eventBus` as a prop:

```jsx
<DynamicWidget
    modId={mod.id}
    state={state}
    config={config?.[mod.id] || {}}
    slotName={slotName}
    eventBus={eventBus}  // NEW
/>
```

---

## 6. Loading, Error, and Empty States

### 6.1 Skeleton Loader

```jsx
// components/shared/SkeletonLoader.jsx
export default function SkeletonLoader({ height = 'h-16', width = 'w-full', className = '' }) {
    return (
        <div className={`${height} ${width} ${className} animate-pulse rounded-lg bg-gray-800`}>
            <div className="flex items-center gap-3 p-3">
                <div className="w-8 h-8 rounded-full bg-gray-700" />
                <div className="flex-1 space-y-2">
                    <div className="h-3 bg-gray-700 rounded w-3/4" />
                    <div className="h-3 bg-gray-700 rounded w-1/2" />
                </div>
            </div>
        </div>
    );
}
```

### 6.2 Connection Status Banner

```jsx
// components/Header/ConnectionStatus.jsx
export default function ConnectionStatus({ isConnected, isReconnecting }) {
    if (isConnected) return null;

    return (
        <div className={`px-4 py-1.5 text-center text-sm ${
            isReconnecting
                ? 'bg-yellow-900/60 text-yellow-200 border-b border-yellow-700/50'
                : 'bg-red-900/60 text-red-200 border-b border-red-700/50'
        }`}>
            {isReconnecting
                ? 'Reconnecting to server...'
                : 'Disconnected from server'}
            {isReconnecting && (
                <span className="inline-block ml-2 animate-spin">⟳</span>
            )}
        </div>
    );
}
```

### 6.3 API Loading HOC

```jsx
// hooks/useApiCall.js
import { useState, useCallback } from 'react';

export function useApiCall(fn) {
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState(null);

    const execute = useCallback(async (...args) => {
        setLoading(true);
        setError(null);
        try {
            const result = await fn(...args);
            return result;
        } catch (e) {
            setError(e);
            throw e;
        } finally {
            setLoading(false);
        }
    }, [fn]);

    return { execute, loading, error, reset: () => setError(null) };
}
```

---

## 7. Markdown Rendering

### 7.1 MarkdownRenderer

```jsx
// components/shared/MarkdownRenderer.jsx
import { useMemo } from 'react';

function simpleMarkdownToHtml(text) {
    if (!text) return '';
    let html = text
        // Bold
        .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
        // Italic
        .replace(/\*(.+?)\*/g, '<em>$1</em>')
        // Inline code
        .replace(/`([^`]+)`/g, '<code class="bg-gray-700 px-1 py-0.5 rounded text-sm">$1</code>')
        // Headers
        .replace(/^### (.+)$/gm, '<h4 class="text-base font-semibold mt-3 mb-1">$1</h4>')
        .replace(/^## (.+)$/gm, '<h3 class="text-lg font-semibold mt-4 mb-2">$1</h3>')
        .replace(/^# (.+)$/gm, '<h2 class="text-xl font-semibold mt-4 mb-2">$1</h2>')
        // Unordered lists
        .replace(/^- (.+)$/gm, '<li class="ml-4 list-disc">$1</li>')
        // Ordered lists
        .replace(/^\d+\. (.+)$/gm, '<li class="ml-4 list-decimal">$1</li>')
        // Paragraphs (double newline)
        .replace(/\n\n/g, '</p><p class="mb-2">')
        // Single newline to <br>
        .replace(/\n/g, '<br/>');

    html = html.replace(/<li class="ml-4 list-disc">.*?<\/li>(\s*<li class="ml-4 list-disc">.*?<\/li>)*/gs,
        match => `<ul class="mb-2">${match}</ul>`);

    html = html.replace(/<li class="ml-4 list-decimal">.*?<\/li>(\s*<li class="ml-4 list-decimal">.*?<\/li>)*/gs,
        match => `<ol class="mb-2">${match}</ol>`);

    return `<p class="mb-2">${html}</p>`;
}

export default function MarkdownRenderer({ content }) {
    const html = useMemo(() => simpleMarkdownToHtml(content), [content]);

    return (
        <div
            className="prose prose-invert prose-sm max-w-none"
            dangerouslySetInnerHTML={{ __html: html }}
        />
    );
}
```

**Note**: For production, replace the simple regex with `marked` + `DOMPurify`:

```bash
npm install marked dompurify
```

```jsx
import { marked } from 'marked';
import DOMPurify from 'dompurify';

export default function MarkdownRenderer({ content }) {
    const html = useMemo(() => {
        const raw = marked.parse(content || '', { breaks: true });
        return DOMPurify.sanitize(raw);
    }, [content]);

    return <div className="prose prose-invert prose-sm max-w-none" dangerouslySetInnerHTML={{ __html: html }} />;
}
```

---

## 8. Accessibility

### 8.1 Required ARIA Additions

| Component | Attribute | Value |
|---|---|---|
| Chat message container | `role="log"` | — |
| Chat message container | `aria-live="polite"` | — |
| Streaming message | `aria-live="assertive"` | — |
| Settings modal | `aria-modal="true"` | — |
| Settings modal | `role="dialog"` | — |
| PromptStudio modal | `aria-modal="true"` | — |
| PromptStudio modal | `role="dialog"` | — |
| Gear button | `aria-label="Open settings"` | — |
| Prompts button | `aria-label="Open prompt editor"` | — |
| Send button | `aria-label="Send message"` | — |
| Chat textarea | `aria-label="Type your action"` | — |
| Sidebar hamburger | `aria-label="Open sidebar"` | — |
| Sidebar close button | `aria-label="Close sidebar"` | — |
| Save dropdown | `aria-label="Select save"` | — |

### 8.2 Focus Trap for Modals

```jsx
// hooks/useFocusTrap.js
import { useEffect, useRef } from 'react';

export function useFocusTrap(isActive) {
    const ref = useRef(null);

    useEffect(() => {
        if (!isActive || !ref.current) return;

        const element = ref.current;
        const focusable = element.querySelectorAll(
            'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        const first = focusable[0];
        const last = focusable[focusable.length - 1];

        function handleKeyDown(e) {
            if (e.key !== 'Tab') return;
            if (e.shiftKey) {
                if (document.activeElement === first) {
                    e.preventDefault();
                    last?.focus();
                }
            } else {
                if (document.activeElement === last) {
                    e.preventDefault();
                    first?.focus();
                }
            }
        }

        element.addEventListener('keydown', handleKeyDown);
        first?.focus();

        return () => element.removeEventListener('keydown', handleKeyDown);
    }, [isActive]);

    return ref;
}
```

---

## 9. Performance Optimizations

### 9.1 Memoize Slot Rendering

```jsx
// In App.jsx or SlotRenderer — wrap with React.memo
const MemoizedSlotRenderer = React.memo(SlotRenderer, (prev, next) => {
    // Only re-render if the modules list or relevant state changes
    const prevModuleIds = prev.modules.map(m => m.id).sort().join(',');
    const nextModuleIds = next.modules.map(m => m.id).sort().join(',');
    if (prevModuleIds !== nextModuleIds) return false;

    // For state changes, only re-render if data changed for modules in this slot
    const slotModules = prev.modules.filter(m => m.ui_slots?.includes(prev.slotName));
    for (const mod of slotModules) {
        const prevData = prev.state?.module_data?.[mod.id];
        const nextData = next.state?.module_data?.[mod.id];
        if (JSON.stringify(prevData) !== JSON.stringify(nextData)) return false;

        const prevCfg = prev.config?.[mod.id];
        const nextCfg = next.config?.[mod.id];
        if (JSON.stringify(prevCfg) !== JSON.stringify(nextCfg)) return false;
    }
    return true;
});
```

### 9.2 Scroll Optimization

```jsx
// In ChatFeed.jsx
const scrollBehavior = isStreaming ? 'auto' : 'smooth';

function scrollToBottom() {
    if (messagesEndRef.current) {
        messagesEndRef.current.scrollIntoView({ behavior: scrollBehavior });
    }
}
```

### 9.3 Virtual List (Future)

For sessions exceeding 200+ messages, integrate `@tanstack/react-virtual`:

```bash
npm install @tanstack/react-virtual
```

```jsx
import { useVirtualizer } from '@tanstack/react-virtual';

// In ChatFeed.jsx
const parentRef = useRef(null);
const virtualizer = useVirtualizer({
    count: messages.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => 80,
});

return (
    <div ref={parentRef} className="flex-1 overflow-y-auto">
        <div style={{ height: `${virtualizer.getTotalSize()}px`, position: 'relative' }}>
            {virtualizer.getVirtualItems().map(virtualRow => (
                <div
                    key={virtualRow.key}
                    style={{
                        position: 'absolute',
                        top: 0,
                        left: 0,
                        width: '100%',
                        transform: `translateY(${virtualRow.start}px)`
                    }}
                >
                    <ChatMessage message={messages[virtualRow.index]} />
                </div>
            ))}
        </div>
    </div>
);
```

---

## 10. Confirmation Dialogs

### 10.1 ConfirmDialog Component

```jsx
// components/shared/ConfirmDialog.jsx
import { useFocusTrap } from '../../hooks/useFocusTrap';

export default function ConfirmDialog({ title, message, confirmLabel, onConfirm, onCancel, destructive }) {
    const focusRef = useFocusTrap(true);

    return (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" role="dialog" aria-modal="true">
            <div ref={focusRef} className="bg-gray-800 rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl">
                <h3 className="text-lg font-semibold mb-2">{title}</h3>
                <p className="text-gray-300 text-sm mb-6">{message}</p>
                <div className="flex justify-end gap-3">
                    <button
                        onClick={onCancel}
                        className="px-4 py-2 text-sm rounded-lg bg-gray-700 hover:bg-gray-600 transition"
                    >
                        Cancel
                    </button>
                    <button
                        onClick={onConfirm}
                        className={`px-4 py-2 text-sm rounded-lg transition ${
                            destructive
                                ? 'bg-red-600 hover:bg-red-500 text-white'
                                : 'bg-blue-600 hover:bg-blue-500 text-white'
                        }`}
                    >
                        {confirmLabel || 'Confirm'}
                    </button>
                </div>
            </div>
        </div>
    );
}
```

### 10.2 Usage in SaveManager

```jsx
// In SaveManager.jsx
const [confirmUndo, setConfirmUndo] = useState(null);

const handleUndoClick = (targetTurn) => {
    setConfirmUndo(targetTurn);
};

return (
    <>
        <button onClick={() => handleUndoClick(currentTurn - 1)}>
            Undo Last Turn
        </button>
        
        {confirmUndo !== null && (
            <ConfirmDialog
                title="Undo Turn"
                message={`Revert to turn ${confirmUndo}? This will discard all turns after this point. This cannot be undone.`}
                confirmLabel="Undo"
                destructive
                onConfirm={() => {
                    onUndo(confirmUndo);
                    setConfirmUndo(null);
                }}
                onCancel={() => setConfirmUndo(null)}
            />
        )}
    </>
);
```

---

## 11. PromptStudio Improvements

### 11.1 Unsaved Changes Warning

```jsx
// In PromptStudio.jsx
const [hasUnsavedChanges, setHasUnsavedChanges] = useState(false);

const handleClose = useCallback(() => {
    if (hasUnsavedChanges) {
        setShowCloseConfirm(true);
    } else {
        onClose();
    }
}, [hasUnsavedChanges, onClose]);

// Reset draft warning on save
const handleSave = async () => {
    await onSave(draftBlocks);
    setHasUnsavedChanges(false);
};
```

### 11.2 Drag-and-Drop Reordering

```jsx
// In PromptStudio.jsx — add drag/drop
const [dragIndex, setDragIndex] = useState(null);

const handleDragStart = (e, index) => {
    setDragIndex(index);
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', index);
};

const handleDragOver = (e, index) => {
    e.preventDefault();
    if (dragIndex === index) return;
    e.dataTransfer.dropEffect = 'move';
};

const handleDrop = (e, index) => {
    e.preventDefault();
    if (dragIndex === null || dragIndex === index) return;

    const newBlocks = [...draftBlocks];
    const [moved] = newBlocks.splice(dragIndex, 1);
    newBlocks.splice(index, 0, moved);

    setDraftBlocks(newBlocks);
    setHasUnsavedChanges(true);
    setDragIndex(null);
};
```

---

## 12. HTML Title + Dead Code Removal

### 12.1 index.html

```html
<title>WorldBox — AI RPG Engine</title>
```

### 12.2 Files to Delete

- `frontend/src/App.css` (184 lines, never imported)
- `frontend/public/vite.svg` (if unused)
- `frontend/src/assets/react.svg` (if unused)

### 12.3 Tailwind v4 Alignment

Remove `tailwind.config.js` if using Tailwind v4 with `@tailwindcss/postcss`. Migrate any custom config to CSS:

```css
/* index.css */
@import "tailwindcss";

@theme {
    --color-gray-750: #2d3748;
    --color-gray-850: #1a202c;
    --color-gray-950: #0d1117;
}
```

---

## 13. Widget Contract Documentation

### 13.1 Widget Contract (new doc)

Create `docs/WIDGET_CONTRACT.md`:

```markdown
# WorldBox Widget Contract

## Props

| Prop | Type | Description |
|---|---|---|
| `state` | `WorldState` | Full game state (module_data, module_configs, turn, chat_messages, etc.) |
| `config` | `object` | This module's settings from module_configs[moduleId] |
| `slotName` | `'slot_sidebar' \| 'slot_header' \| 'slot_chat_feed' \| 'slot_modal'` | Which slot this widget is rendering in |
| `assetsBaseUrl` | `string` | Base URL for module assets (`/assets/{moduleId}`) |
| `eventBus` | `ModuleEventBus` | Cross-module communication (openModal, emitEvent) |

## ModuleEventBus

| Method | Signature | Description |
|---|---|---|
| `openModal(modId)` | `(string) => void` | Open a module's modal view |
| `closeModal(modId)` | `(string) => void` | Close a module's modal view |
| `isModalOpen(modId)` | `(string) => boolean` | Check if a module's modal is open |
| `emitEvent(name, payload)` | `(string, any) => void` | Emit a named event |

## Available Globals

- `React` — React object (useState, useEffect, etc.)
- No other imports are available

## File Structure

```jsx
// modules/my_module/widget.jsx
export default function MyWidget({ state, config, slotName, assetsBaseUrl, eventBus }) {
    // Branch on slotName to render in the right slot
    if (slotName === 'slot_header') return <HeaderView ... />;
    if (slotName === 'slot_sidebar') return <SidebarView ... />;
    if (slotName === 'slot_chat_feed') return <ChatFeedView ... />;
    if (slotName === 'slot_modal') return <ModalView ... />;
    return null;
}
```

## Constraints

- No CSS imports — use Tailwind classes inline
- No external npm imports — only React is available
- No `window` events — use `eventBus` for cross-module communication
- Widget errors are caught by the WidgetErrorBoundary and displayed as error cards
- Return `null` for slots you don't handle
```

---

## 14. Implementation Order

### Sprint 1: Foundation (Enables Everything)

1. **Delete dead code**: `App.css`, unused assets, fix `<title>`, align Tailwind config
2. **Add Vite proxy**: Replace all hardcoded URLs with relative paths + create `constants.js`
3. **Create `lib/api.js`**: Centralized API layer
4. **Create `WidgetErrorBoundary`**: Wrap all widget rendering
5. **Create `SkeletonLoader`**: Replace `return null` in DynamicWidget
6. **Cache compiled widgets**: Add module-level `Map` in DynamicWidget

### Sprint 2: Component Decomposition

7. **Extract `useWebSocket` hook** — tests independently
8. **Extract `useSession` hook** — with AbortController
9. **Extract `useModules` hook**
10. **Extract `usePromptPipeline` hook**
11. **Create `ChatMessage`, `ChatFeed`, `ChatInput`** components
12. **Create `Sidebar`, `SaveManager`, `Header`** components
13. **Create `SlotRenderer`** generic wrapper
14. **Thin App.jsx** to orchestrator

### Sprint 3: UX Polish

15. **Mobile sidebar** (hamburger + drawer)
16. **Connection status banner**
17. **Markdown rendering** (`marked` + `DOMPurify`)
18. **Confirmation dialogs** (undo, unsaved prompts)
19. **Accessibility** (ARIA labels, focus traps, roles)

### Sprint 4: Module Integration

20. **Module Event Bus** (replace `window` events)
21. **Fix widget state consumption** (weather, dice, inventory read real state)
22. **Virtual list** for long chat sessions
23. **Drag/drop** in Prompt Studio

### Sprint 5: Quality

24. **TypeScript types** for state, API, widget contract
25. **Widget contract documentation** (WIDGET_CONTRACT.md)
26. **Dark/light theme toggle**
27. **Message retry/edit**

---

## 15. Testing Strategy

### Unit Tests (Vitest)

```bash
npm install -D vitest @testing-library/react @testing-library/jest-dom jsdom
```

```js
// hooks/useWebSocket.test.js
describe('useWebSocket', () => {
    it('connects and tracks connection state');
    it('handles incoming token messages');
    it('handles done messages and transitions state');
    it('handles error messages');
    it('reconnects on close with delay');
    it('cleans up on unmount');
});

// hooks/useSession.test.js
describe('useSession', () => {
    it('fetches session, saves, and configs on refresh');
    it('creates save and refreshes');
    it('loads save and refreshes');
    it('aborts previous requests on re-refresh');
    it('handles API errors gracefully');
});

// components/WidgetErrorBoundary.test.jsx
describe('WidgetErrorBoundary', () => {
    it('renders children normally');
    it('renders error card on child error');
    it('logs error details');
});

// components/ChatMessage.test.jsx
describe('ChatMessage', () => {
    it('renders user messages right-aligned');
    it('renders AI messages left-aligned with markdown');
    it('renders error messages with red styling');
    it('displays turn number');
});
```

### Integration Tests

```js
describe('App integration', () => {
    it('renders sidebar with save controls');
    it('renders header with module widgets');
    it('sends message via WebSocket and displays streaming response');
    it('shows reconnecting banner on disconnect');
});
```

---

## 16. Migration Steps (Safe Rollback)

1. Create all new files (hooks, components, lib) — existing code untouched
2. Import and use new hooks/components in a **copy** of App.jsx
3. Run `npm run build` to verify no errors
4. Delete old App.jsx, rename new one
5. Delete dead files (`App.css`, etc.)
6. Test full flow: connect → send → stream → undo → reload

import { useState, useEffect, useCallback } from 'react';
import { api } from '../../lib/api';
import { storage } from '../../lib/storage';

export default function DebugBridgeOverlay() {
  const [bridge, setBridge] = useState(null);
  // Keyed by pending request_id so each new connection request automatically
  // starts at safe Read-Only (`false`) without calling setState in an effect.
  const [writeGrant, setWriteGrant] = useState({ requestId: null, allow: false });
  const [busy, setBusy] = useState(false);
  const [pillOpenSessionId, setPillOpenSessionId] = useState(null);
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));

  const refreshStatus = useCallback(async () => {
    try {
      const data = await api.getBridgeStatus();
      setNowSec(Math.floor(Date.now() / 1000));
      setBridge(data);
    } catch {
      // Server may still be starting up
    }
  }, []);

  useEffect(() => {
    let mounted = true;
    api.getBridgeStatus()
      .then((data) => {
        if (!mounted) return;
        setNowSec(Math.floor(Date.now() / 1000));
        setBridge(data);
      })
      .catch(() => {});

    const onWsState = (ev) => {
      if (ev.detail) {
        setNowSec(Math.floor(Date.now() / 1000));
        setBridge(ev.detail);
      }
    };
    const onReconnect = () => refreshStatus();
    window.addEventListener('wb:debug-bridge-state', onWsState);
    window.addEventListener('wb:ws-reconnected', onReconnect);
    return () => {
      mounted = false;
      window.removeEventListener('wb:debug-bridge-state', onWsState);
      window.removeEventListener('wb:ws-reconnected', onReconnect);
    };
  }, [refreshStatus]);

  const pending = bridge?.pending_session;
  const active = bridge?.active_session;
  const allowWriteChoice = pending && writeGrant.requestId === pending.request_id ? writeGrant.allow : false;
  const pillOpen = active && pillOpenSessionId === active.session_id;

  // Tick countdown while a pending prompt or active session exists
  useEffect(() => {
    if (!pending && !active) return undefined;
    const id = setInterval(() => {
      setNowSec(Math.floor(Date.now() / 1000));
    }, 1000);
    return () => clearInterval(id);
  }, [pending, active]);

  // Capture mobile browser JS errors + current screen for remote diagnostics
  useEffect(() => {
    const errorQueue = [];
    let flushTimer = null;

    const readUiSnapshot = () => {
      let saved = null;
      try {
        saved = JSON.parse(storage.getItem('wb_ui_state') || 'null');
      } catch {
        // Ignore malformed localStorage
      }
      return {
        mode: saved?.mode || 'menu',
        active_save_id: saved?.saveId || null,
        viewport: { width: window.innerWidth, height: window.innerHeight },
        user_agent: navigator.userAgent,
        online: navigator.onLine,
      };
    };

    const scheduleFlush = (delay = 600) => {
      if (flushTimer) return;
      flushTimer = setTimeout(() => {
        flushTimer = null;
        const batch = errorQueue.splice(0, 30);
        api.sendBridgeTelemetry({
          errors: batch.length ? batch : undefined,
          ui_state: readUiSnapshot(),
        }).catch(() => {});
      }, delay);
    };

    const onWindowError = (ev) => {
      errorQueue.push({
        kind: 'uncaught_error',
        timestamp: Date.now() / 1000,
        message: ev.message || String(ev.error || 'Window error'),
        stack: ev.error?.stack || null,
        source: ev.filename ? `${ev.filename}:${ev.lineno || 0}:${ev.colno || 0}` : null,
      });
      scheduleFlush(300);
    };

    const onRejection = (ev) => {
      const reason = ev.reason;
      errorQueue.push({
        kind: 'unhandled_rejection',
        timestamp: Date.now() / 1000,
        message: reason?.message || String(reason || 'Unhandled promise rejection'),
        stack: reason?.stack || null,
      });
      scheduleFlush(300);
    };

    const origConsoleError = console.error;
    console.error = (...args) => {
      origConsoleError.apply(console, args);
      try {
        const msg = args
          .map((a) => (typeof a === 'string' ? a : a instanceof Error ? `${a.name}: ${a.message}` : JSON.stringify(a)))
          .join(' ');
        const firstErr = args.find((a) => a instanceof Error);
        errorQueue.push({
          kind: 'console_error',
          timestamp: Date.now() / 1000,
          message: msg.slice(0, 2000),
          stack: firstErr?.stack || null,
        });
        scheduleFlush(800);
      } catch {
        // Ignore serialization issues
      }
    };

    const onResizeOrPop = () => scheduleFlush(1200);
    window.addEventListener('error', onWindowError);
    window.addEventListener('unhandledrejection', onRejection);
    window.addEventListener('resize', onResizeOrPop);
    window.addEventListener('popstate', onResizeOrPop);
    scheduleFlush(500);
    const uiHeartbeat = setInterval(() => scheduleFlush(100), 15000);

    return () => {
      console.error = origConsoleError;
      window.removeEventListener('error', onWindowError);
      window.removeEventListener('unhandledrejection', onRejection);
      window.removeEventListener('resize', onResizeOrPop);
      window.removeEventListener('popstate', onResizeOrPop);
      clearInterval(uiHeartbeat);
      if (flushTimer) clearTimeout(flushTimer);
    };
  }, []);

  const handleDecide = async (approve) => {
    if (!pending?.request_id) return;
    setBusy(true);
    try {
      const next = await api.decideBridgeSession(pending.request_id, approve, approve ? allowWriteChoice : false);
      setNowSec(Math.floor(Date.now() / 1000));
      setBridge(next);
    } catch {
      refreshStatus();
    } finally {
      setBusy(false);
    }
  };

  const handleToggleWrite = async () => {
    if (!active) return;
    setBusy(true);
    try {
      const next = await api.setBridgeWriteMode(!active.allow_write);
      setBridge(next);
    } catch {
      refreshStatus();
    } finally {
      setBusy(false);
    }
  };

  const handleDisconnect = async () => {
    setBusy(true);
    try {
      const next = await api.disconnectBridgeSession();
      setBridge(next);
      setPillOpenSessionId(null);
    } catch {
      refreshStatus();
    } finally {
      setBusy(false);
    }
  };

  const pendingSecLeft = pending?.expires_at ? Math.max(0, Math.ceil(pending.expires_at - nowSec)) : 0;
  const showPending = pending && pendingSecLeft > 0;
  const activeMinLeft = active?.expires_at ? Math.max(0, Math.ceil((active.expires_at - nowSec) / 60)) : 0;
  const showActive = active && activeMinLeft > 0;

  if (!showPending && !showActive) return null;

  return (
    <>
      {/* Per-session approval modal */}
      {showPending && (
        <div
          className="fixed inset-0 z-[70] bg-black/75 backdrop-blur-xs flex items-center justify-center p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="wb-bridge-prompt-title"
        >
          <div className="w-full max-w-md bg-gray-900 border border-purple-500/50 rounded-2xl shadow-2xl p-5 space-y-4">
            <div className="flex items-start justify-between gap-2">
              <div>
                <h3 id="wb-bridge-prompt-title" className="text-lg font-bold text-gray-100">
                  Allow Diagnostic Connection?
                </h3>
                <p className="text-xs text-emerald-400 mt-0.5">
                  ✓ Cryptographically verified paired device on your local subnet
                </p>
              </div>
              <span className="font-mono text-xs px-2 py-1 rounded bg-gray-800 border border-gray-700 text-gray-300 shrink-0">
                {pendingSecLeft}s
              </span>
            </div>

            <div className="bg-gray-950/80 border border-gray-800 rounded-xl p-3.5 text-sm space-y-1">
              <div className="flex justify-between gap-2">
                <span className="text-gray-400">Assistant</span>
                <span className="text-gray-100 font-medium truncate">{pending.client_name}</span>
              </div>
              <div className="flex justify-between gap-2">
                <span className="text-gray-400">Local IP</span>
                <span className="font-mono text-gray-200">{pending.peer_ip}</span>
              </div>
            </div>

            {pending.requested_write && (
              <div className="bg-amber-950/40 border border-amber-600/40 rounded-lg px-3 py-2 text-xs text-amber-200">
                This request asked for write access. Leave the switch below off if you want Read-Only inspection.
              </div>
            )}

            <label className="flex items-center justify-between gap-3 bg-gray-800/60 border border-gray-700/80 rounded-xl p-3 cursor-pointer">
              <div>
                <div className="text-sm font-medium text-gray-100">Allow making changes</div>
                <div className="text-xs text-gray-400">
                  Off = Read-Only (default). On = can also edit module data (e.g. Plot), settings, or state.
                </div>
              </div>
              <input
                type="checkbox"
                checked={allowWriteChoice}
                onChange={(e) => setWriteGrant({ requestId: pending.request_id, allow: e.target.checked })}
                className="w-5 h-5 accent-amber-500 rounded cursor-pointer shrink-0"
              />
            </label>

            <div className="flex items-center gap-2.5 pt-1">
              <button
                onClick={() => handleDecide(false)}
                disabled={busy}
                className="flex-1 min-h-11 px-4 py-2.5 rounded-xl bg-gray-800 hover:bg-gray-700 border border-gray-700 text-sm font-medium text-gray-200 transition-colors"
              >
                Deny
              </button>
              <button
                onClick={() => handleDecide(true)}
                disabled={busy}
                className={`flex-1 min-h-11 px-4 py-2.5 rounded-xl text-sm font-semibold text-white transition-colors ${
                  allowWriteChoice
                    ? 'bg-amber-600 hover:bg-amber-500'
                    : 'bg-purple-600 hover:bg-purple-500'
                }`}
              >
                {allowWriteChoice ? 'Allow Read + Write (1h)' : 'Allow Read-Only (1h)'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Persistent live session badge & one-tap controls */}
      {showActive && (
        <div className="fixed bottom-3 left-3 z-[60] flex flex-col items-start gap-2">
          {pillOpen && (
            <div className="w-72 bg-gray-900/95 backdrop-blur-md border border-gray-700 rounded-xl shadow-2xl p-3.5 space-y-3 text-xs">
              <div className="flex items-center justify-between gap-2">
                <div className="font-semibold text-gray-100 truncate">{active.client_name}</div>
                <span className="font-mono text-gray-400">~{activeMinLeft}m left</span>
              </div>
              <div className="text-gray-400 flex justify-between">
                <span>Peer <span className="font-mono text-gray-300">{active.peer_ip}</span></span>
                <span>{active.call_count || 0} calls</span>
              </div>
              <div className="flex items-center gap-2 pt-1">
                <button
                  onClick={handleToggleWrite}
                  disabled={busy}
                  className={`flex-1 min-h-10 px-2.5 py-1.5 rounded-lg border font-medium transition-colors ${
                    active.allow_write
                      ? 'bg-amber-950/60 border-amber-500/60 text-amber-200 hover:bg-amber-900/60'
                      : 'bg-gray-800 border-gray-700 text-gray-200 hover:bg-gray-700'
                  }`}
                >
                  {active.allow_write ? 'Switch to Read-Only' : 'Enable Changes'}
                </button>
                <button
                  onClick={handleDisconnect}
                  disabled={busy}
                  className="min-h-10 px-3 py-1.5 rounded-lg bg-red-900/60 hover:bg-red-800 border border-red-700/60 text-red-100 font-medium transition-colors"
                >
                  Disconnect
                </button>
              </div>
            </div>
          )}

          <button
            onClick={() => setPillOpenSessionId(pillOpen ? null : active.session_id)}
            className={`min-h-9 px-3 py-1.5 rounded-full border shadow-lg backdrop-blur-md flex items-center gap-2 text-xs font-medium transition-colors ${
              active.allow_write
                ? 'bg-amber-950/85 border-amber-500/60 text-amber-200'
                : 'bg-gray-900/90 border-emerald-500/50 text-emerald-200'
            }`}
            title="Diagnostic session active — tap to toggle write mode or disconnect"
          >
            <span className={`w-2 h-2 rounded-full animate-pulse ${active.allow_write ? 'bg-amber-400' : 'bg-emerald-400'}`} />
            <span>{active.allow_write ? 'Agent · Write ON' : 'Agent · Read-Only'}</span>
          </button>
        </div>
      )}
    </>
  );
}

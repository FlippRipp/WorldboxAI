import { useState, useEffect, useCallback } from 'react';
import { api } from '../../lib/api';

function formatTimestamp(ts) {
  if (!ts) return 'Unknown';
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return 'Unknown';
  }
}

export default function RemoteBridgeSettings() {
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [copied, setCopied] = useState(false);
  const [nowSec, setNowSec] = useState(() => Math.floor(Date.now() / 1000));

  const refresh = useCallback(async () => {
    try {
      const data = await api.getBridgeStatus();
      setStatus(data);
      setNowSec(Math.floor(Date.now() / 1000));
      setError('');
    } catch (e) {
      setError(e.message || 'Could not load bridge status');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    let active = true;
    api.getBridgeStatus()
      .then((data) => {
        if (!active) return;
        setStatus(data);
        setNowSec(Math.floor(Date.now() / 1000));
        setError('');
      })
      .catch((e) => {
        if (active) setError(e.message || 'Could not load bridge status');
      })
      .finally(() => {
        if (active) setLoading(false);
      });

    const onWsState = (ev) => {
      if (ev.detail) {
        setNowSec(Math.floor(Date.now() / 1000));
        setStatus((prev) => {
          const next = ev.detail;
          if (prev?.pairing?.code && next?.pairing?.active && !next.pairing.code) {
            return { ...next, pairing: { ...next.pairing, code: prev.pairing.code } };
          }
          return next;
        });
      }
    };
    window.addEventListener('wb:debug-bridge-state', onWsState);
    return () => {
      active = false;
      window.removeEventListener('wb:debug-bridge-state', onWsState);
    };
  }, []);

  // Countdown tick while pairing window or active session is visible
  const pairExpiresAt = status?.pairing?.expires_at || 0;
  const hasActiveSession = !!status?.active_session;
  useEffect(() => {
    if (!status?.pairing?.active && !hasActiveSession) return undefined;
    const id = setInterval(() => {
      const current = Math.floor(Date.now() / 1000);
      setNowSec(current);
      if (pairExpiresAt && current >= pairExpiresAt) {
        refresh();
      }
    }, 1000);
    return () => clearInterval(id);
  }, [status?.pairing?.active, pairExpiresAt, hasActiveSession, refresh]);

  const handleStartPairing = async () => {
    setBusy(true);
    setError('');
    setCopied(false);
    try {
      const data = await api.startBridgePairing();
      setNowSec(Math.floor(Date.now() / 1000));
      setStatus(data);
    } catch (e) {
      setError(e.message || 'Failed to start pairing');
    } finally {
      setBusy(false);
    }
  };

  const handleCancelPairing = async () => {
    setBusy(true);
    try {
      const data = await api.cancelBridgePairing();
      setStatus(data);
    } catch (e) {
      setError(e.message || 'Failed to cancel pairing');
    } finally {
      setBusy(false);
    }
  };

  const handleUnpair = async () => {
    setBusy(true);
    setError('');
    try {
      const data = await api.unpairBridgeDevice();
      setStatus(data);
    } catch (e) {
      setError(e.message || 'Failed to unpair device');
    } finally {
      setBusy(false);
    }
  };

  const handleToggleWrite = async (nextWrite) => {
    setBusy(true);
    setError('');
    try {
      const data = await api.setBridgeWriteMode(nextWrite);
      setStatus(data);
    } catch (e) {
      setError(e.message || 'Failed to update permission');
    } finally {
      setBusy(false);
    }
  };

  const handleDisconnect = async () => {
    setBusy(true);
    setError('');
    try {
      const data = await api.disconnectBridgeSession();
      setStatus(data);
    } catch (e) {
      setError(e.message || 'Failed to disconnect session');
    } finally {
      setBusy(false);
    }
  };

  const copyCode = async () => {
    const code = status?.pairing?.code;
    if (!code) return;
    try {
      await navigator.clipboard.writeText(code);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API may fail in non-HTTPS contexts; user can still read it
    }
  };

  if (loading) {
    return <div className="text-sm text-gray-400 py-4">Loading diagnostic bridge status…</div>;
  }

  const pairRemaining = status?.pairing?.expires_at
    ? Math.max(0, Math.ceil(status.pairing.expires_at - nowSec))
    : 0;
  const sessRemainingMin = status?.active_session?.expires_at
    ? Math.max(0, Math.ceil((status.active_session.expires_at - nowSec) / 60))
    : 0;

  return (
    <div className="space-y-4">
      {error && (
        <div className="bg-red-900/40 border border-red-700/50 rounded-lg p-3 text-sm text-red-200">
          {error}
        </div>
      )}

      {/* Local Network Info */}
      <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4 space-y-2">
        <div className="flex items-center justify-between gap-2 flex-wrap">
          <div>
            <div className="text-sm font-medium text-gray-200">Local Subnet Endpoint</div>
            <div className="text-xs text-gray-500">
              Connections are only accepted from devices on your exact local Wi-Fi/LAN subnet and require your approval each session.
            </div>
          </div>
          {status?.lan_ips?.length > 0 ? (
            <div className="flex flex-wrap gap-1.5">
              {status.lan_ips.map((ip) => (
                <span key={ip} className="font-mono text-xs px-2.5 py-1 rounded bg-gray-800 border border-gray-700 text-purple-300">
                  {ip}
                </span>
              ))}
            </div>
          ) : (
            <span className="text-xs text-amber-400">No private LAN IP detected</span>
          )}
        </div>
      </div>

      {/* Paired Device & One-Time Pairing Window */}
      <div className="bg-gray-900/70 border border-gray-800 rounded-xl p-4 space-y-4">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <div className="text-sm font-medium text-gray-200">Paired AI Assistant</div>
            {status?.paired ? (
              <div className="text-xs text-gray-400 mt-1 space-y-0.5">
                <div>
                  <span className="text-emerald-400 font-medium">● Paired:</span>{' '}
                  <span className="text-gray-200">{status.paired.client_name}</span>
                  {status.paired.last_ip && (
                    <span className="font-mono text-gray-500"> ({status.paired.last_ip})</span>
                  )}
                </div>
                <div className="text-gray-500">Paired {formatTimestamp(status.paired.paired_at)}</div>
              </div>
            ) : (
              <div className="text-xs text-gray-500 mt-0.5">
                No device paired. Unpaired LAN requests are silently blocked.
              </div>
            )}
          </div>

          <div className="flex items-center gap-2">
            {!status?.pairing?.active && (
              <button
                onClick={handleStartPairing}
                disabled={busy}
                className="min-h-11 px-4 py-2 rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-50 text-sm font-medium text-white transition-colors"
              >
                {status?.paired ? 'Re-pair Device' : 'Pair Device'}
              </button>
            )}
            {status?.paired && (
              <button
                onClick={handleUnpair}
                disabled={busy}
                className="min-h-11 px-3 py-2 rounded-lg bg-gray-800 hover:bg-red-900/50 border border-gray-700 hover:border-red-700/60 text-sm text-gray-300 hover:text-red-200 transition-colors"
              >
                Unpair
              </button>
            )}
          </div>
        </div>

        {status?.pairing?.active && (
          <div className="bg-purple-950/30 border border-purple-500/40 rounded-lg p-4 space-y-3">
            <div className="flex items-center justify-between text-xs">
              <span className="text-purple-200 font-medium">One-Time Pairing Code (single attempt)</span>
              <span className="font-mono text-purple-300">{pairRemaining}s remaining</span>
            </div>

            {status.pairing.code ? (
              <div className="flex items-center justify-between gap-2 bg-gray-950/80 border border-gray-700 rounded-lg px-4 py-3">
                <span className="font-mono text-lg sm:text-xl tracking-wider text-gray-100 select-all">
                  {status.pairing.code}
                </span>
                <button
                  onClick={copyCode}
                  className="min-h-10 px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 text-xs text-gray-200 transition-colors shrink-0"
                >
                  {copied ? 'Copied' : 'Copy'}
                </button>
              </div>
            ) : (
              <div className="text-xs text-gray-400">
                Pairing window active ({pairRemaining}s left). Tap Re-pair to generate a fresh code on this screen.
              </div>
            )}

            <div className="flex items-center justify-between gap-3 flex-wrap">
              <p className="text-xs text-gray-400">
                Share this code with Antigravity{status.lan_ips?.[0] ? ` (phone IP: ${status.lan_ips[0]})` : ''}. Any wrong attempt immediately closes the window.
              </p>
              <button
                onClick={handleCancelPairing}
                disabled={busy}
                className="min-h-10 px-3 py-1.5 rounded bg-gray-800 hover:bg-gray-700 text-xs text-gray-300 transition-colors"
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>

      {/* Active Diagnostic Session */}
      {status?.active_session && (
        <div className="bg-gray-900/70 border border-emerald-600/40 rounded-xl p-4 space-y-3">
          <div className="flex items-center justify-between gap-2 flex-wrap">
            <div>
              <div className="text-sm font-medium text-emerald-300">
                Active Session · {status.active_session.client_name}
              </div>
              <div className="text-xs text-gray-400">
                IP <span className="font-mono">{status.active_session.peer_ip}</span> · Expires in ~{sessRemainingMin}m · {status.active_session.call_count || 0} calls
              </div>
            </div>

            <button
              onClick={handleDisconnect}
              disabled={busy}
              className="min-h-11 px-3.5 py-2 rounded-lg bg-red-900/50 hover:bg-red-800/70 border border-red-700/60 text-xs font-medium text-red-100 transition-colors"
            >
              Disconnect
            </button>
          </div>

          <label className="flex items-center justify-between gap-3 pt-2 border-t border-gray-800 cursor-pointer">
            <div>
              <div className="text-sm text-gray-200">Allow making changes (Read + Write)</div>
              <div className="text-xs text-gray-500">
                Lets the connected assistant update story module data (e.g. Plot), settings, or state. API keys are never accessible.
              </div>
            </div>
            <input
              type="checkbox"
              checked={!!status.active_session.allow_write}
              disabled={busy}
              onChange={(e) => handleToggleWrite(e.target.checked)}
              className="w-5 h-5 accent-amber-500 rounded cursor-pointer shrink-0"
            />
          </label>
        </div>
      )}
    </div>
  );
}

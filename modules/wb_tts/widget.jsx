import React, { useState, useEffect } from 'react';
import { ttsCoordinator } from './ttsClient';

function useTtsSnapshot(storyConfigEnabled) {
  const [, setTick] = useState(0);

  useEffect(() => {
    ttsCoordinator.mountWidget();
    const unsub = ttsCoordinator.subscribe(() => setTick((n) => n + 1));
    return () => {
      unsub();
      ttsCoordinator.unmountWidget();
    };
  }, []);

  useEffect(() => {
    ttsCoordinator.setStoryEnabled(storyConfigEnabled !== false);
  }, [storyConfigEnabled]);

  return {
    config: ttsCoordinator.status?.config || {},
    health: ttsCoordinator.status?.health || {},
    effectiveEnabled: ttsCoordinator.isEffectivelyEnabled(),
    speaking: ttsCoordinator.speaking,
    activeEmotion: ttsCoordinator.activeEmotion || '',
    paused: ttsCoordinator.paused,
  };
}

function HeaderTtsControl({ storyEnabled }) {
  const { health, effectiveEnabled, speaking, activeEmotion } = useTtsSnapshot(storyEnabled);
  const ready = Boolean(health.ready);
  const labelText = speaking
    ? activeEmotion
      ? `Speaking · ${activeEmotion}`
      : 'Speaking'
    : 'TTS';

  return (
    <div className="inline-flex items-center gap-1">
      <button
        type="button"
        onClick={() => ttsCoordinator.toggleEnabled().catch(() => {})}
        className={`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs transition-colors ${
          effectiveEnabled
            ? speaking
              ? 'bg-purple-600/25 border-purple-500/60 text-purple-200'
              : 'bg-gray-900/70 hover:bg-gray-700 border-gray-700 text-gray-200'
            : 'bg-gray-900/40 hover:bg-gray-800 border-gray-800 text-gray-500'
        }`}
        title={
          effectiveEnabled
            ? ready
              ? 'Text-to-Speech active (click to mute)'
              : `Text-to-Speech enabled · Server offline (${health.error || 'not running'})`
            : 'Text-to-Speech muted (click to enable)'
        }
      >
        <span>{effectiveEnabled ? '🔊' : '🔇'}</span>
        <span className="hidden xl:inline">{labelText}</span>
        <span
          className={`w-1.5 h-1.5 rounded-full ${
            !effectiveEnabled
              ? 'bg-gray-600'
              : speaking
                ? 'bg-purple-400 animate-ping'
                : ready
                  ? 'bg-emerald-400'
                  : 'bg-amber-400'
          }`}
        />
      </button>
      {speaking && (
        <button
          type="button"
          onClick={() => ttsCoordinator.stopAll('header_stop')}
          className="px-1.5 py-1 rounded-lg bg-gray-900/80 hover:bg-gray-700 border border-gray-700 text-[11px] text-gray-300 transition-colors"
          title="Stop current speech"
        >
          ⏹
        </button>
      )}
    </div>
  );
}

function SidebarTtsPanel({ storyEnabled }) {
  const { config, health, effectiveEnabled, speaking, activeEmotion } = useTtsSnapshot(storyEnabled);
  const [editingUrl, setEditingUrl] = useState(false);
  const [urlDraft, setUrlDraft] = useState(config.server_url || 'http://127.0.0.1:7864');
  const [probing, setProbing] = useState(false);

  useEffect(() => {
    if (!editingUrl && config.server_url) {
      setUrlDraft(config.server_url);
    }
  }, [config.server_url, editingUrl]);

  const handleSaveUrl = async (e) => {
    e.preventDefault();
    setProbing(true);
    try {
      await ttsCoordinator.updateConfig({ server_url: urlDraft });
      setEditingUrl(false);
    } catch (err) {
      // keep form open on error
    } finally {
      setProbing(false);
    }
  };

  const handleProbe = async () => {
    setProbing(true);
    try {
      await ttsCoordinator.refreshStatus(true);
    } finally {
      setProbing(false);
    }
  };

  const ready = Boolean(health.ready);

  return (
    <div className="rounded-xl border border-gray-700/80 bg-gray-800/60 p-3 space-y-2.5 text-xs">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-sm">{effectiveEnabled ? '🔊' : '🔇'}</span>
          <span className="font-semibold text-gray-200 truncate">Voice Narration</span>
        </div>
        <span
          className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] border ${
            ready
              ? 'bg-emerald-500/10 border-emerald-500/30 text-emerald-300'
              : 'bg-amber-500/10 border-amber-500/30 text-amber-300'
          }`}
          title={health.error || config.server_url || ''}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${ready ? 'bg-emerald-400' : 'bg-amber-400'}`} />
          <span>{ready ? 'Ready' : 'Offline'}</span>
        </span>
      </div>

      <div className="flex items-center gap-1.5">
        <button
          type="button"
          onClick={() => ttsCoordinator.toggleEnabled().catch(() => {})}
          className={`flex-1 py-1.5 px-2.5 rounded-lg border font-medium transition-colors ${
            effectiveEnabled
              ? 'bg-purple-600/25 hover:bg-purple-600/35 border-purple-500/50 text-purple-100'
              : 'bg-gray-900/70 hover:bg-gray-700 border-gray-700 text-gray-400'
          }`}
        >
          {effectiveEnabled
            ? speaking
              ? activeEmotion
                ? `Speaking (${activeEmotion})…`
                : 'Speaking… (Mute)'
              : 'Enabled'
            : 'Muted'}
        </button>

        {speaking ? (
          <button
            type="button"
            onClick={() => ttsCoordinator.stopAll('sidebar_stop')}
            className="py-1.5 px-2.5 rounded-lg bg-red-950/50 hover:bg-red-900/60 border border-red-800/60 text-red-200 transition-colors"
            title="Stop active voice playback"
          >
            Stop
          </button>
        ) : (
          <button
            type="button"
            onClick={() => ttsCoordinator.playPreviewSample()}
            disabled={!ready}
            className="py-1.5 px-2.5 rounded-lg bg-gray-900/70 hover:bg-gray-700 border border-gray-700 text-gray-300 disabled:opacity-40 transition-colors"
            title="Play a short voice test sample"
          >
            Test
          </button>
        )}
      </div>

      {editingUrl ? (
        <form onSubmit={handleSaveUrl} className="space-y-1.5 pt-1 border-t border-gray-700/60">
          <label className="block text-[11px] text-gray-400">TTS Provider URL (local or LAN/Tailscale)</label>
          <input
            type="text"
            value={urlDraft}
            onChange={(e) => setUrlDraft(e.target.value)}
            placeholder="http://192.168.1.50:7864"
            className="w-full px-2.5 py-1.5 rounded-lg bg-gray-900 border border-gray-700 text-gray-100 text-xs font-mono focus:outline-none focus:border-purple-500"
          />
          <div className="flex items-center justify-end gap-1.5">
            <button
              type="button"
              onClick={() => setEditingUrl(false)}
              className="px-2 py-1 rounded bg-gray-800 hover:bg-gray-700 text-gray-400"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={probing}
              className="px-2.5 py-1 rounded bg-purple-600 hover:bg-purple-500 text-white font-medium disabled:opacity-50"
            >
              {probing ? 'Checking…' : 'Save'}
            </button>
          </div>
        </form>
      ) : (
        <div className="flex items-center justify-between gap-2 pt-1 border-t border-gray-700/50 text-[11px] text-gray-400">
          <span className="font-mono truncate" title={config.server_url}>
            {config.server_url || 'http://127.0.0.1:7864'}
          </span>
          <div className="flex items-center gap-1 shrink-0">
            <button
              type="button"
              onClick={handleProbe}
              disabled={probing}
              className="px-1.5 py-0.5 rounded hover:bg-gray-700 text-gray-400 hover:text-gray-200"
              title="Re-check server connection"
            >
              {probing ? '…' : '↻'}
            </button>
            <button
              type="button"
              onClick={() => setEditingUrl(true)}
              className="px-1.5 py-0.5 rounded hover:bg-gray-700 text-purple-300 hover:text-purple-200"
            >
              Host
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export default function TtsWidget({ slotName, config }) {
  const storyEnabled = config?.enabled !== false;
  if (slotName === 'slot_header') {
    return <HeaderTtsControl storyEnabled={storyEnabled} />;
  }
  return <SidebarTtsPanel storyEnabled={storyEnabled} />;
}

import React, { useState, useEffect } from 'react';
import { ttsCoordinator } from '../ttsClient';

const DEFAULT_EMOTIONS = [
  { id: 'neutral', label: 'Neutral' },
  { id: 'warm', label: 'Warm' },
  { id: 'awed', label: 'Awed' },
  { id: 'grim', label: 'Grim' },
  { id: 'somber', label: 'Somber' },
  { id: 'hushed', label: 'Hushed' },
  { id: 'tense', label: 'Tense' },
  { id: 'angry', label: 'Angry' },
  { id: 'cold', label: 'Cold' },
  { id: 'fearful', label: 'Fearful' },
  { id: 'weary', label: 'Weary' },
  { id: 'excited', label: 'Excited' },
  { id: 'amused', label: 'Amused' },
  { id: 'teasing', label: 'Teasing' },
  { id: 'flustered', label: 'Flustered' },
  { id: 'horny', label: 'Horny' },
];

export default function VoiceServer({ onBack }) {
  const [serverUrl, setServerUrl] = useState('http://127.0.0.1:7864');
  const [token, setToken] = useState('');
  const [temperature, setTemperature] = useState(0.9);
  const [chunkSize, setChunkSize] = useState(8);
  const [enabled, setEnabled] = useState(true);
  const [emotionEnabled, setEmotionEnabled] = useState(true);
  const [emotions, setEmotions] = useState(DEFAULT_EMOTIONS);
  const [jevStatus, setJevStatus] = useState({ available: false, enabled: false, tts_enabled: true });
  const [testEmotion, setTestEmotion] = useState('auto');
  const [activeEmotion, setActiveEmotion] = useState('');
  const [sampleText, setSampleText] = useState(
    'Warm candlelight flickered across the rain-streaked glass. "Everything is synced and ready," she murmured.'
  );

  const [health, setHealth] = useState({ reachable: false, ready: false, model: '', error: '' });
  const [saving, setSaving] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [speaking, setSpeaking] = useState(false);

  useEffect(() => {
    ttsCoordinator.mountWidget();
    const unsub = ttsCoordinator.subscribe(() => {
      setSpeaking(ttsCoordinator.speaking);
      setActiveEmotion(ttsCoordinator.activeEmotion || '');
      if (ttsCoordinator.status?.health) {
        setHealth(ttsCoordinator.status.health);
      }
      if (ttsCoordinator.status?.jev) {
        setJevStatus(ttsCoordinator.status.jev);
      }
    });

    ttsCoordinator.refreshStatus(true).then((data) => {
      if (!data) return;
      const cfg = data.config || {};
      setEnabled(cfg.enabled !== false);
      setEmotionEnabled(cfg.emotion_enabled !== false);
      setServerUrl(cfg.server_url || 'http://127.0.0.1:7864');
      setToken(cfg.token || '');
      setTemperature(Number(cfg.temperature) || 0.9);
      setChunkSize(Number(cfg.chunk_size) || 8);
      if (data.health) setHealth(data.health);
      if (Array.isArray(data.emotions) && data.emotions.length > 0) {
        setEmotions(data.emotions);
      }
      if (data.jev) setJevStatus(data.jev);
    });

    return () => {
      unsub();
      ttsCoordinator.unmountWidget();
    };
  }, []);

  const handleSaveAndProbe = async (e) => {
    if (e) e.preventDefault();
    setSaving(true);
    setFeedback('');
    try {
      const res = await ttsCoordinator.updateConfig({
        enabled,
        emotion_enabled: emotionEnabled,
        server_url: serverUrl,
        token,
        temperature: Number(temperature),
        chunk_size: Number(chunkSize),
      });
      setServerUrl(res.config.server_url);
      setToken(res.config.token || '');
      setEmotionEnabled(res.config.emotion_enabled !== false);
      setHealth(res.health || {});
      if (res.jev) setJevStatus(res.jev);
      setFeedback(
        res.health?.ready
          ? `Connected to ${res.health.model || 'Faster Qwen3-TTS'}!`
          : `Saved — server not ready (${res.health?.error || 'offline'}).`
      );
    } catch (err) {
      setFeedback(`Error saving settings: ${err.message}`);
    } finally {
      setSaving(false);
    }
  };

  const handlePlaySample = async () => {
    if (speaking) {
      ttsCoordinator.stopAll('sample_stop');
      return;
    }
    await ttsCoordinator.playPreviewSample(sampleText, testEmotion);
  };

  const jevHint = !emotionEnabled
    ? 'Disabled — narration always uses the clean neutral reference voice.'
    : jevStatus.available
      ? 'Jev active — classifying each sentence across 16 vocal emotions automatically.'
      : !jevStatus.enabled
        ? 'Enable Jev in Model Settings to classify sentences automatically (falling back to Neutral until enabled).'
        : !jevStatus.tts_enabled
          ? 'TTS emotions is unchecked in Jev Settings (falling back to Neutral).'
          : 'No Jev/Router API key configured — falling back to Neutral.';

  return (
    <div className="min-h-dvh bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 text-gray-100 flex flex-col">
      <header className="h-14 border-b border-gray-800 px-4 sm:px-6 flex items-center justify-between bg-gray-900/80 shrink-0">
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => {
              ttsCoordinator.stopAll('leave_studio');
              onBack?.();
            }}
            className="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-xs text-gray-200 transition-colors"
          >
            ← Back
          </button>
          <h1 className="text-base sm:text-lg font-semibold flex items-center gap-2">
            <span>🔊</span>
            <span>Voice Server (Faster Qwen3-TTS)</span>
          </h1>
        </div>

        <span
          className={`inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs border ${
            health.ready
              ? 'bg-emerald-500/15 border-emerald-500/40 text-emerald-200'
              : 'bg-amber-500/15 border-amber-500/40 text-amber-200'
          }`}
        >
          <span className={`w-2 h-2 rounded-full ${health.ready ? 'bg-emerald-400' : 'bg-amber-400'}`} />
          <span>{health.ready ? `Ready · ${health.model || 'Qwen3-TTS'}` : 'Server Offline'}</span>
        </span>
      </header>

      <main className="flex-1 max-w-3xl w-full mx-auto p-4 sm:p-8 space-y-6">
        <form
          onSubmit={handleSaveAndProbe}
          className="rounded-2xl border border-gray-800 bg-gray-900/70 p-5 sm:p-6 space-y-5 shadow-xl"
        >
          <div className="flex items-center justify-between gap-4 pb-4 border-b border-gray-800">
            <div>
              <h2 className="text-sm font-semibold text-gray-100">Enable Text-to-Speech</h2>
              <p className="text-xs text-gray-400 mt-0.5">
                Streams voice from Sentence 1 in Classic Chat and synchronizes card reveal speed in Paced Stage mode.
              </p>
            </div>
            <button
              type="button"
              onClick={() => setEnabled((v) => !v)}
              className={`px-4 py-2 rounded-xl border text-xs font-semibold transition-colors shrink-0 ${
                enabled
                  ? 'bg-purple-600/30 border-purple-500 text-purple-100'
                  : 'bg-gray-800 border-gray-700 text-gray-400'
              }`}
            >
              {enabled ? 'Enabled' : 'Muted'}
            </button>
          </div>

          <div className="flex items-center justify-between gap-4 pb-4 border-b border-gray-800">
            <div>
              <h2 className="text-sm font-semibold text-gray-100">Emotional Delivery (via Jev)</h2>
              <p className="text-xs text-gray-400 mt-0.5">{jevHint}</p>
            </div>
            <button
              type="button"
              onClick={() => setEmotionEnabled((v) => !v)}
              className={`px-4 py-2 rounded-xl border text-xs font-semibold transition-colors shrink-0 ${
                emotionEnabled
                  ? 'bg-purple-600/30 border-purple-500 text-purple-100'
                  : 'bg-gray-800 border-gray-700 text-gray-400'
              }`}
            >
              {emotionEnabled ? 'Emotions On' : 'Neutral Only'}
            </button>
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="sm:col-span-2">
              <label className="block text-xs font-medium text-gray-300 mb-1.5">
                Provider Server URL (Local or Remote GPU Machine)
              </label>
              <input
                type="text"
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                placeholder="http://127.0.0.1:7864 or http://192.168.1.50:7864"
                className="w-full px-3.5 py-2.5 rounded-xl bg-gray-950 border border-gray-700 text-sm font-mono text-gray-100 focus:outline-none focus:border-purple-500"
              />
              <p className="text-[11px] text-gray-500 mt-1">
                WorldBox proxies audio streams through its own backend, so mobile browsers work seamlessly over LAN or Tailscale without opening extra phone ports.
              </p>
            </div>

            <div className="sm:col-span-2">
              <label className="block text-xs font-medium text-gray-300 mb-1.5">
                Bearer Token <span className="text-gray-500 font-normal">(optional — matches WB_TTS_TOKEN)</span>
              </label>
              <input
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder="Leave blank on trusted local networks"
                className="w-full px-3.5 py-2 rounded-xl bg-gray-950 border border-gray-700 text-sm font-mono text-gray-100 focus:outline-none focus:border-purple-500"
              />
            </div>

            <div>
              <div className="flex justify-between text-xs text-gray-300 mb-1">
                <span>Expressiveness Scale (Temperature)</span>
                <span className="font-mono text-gray-400">{Number(temperature).toFixed(2)}</span>
              </div>
              <input
                type="range"
                min="0.3"
                max="1.2"
                step="0.05"
                value={temperature}
                onChange={(e) => setTemperature(Number(e.target.value))}
                className="w-full accent-purple-500"
              />
            </div>

            <div>
              <div className="flex justify-between text-xs text-gray-300 mb-1">
                <span>Stream Chunk Size</span>
                <span className="font-mono text-gray-400">{chunkSize} frames</span>
              </div>
              <input
                type="range"
                min="4"
                max="16"
                step="2"
                value={chunkSize}
                onChange={(e) => setChunkSize(Number(e.target.value))}
                className="w-full accent-purple-500"
              />
            </div>
          </div>

          <div className="flex flex-wrap items-center justify-between gap-3 pt-2">
            <span className="text-xs text-gray-400">{feedback || (health.error ? `Last error: ${health.error}` : '')}</span>
            <button
              type="submit"
              disabled={saving}
              className="px-5 py-2.5 rounded-xl bg-purple-600 hover:bg-purple-500 text-white text-xs font-semibold transition-colors disabled:opacity-50"
            >
              {saving ? 'Connecting…' : 'Save & Check Connection'}
            </button>
          </div>
        </form>

        {/* Live Audio Test Card */}
        <div className="rounded-2xl border border-gray-800 bg-gray-900/70 p-5 sm:p-6 space-y-3 shadow-xl">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <h2 className="text-sm font-semibold text-gray-100">Test Streaming Playback</h2>
              {speaking && activeEmotion && (
                <span className="px-2 py-0.5 rounded-full text-[11px] bg-purple-500/20 border border-purple-500/40 text-purple-200">
                  {activeEmotion}
                </span>
              )}
            </div>
            <div className="flex items-center gap-2 text-xs">
              <label htmlFor="wb-tts-test-emotion" className="text-gray-400">
                Emotion:
              </label>
              <select
                id="wb-tts-test-emotion"
                value={testEmotion}
                onChange={(e) => setTestEmotion(e.target.value)}
                className="px-2.5 py-1.5 rounded-lg bg-gray-950 border border-gray-700 text-xs text-gray-100 focus:outline-none focus:border-purple-500"
              >
                <option value="auto">Auto (via Jev)</option>
                {emotions.map((emo) => (
                  <option key={emo.id} value={emo.id}>
                    {emo.label}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <textarea
            rows={2}
            value={sampleText}
            onChange={(e) => setSampleText(e.target.value)}
            className="w-full px-3.5 py-2.5 rounded-xl bg-gray-950 border border-gray-700 text-sm text-gray-100 focus:outline-none focus:border-purple-500"
          />
          <div className="flex justify-end">
            <button
              type="button"
              onClick={handlePlaySample}
              className={`px-4 py-2 rounded-xl text-xs font-semibold border transition-colors ${
                speaking
                  ? 'bg-red-950/60 border-red-700 text-red-200 hover:bg-red-900/60'
                  : 'bg-gray-800 hover:bg-gray-700 border-gray-700 text-gray-100'
              }`}
            >
              {speaking ? '⏹ Stop Test' : '▶ Stream Test Voice'}
            </button>
          </div>
        </div>

        {/* Remote / Local Launch Instructions */}
        <div className="rounded-2xl border border-gray-800 bg-gray-900/50 p-5 sm:p-6 space-y-2.5 text-xs text-gray-400">
          <h3 className="text-sm font-semibold text-gray-200">Hosting on this PC or another GPU machine</h3>
          <p>
            Run the launcher from the WorldBox root on any NVIDIA GPU host. It creates an isolated environment in{' '}
            <code className="text-gray-200">tts_server/</code>, downloads <code className="text-gray-200">Qwen3-TTS-12Hz-1.7B-Base</code> on first run, and listens on port <code className="text-gray-200">7864</code> across your network:
          </p>
          <pre className="p-3 rounded-xl bg-gray-950 border border-gray-800 text-gray-200 font-mono overflow-x-auto">
{`# Windows
.\\modules\\wb_tts\\tts_server.bat

# Linux
./modules/wb_tts/tts_server.sh`}
          </pre>
        </div>
      </main>
    </div>
  );
}

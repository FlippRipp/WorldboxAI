// Module-scoped singleton Web Audio streaming PCM player + StageReader pacer
// + Classic Chat sentence queue for wb_tts.
//
// Shared across desktop header, desktop sidebar, mobile drawer, and the
// VoiceServer setup screen so responsive slot mounts never duplicate network
// fetches or audio contexts.

const API = '/api/modules/wb_tts';
const DEFAULT_SAMPLE_RATE = 24000;
const PREBUFFER_SEC = 0.16;

const HIDDEN_BLOCK_RE =
  /<!--[\s\S]*?(?:-->|$)|^ {0,3}<(details|internal_states)\b[^>]*>[\s\S]*?(?:<\/\1\s*>|$)/gim;
const TAG_RE = /<\/?\s*(?:color\s*:\s*)?[a-z0-9_:-]+(?:\s+[^>]*)?>/gi;
const MD_LINK_RE = /\[([^\]]+)\]\([^)]+\)/g;
const MD_DECOR_RE = /[*_~`#>]+/g;
const SPEAKABLE_RE = /[A-Za-z0-9\u00C0-\u024F\u0400-\u04FF\u3040-\u30FF\u4E00-\u9FFF]/;

export function cleanSpokenText(raw) {
  if (!raw) return '';
  let text = String(raw).replace(HIDDEN_BLOCK_RE, ' ');
  text = text.replace(MD_LINK_RE, '$1');
  text = text.replace(TAG_RE, '');
  text = text.replace(MD_DECOR_RE, '');
  text = text
    .replace(/[“”]/g, '"')
    .replace(/[‘’]/g, "'")
    .replace(/\s+/g, ' ')
    .trim();
  if (!SPEAKABLE_RE.test(text)) return '';
  return text;
}

class SentenceJob {
  constructor(index, rawText) {
    this.index = index;
    this.rawText = rawText || '';
    this.text = cleanSpokenText(rawText);
    this.sampleRate = DEFAULT_SAMPLE_RATE;
    this.chunks = [];
    this.receivedSamples = 0;
    this.startedFetch = false;
    this.isComplete = this.text.length === 0;
    this.isSilent = this.text.length === 0;
    this.failed = false;
    this.error = '';
    this.abortController = null;
  }

  bufferedSec() {
    return this.sampleRate > 0 ? this.receivedSamples / this.sampleRate : 0;
  }

  totalSec() {
    if (!this.isComplete) return null;
    return this.sampleRate > 0 ? this.receivedSamples / this.sampleRate : 0;
  }

  hasPrebuffer() {
    return (
      this.isSilent ||
      this.failed ||
      this.isComplete ||
      this.bufferedSec() >= PREBUFFER_SEC
    );
  }

  abort() {
    if (this.abortController) {
      try {
        this.abortController.abort();
      } catch (e) {
        // ignore abort errors
      }
      this.abortController = null;
    }
  }
}

class TtsCoordinator {
  constructor() {
    this.listeners = new Set();
    this.status = {
      config: {
        enabled: true,
        server_url: 'http://127.0.0.1:7864',
        token: '',
        has_token: false,
        temperature: 0.9,
        chunk_size: 8,
      },
      health: {
        reachable: false,
        ready: false,
        model: '',
        error: '',
      },
    };
    this.storyEnabled = true;
    this.speaking = false;
    this.paused = false;

    this.audioCtx = null;
    this.activeSources = new Set();
    this.playback = null; // { job, scheduledChunkIdx, playStartCtxTime, cursorCtxTime, started }

    this.stageTurn = null; // { turnId, jobs: Map, segments: [], activeIdx: -1, turnFailed: false }
    this.feedTurn = null;  // { turnId, jobs: Map, segments: [], isComplete: false, activeIdx: -1, turnFailed: false }
    this.feedTimer = null;
    this.pollTimer = null;
    this.mountedCount = 0;
    this.globalAttached = false;
  }

  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  notify() {
    this.listeners.forEach((fn) => {
      try {
        fn();
      } catch (e) {
        // ignore listener error
      }
    });
  }

  isEffectivelyEnabled() {
    return Boolean(this.status?.config?.enabled !== false && this.storyEnabled !== false);
  }

  setStoryEnabled(flag) {
    const next = flag !== false;
    if (this.storyEnabled !== next) {
      this.storyEnabled = next;
      if (!this.isEffectivelyEnabled()) {
        this.stopAll('muted');
      }
      this.notify();
    }
  }

  ensureAudioContext(fromGesture = false) {
    if (typeof window === 'undefined') return null;
    const AudioCtx = window.AudioContext || window.webkitAudioContext;
    if (!AudioCtx) return null;
    if (!this.audioCtx || this.audioCtx.state === 'closed') {
      try {
        this.audioCtx = new AudioCtx({ sampleRate: DEFAULT_SAMPLE_RATE });
      } catch (e) {
        this.audioCtx = new AudioCtx();
      }
    }
    if (fromGesture && this.audioCtx.state === 'suspended' && !this.paused) {
      this.audioCtx.resume().catch(() => {});
    }
    return this.audioCtx;
  }

  attachGlobalListeners() {
    if (this.globalAttached || typeof window === 'undefined') return;
    this.globalAttached = true;

    const unlock = () => {
      this.ensureAudioContext(true);
    };
    window.addEventListener('pointerdown', unlock, { passive: true });
    window.addEventListener('keydown', unlock, { passive: true });

    const onInterrupt = (e) => {
      this.stopAll(e?.detail?.reason || 'interrupt');
    };
    window.addEventListener('wb:tts-interrupt', onInterrupt);

    const onFeedStream = (e) => {
      this.handleFeedStream(e?.detail || {});
    };
    window.addEventListener('wb:feed-stream', onFeedStream);

    // Register StageReader audio-sync pacer
    window.__wbStagePacer = {
      isActive: () => this.isEffectivelyEnabled(),
      onTurnStart: (info) => this.onStageTurnStart(info),
      onSegments: (info) => this.onStageSegments(info),
      isSentenceReady: (turnId, idx) => this.isStageSentenceReady(turnId, idx),
      onEnterSentence: (info) => this.onStageEnterSentence(info),
      getSentenceState: (turnId, idx) => this.getStageSentenceState(turnId, idx),
      onPauseChange: (info) => this.onStagePauseChange(info),
      onTurnEnd: (info) => this.onStageTurnEnd(info),
    };
  }

  mountWidget() {
    this.mountedCount += 1;
    this.attachGlobalListeners();
    if (this.mountedCount === 1) {
      this.refreshStatus(false);
      this.pollTimer = setInterval(() => this.refreshStatus(false), 12000);
    }
  }

  unmountWidget() {
    this.mountedCount = Math.max(0, this.mountedCount - 1);
    if (this.mountedCount === 0 && this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }

  async refreshStatus(probe = false) {
    try {
      const res = await fetch(`${API}/status?probe=${probe ? 'true' : 'false'}`);
      if (!res.ok) return null;
      const data = await res.json();
      this.status = data;
      this.notify();
      return data;
    } catch (e) {
      return null;
    }
  }

  async updateConfig(patch) {
    const res = await fetch(`${API}/config`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch || {}),
    });
    if (!res.ok) {
      const err = await res.text();
      throw new Error(err || `HTTP ${res.status}`);
    }
    const data = await res.json();
    this.status = { ...this.status, config: data.config, health: data.health };
    if (!this.isEffectivelyEnabled()) {
      this.stopAll('muted');
    }
    this.notify();
    return data;
  }

  async toggleEnabled() {
    this.ensureAudioContext(true);
    const next = !(this.status?.config?.enabled !== false);
    return this.updateConfig({ enabled: next });
  }

  // ------------------------------------------------------------------
  // PCM fetch + Web Audio gapless scheduling
  // ------------------------------------------------------------------

  startJobFetch(job, turnState, opts = {}) {
    if (!job || job.startedFetch || job.isSilent) return;
    job.startedFetch = true;
    const ctrl = new AbortController();
    job.abortController = ctrl;

    (async () => {
      let carryByte = null;
      let isFirstChunk = true;
      try {
        const res = await fetch(`${API}/stream`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            text: job.text,
            force: Boolean(opts.force),
          }),
          signal: ctrl.signal,
        });

        if (res.status === 204) {
          job.isSilent = true;
          job.isComplete = true;
          this.onJobUpdated(job);
          return;
        }
        if (!res.ok) {
          job.failed = true;
          job.isComplete = true;
          job.error = `HTTP ${res.status}`;
          if (turnState && res.status >= 500) {
            turnState.turnFailed = true;
          }
          this.onJobUpdated(job);
          return;
        }

        const srHeader = Number(res.headers.get('X-Sample-Rate'));
        if (Number.isFinite(srHeader) && srHeader >= 8000 && srHeader <= 96000) {
          job.sampleRate = srHeader;
        }

        // Update cached health optimistically once audio streams cleanly
        if (!this.status?.health?.ready) {
          this.status = {
            ...this.status,
            health: { ...this.status.health, reachable: true, ready: true, error: '' },
          };
          this.notify();
        }

        if (!res.body || typeof res.body.getReader !== 'function') {
          const buf = await res.arrayBuffer();
          const floats = this.decodePcm16Bytes(new Uint8Array(buf), isFirstChunk);
          if (floats.length > 0) {
            job.chunks.push(floats);
            job.receivedSamples += floats.length;
          }
          job.isComplete = true;
          this.onJobUpdated(job);
          return;
        }

        const reader = res.body.getReader();
        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          if (!value || value.byteLength === 0) continue;

          let bytes = value;
          if (carryByte !== null) {
            const merged = new Uint8Array(value.byteLength + 1);
            merged[0] = carryByte;
            merged.set(value, 1);
            bytes = merged;
            carryByte = null;
          }
          if (bytes.byteLength % 2 === 1) {
            carryByte = bytes[bytes.byteLength - 1];
            bytes = bytes.subarray(0, bytes.byteLength - 1);
          }
          if (bytes.byteLength >= 2) {
            const floats = this.decodePcm16Bytes(bytes, isFirstChunk);
            isFirstChunk = false;
            job.chunks.push(floats);
            job.receivedSamples += floats.length;
            this.onJobUpdated(job);
          }
        }
        job.isComplete = true;
        this.onJobUpdated(job);
      } catch (err) {
        if (err?.name === 'AbortError') return;
        job.failed = true;
        job.isComplete = true;
        job.error = err?.message || 'Stream error';
        if (turnState) turnState.turnFailed = true;
        this.onJobUpdated(job);
      } finally {
        job.abortController = null;
      }
    })();
  }

  decodePcm16Bytes(bytes, applyFadeIn = false) {
    const sampleCount = bytes.byteLength >>> 1;
    const out = new Float32Array(sampleCount);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    for (let i = 0; i < sampleCount; i += 1) {
      out[i] = view.getInt16(i * 2, true) / 32768.0;
    }
    if (applyFadeIn && sampleCount > 0) {
      const ramp = Math.min(72, sampleCount); // ~3 ms anti-click ramp
      for (let i = 0; i < ramp; i += 1) {
        out[i] *= i / ramp;
      }
    }
    return out;
  }

  stopActivePlayback() {
    this.activeSources.forEach((src) => {
      try {
        src.onended = null;
        src.stop();
        src.disconnect();
      } catch (e) {
        // ignore already-stopped nodes
      }
    });
    this.activeSources.clear();
    this.playback = null;
    if (this.speaking) {
      this.speaking = false;
      this.notify();
    }
  }

  stopAll(_reason = 'stop') {
    this.stopActivePlayback();
    if (this.stageTurn) {
      this.stageTurn.jobs.forEach((j) => j.abort());
      this.stageTurn = null;
    }
    if (this.feedTurn) {
      this.feedTurn.jobs.forEach((j) => j.abort());
      this.feedTurn = null;
    }
    if (this.feedTimer) {
      clearInterval(this.feedTimer);
      this.feedTimer = null;
    }
    if (this.paused && this.audioCtx && this.audioCtx.state === 'suspended') {
      this.paused = false;
      this.audioCtx.resume().catch(() => {});
      this.notify();
    } else if (this.paused) {
      this.paused = false;
      this.notify();
    }
  }

  beginPlaybackForJob(job, replayFromStart = false) {
    this.stopActivePlayback();
    if (!job || job.isSilent || job.failed) return;
    if (replayFromStart && !job.startedFetch) {
      this.startJobFetch(job, null);
    }
    this.playback = {
      job,
      scheduledChunkIdx: 0,
      playStartCtxTime: 0,
      cursorCtxTime: 0,
      started: false,
      ended: false,
    };
    this.pumpPlayback();
  }

  pumpPlayback() {
    const pb = this.playback;
    if (!pb || pb.ended) return;
    const { job } = pb;

    if (job.isSilent || job.failed) {
      pb.ended = true;
      if (this.speaking) {
        this.speaking = false;
        this.notify();
      }
      return;
    }

    if (!pb.started && !job.hasPrebuffer()) {
      return;
    }

    const ctx = this.ensureAudioContext(false);
    if (!ctx) {
      job.failed = true;
      pb.ended = true;
      return;
    }

    if (!this.paused && ctx.state === 'suspended') {
      ctx.resume().catch(() => {});
    }

    if (!pb.started) {
      const startAt = ctx.currentTime + 0.015;
      pb.playStartCtxTime = startAt;
      pb.cursorCtxTime = startAt;
      pb.started = true;
      if (!this.speaking) {
        this.speaking = true;
        this.notify();
      }
    }

    while (pb.scheduledChunkIdx < job.chunks.length) {
      const samples = job.chunks[pb.scheduledChunkIdx];
      pb.scheduledChunkIdx += 1;
      if (!samples || samples.length === 0) continue;

      const audioBuf = ctx.createBuffer(1, samples.length, job.sampleRate);
      audioBuf.getChannelData(0).set(samples);

      const src = ctx.createBufferSource();
      src.buffer = audioBuf;
      src.connect(ctx.destination);

      const when = Math.max(ctx.currentTime + 0.004, pb.cursorCtxTime);
      pb.cursorCtxTime = when + samples.length / job.sampleRate;
      this.activeSources.add(src);
      src.onended = () => {
        this.activeSources.delete(src);
        this.checkPlaybackEnded();
      };
      src.start(when);
    }

    this.checkPlaybackEnded();
  }

  checkPlaybackEnded() {
    const pb = this.playback;
    if (!pb || pb.ended) return;
    const { job } = pb;
    if (!job.isComplete || pb.scheduledChunkIdx < job.chunks.length) return;
    const ctx = this.audioCtx;
    if (
      !pb.started ||
      (this.activeSources.size === 0 && (!ctx || ctx.currentTime >= pb.cursorCtxTime - 0.01))
    ) {
      pb.ended = true;
      if (this.speaking) {
        this.speaking = false;
        this.notify();
      }
      if (this.feedTurn) {
        this.advanceFeedQueue();
      }
    }
  }

  onJobUpdated(job) {
    if (this.playback && this.playback.job === job) {
      this.pumpPlayback();
    }
    if (this.feedTurn) {
      this.pumpFeedQueue();
    }
  }

  async playPreviewSample(sampleText) {
    this.stopAll('preview');
    this.ensureAudioContext(true);
    const text =
      sampleText ||
      'Warm candlelight flickered across the rain-streaked window. "Everything works," she said softly.';
    const job = new SentenceJob(0, text);
    this.startJobFetch(job, null, { force: true });
    this.beginPlaybackForJob(job, false);
    this.ensureFeedPumpTimer();
  }

  // ------------------------------------------------------------------
  // StageReader audio-sync pacer implementation
  // ------------------------------------------------------------------

  getOrCreateJob(turnObj, idx, rawText) {
    let job = turnObj.jobs.get(idx);
    if (!job) {
      job = new SentenceJob(idx, rawText);
      turnObj.jobs.set(idx, job);
    }
    return job;
  }

  onStageTurnStart({ turnId }) {
    this.stopAll('stage_turn_start');
    if (!this.isEffectivelyEnabled()) return;
    this.stageTurn = {
      turnId,
      jobs: new Map(),
      segments: [],
      activeIdx: -1,
      turnFailed: false,
    };
  }

  onStageSegments({ turnId, segments, activeIdx }) {
    if (!this.isEffectivelyEnabled()) return;
    if (!this.stageTurn || this.stageTurn.turnId !== turnId) {
      this.stageTurn = {
        turnId,
        jobs: new Map(),
        segments: [],
        activeIdx: activeIdx ?? -1,
        turnFailed: false,
      };
    }
    const st = this.stageTurn;
    st.segments = segments || [];
    if (typeof activeIdx === 'number') st.activeIdx = activeIdx;
    if (st.turnFailed) return;

    // Prefetch Sentence 0 immediately on completion, plus 1 sentence ahead of activeIdx
    const targetStart = Math.max(0, st.activeIdx);
    const prefetchIndices = st.activeIdx < 0 ? [0, 1] : [targetStart, targetStart + 1];
    for (const idx of prefetchIndices) {
      const seg = st.segments[idx];
      if (seg && seg.complete) {
        const job = this.getOrCreateJob(st, idx, seg.text);
        this.startJobFetch(job, st);
      }
    }
  }

  isStageSentenceReady(turnId, idx) {
    const st = this.stageTurn;
    if (!st || st.turnId !== turnId || !this.isEffectivelyEnabled() || st.turnFailed) {
      return false;
    }
    const job = st.jobs.get(idx);
    return Boolean(job && job.hasPrebuffer());
  }

  onStageEnterSentence({ turnId, sentenceIndex, text, segments, replay }) {
    if (!this.isEffectivelyEnabled()) return;
    if (!this.stageTurn || this.stageTurn.turnId !== turnId) {
      this.stageTurn = {
        turnId,
        jobs: new Map(),
        segments: segments || [],
        activeIdx: sentenceIndex,
        turnFailed: false,
      };
    }
    const st = this.stageTurn;
    if (segments) st.segments = segments;
    st.activeIdx = sentenceIndex;
    if (st.turnFailed) return;

    const job = this.getOrCreateJob(st, sentenceIndex, text);
    this.startJobFetch(job, st);
    this.beginPlaybackForJob(job, Boolean(replay));

    // Prefetch next sentence if already streamed from LLM
    const nextSeg = st.segments[sentenceIndex + 1];
    if (nextSeg && nextSeg.complete) {
      const nextJob = this.getOrCreateJob(st, sentenceIndex + 1, nextSeg.text);
      this.startJobFetch(nextJob, st);
    }
  }

  getStageSentenceState(turnId, sentenceIndex) {
    const st = this.stageTurn;
    if (
      !st ||
      st.turnId !== turnId ||
      !this.isEffectivelyEnabled() ||
      st.turnFailed
    ) {
      return { active: false };
    }
    const job = st.jobs.get(sentenceIndex);
    if (!job || job.failed) {
      return { active: false };
    }
    if (job.isSilent) {
      return { active: true, state: 'silent', playedSec: 0, bufferedSec: 0, totalSec: 0 };
    }

    this.pumpPlayback();
    this.checkPlaybackEnded();

    const pb = this.playback && this.playback.job === job ? this.playback : null;
    if (!pb || !pb.started) {
      return {
        active: true,
        state: 'loading',
        playedSec: 0,
        bufferedSec: job.bufferedSec(),
        totalSec: job.totalSec(),
      };
    }

    const ctx = this.audioCtx;
    const rawPlayed = ctx ? Math.max(0, ctx.currentTime - pb.playStartCtxTime) : 0;
    const totalSec = job.totalSec();
    const clampedPlayed = totalSec != null ? Math.min(totalSec, rawPlayed) : rawPlayed;

    return {
      active: true,
      state: pb.ended ? 'ended' : 'playing',
      playedSec: clampedPlayed,
      bufferedSec: job.bufferedSec(),
      totalSec,
    };
  }

  onStagePauseChange({ paused }) {
    this.paused = Boolean(paused);
    if (this.audioCtx) {
      if (this.paused && this.audioCtx.state === 'running') {
        this.audioCtx.suspend().catch(() => {});
      } else if (!this.paused && this.audioCtx.state === 'suspended') {
        this.audioCtx.resume().catch(() => {});
      }
    }
    this.notify();
  }

  onStageTurnEnd({ turnId }) {
    if (this.stageTurn && this.stageTurn.turnId === turnId) {
      this.stopAll('stage_turn_end');
    }
  }

  // ------------------------------------------------------------------
  // Classic Chat ('feed') sequential sentence streaming
  // ------------------------------------------------------------------

  ensureFeedPumpTimer() {
    if (this.feedTimer) return;
    this.feedTimer = setInterval(() => {
      this.pumpPlayback();
      this.checkPlaybackEnded();
      if (!this.playback && !this.feedTurn) {
        clearInterval(this.feedTimer);
        this.feedTimer = null;
      }
    }, 45);
  }

  handleFeedStream(detail) {
    const phase = detail?.phase;
    const turnId = detail?.turnId;

    if (phase === 'abort') {
      if (!this.feedTurn || this.feedTurn.turnId === turnId) {
        this.stopAll('feed_abort');
      }
      return;
    }

    if (!this.isEffectivelyEnabled()) return;

    if (phase === 'start' || !this.feedTurn || this.feedTurn.turnId !== turnId) {
      this.stopAll('feed_turn_start');
      this.feedTurn = {
        turnId,
        jobs: new Map(),
        segments: detail.segments || [],
        isComplete: Boolean(detail.isComplete),
        activeIdx: -1,
        turnFailed: false,
      };
    } else if (phase === 'segments') {
      this.feedTurn.segments = detail.segments || [];
      this.feedTurn.isComplete = Boolean(detail.isComplete);
    }

    this.ensureFeedPumpTimer();
    this.pumpFeedQueue();
  }

  pumpFeedQueue() {
    const ft = this.feedTurn;
    if (!ft || ft.turnFailed || !this.isEffectivelyEnabled()) return;

    // Start first sentence as soon as Sentence 0 finishes streaming from LLM
    if (ft.activeIdx < 0) {
      const seg0 = ft.segments[0];
      if (seg0 && seg0.complete) {
        ft.activeIdx = 0;
        const job0 = this.getOrCreateJob(ft, 0, seg0.text);
        this.startJobFetch(job0, ft);
        this.beginPlaybackForJob(job0, false);
      } else if (ft.isComplete && ft.segments.length === 0) {
        this.feedTurn = null;
        return;
      }
    }

    // Prefetch 1 sentence ahead of currently playing sentence
    if (ft.activeIdx >= 0) {
      const nextIdx = ft.activeIdx + 1;
      const nextSeg = ft.segments[nextIdx];
      if (nextSeg && nextSeg.complete) {
        const nextJob = this.getOrCreateJob(ft, nextIdx, nextSeg.text);
        this.startJobFetch(nextJob, ft);
      }
      // If active job was silent or already finished while waiting for next sentence, advance now
      const curJob = ft.jobs.get(ft.activeIdx);
      const curEnded =
        !curJob ||
        curJob.isSilent ||
        curJob.failed ||
        (this.playback?.job === curJob && this.playback.ended) ||
        !this.playback;
      if (curEnded && (!curJob || curJob.isComplete)) {
        this.advanceFeedQueue();
      }
    }
  }

  advanceFeedQueue() {
    const ft = this.feedTurn;
    if (!ft) return;
    if (ft.turnFailed) {
      this.stopAll('feed_failed');
      return;
    }

    const nextIdx = ft.activeIdx + 1;
    const nextSeg = ft.segments[nextIdx];
    if (nextSeg && nextSeg.complete) {
      ft.activeIdx = nextIdx;
      const nextJob = this.getOrCreateJob(ft, nextIdx, nextSeg.text);
      this.startJobFetch(nextJob, ft);
      this.beginPlaybackForJob(nextJob, false);

      const lookahead = ft.segments[nextIdx + 1];
      if (lookahead && lookahead.complete) {
        const lookJob = this.getOrCreateJob(ft, nextIdx + 1, lookahead.text);
        this.startJobFetch(lookJob, ft);
      }
      return;
    }

    if (ft.isComplete && nextIdx >= ft.segments.length) {
      this.feedTurn = null;
      this.stopActivePlayback();
    }
  }
}

export const ttsCoordinator = new TtsCoordinator();

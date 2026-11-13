// Prosody-weighted timeline and velocity-smoothed reveal controller for
// locking StageReader's character reveal to real-time streaming TTS audio.

const DEFAULT_CHARS_PER_SEC = 15.2;
const LOADING_FALLBACK_SEC = 3.5;
const POST_AUDIO_HOLD_SEC = 0.2;
const TRAILING_SILENCE_TRIM_SEC = 0.06;

/**
 * Build a prosody-weighted cumulative timeline over `plainText`.
 * Punctuation marks that induce natural speech pauses receive extra weight so
 * the visual reveal holds briefly at commas, em-dashes, and mid-beat stops
 * right where the voice pauses.
 */
export function buildProsodyTimeline(plainText) {
  const text = String(plainText || '');
  const len = text.length;
  if (len === 0) {
    return { length: 0, totalWeight: 0, cumNorm: new Float32Array([0]) };
  }

  const cum = new Float32Array(len + 1);
  let acc = 0;
  for (let i = 0; i < len; i += 1) {
    const ch = text[i];
    const nextCh = text[i + 1] || '';
    let w = 1.0;
    if ((ch === ',' || ch === ';' || ch === ':') && /\s/.test(nextCh)) {
      w += 3.2;
    } else if (ch === '—' || ch === '…') {
      w += 4.2;
    } else if ((ch === '.' || ch === '!' || ch === '?') && /\s/.test(nextCh)) {
      w += 5.0;
    }
    acc += w;
    cum[i + 1] = acc;
  }

  const inv = acc > 0 ? 1 / acc : 0;
  const cumNorm = new Float32Array(len + 1);
  for (let i = 1; i <= len; i += 1) {
    cumNorm[i] = cum[i] * inv;
  }
  cumNorm[len] = 1;

  return { length: len, totalWeight: acc, cumNorm };
}

/**
 * Invert `timeline.cumNorm` at normalized audio progress `u` in [0, 1] to find
 * the prosody-aligned floating-point character index in [0, length].
 */
export function prosodyTargetCharPos(timeline, u) {
  if (!timeline || timeline.length === 0) return 0;
  if (u <= 0) return 0;
  if (u >= 1) return timeline.length;

  const { cumNorm, length } = timeline;
  let lo = 0;
  let hi = length;
  while (lo + 1 < hi) {
    const mid = (lo + hi) >>> 1;
    if (cumNorm[mid] <= u) lo = mid;
    else hi = mid;
  }
  const span = cumNorm[lo + 1] - cumNorm[lo];
  const frac = span > 1e-6 ? (u - cumNorm[lo]) / span : 0;
  return Math.min(length, Math.max(0, lo + Math.min(1, Math.max(0, frac))));
}

/**
 * Compute one animation-frame update for audio-locked character reveal.
 *
 * Never moves `revealPos` backward and smoothly blends velocity when total
 * sentence duration refines from an initial streaming estimate to the exact
 * sample count once the final PCM chunk arrives.
 */
export function stepAudioSyncedReveal({
  revealPos,
  timeline,
  dt,
  pacerState,
  waitingSec = 0,
  endedHoldSec = 0,
}) {
  const len = timeline ? timeline.length : 0;
  if (len === 0) {
    return {
      revealPos: 0,
      endedHoldSec: endedHoldSec + dt,
      shouldAdvance: pacerState?.state === 'ended' || pacerState?.state === 'silent',
      fallbackToClock: false,
    };
  }

  const state = pacerState?.state || 'loading';

  // 1. Waiting for initial PCM prebuffer to start playing on Web Audio
  if (state === 'loading') {
    if (waitingSec >= LOADING_FALLBACK_SEC) {
      return {
        revealPos,
        endedHoldSec: 0,
        shouldAdvance: false,
        fallbackToClock: true,
      };
    }
    return {
      revealPos,
      endedHoldSec: 0,
      shouldAdvance: false,
      fallbackToClock: false,
    };
  }

  // 2. Audio stream finished playing (or sentence had no speakable words)
  if (state === 'ended' || state === 'silent') {
    if (revealPos < len) {
      const catchUpSpeed = Math.max(46, len * 2.8);
      const nextPos = Math.min(len, revealPos + catchUpSpeed * dt);
      return {
        revealPos: nextPos,
        endedHoldSec: 0,
        shouldAdvance: false,
        fallbackToClock: false,
      };
    }
    const nextHold = endedHoldSec + dt;
    const requiredHold = state === 'silent' ? 0.35 : POST_AUDIO_HOLD_SEC;
    return {
      revealPos: len,
      endedHoldSec: nextHold,
      shouldAdvance: nextHold >= requiredHold,
      fallbackToClock: false,
    };
  }

  // 3. Actively speaking: lock velocity smoothly to live audio playback progress
  const playedSec = Math.max(0, Number(pacerState?.playedSec) || 0);
  const bufferedSec = Math.max(0, Number(pacerState?.bufferedSec) || 0);
  const exactTotalSec =
    pacerState?.totalSec != null && Number.isFinite(pacerState.totalSec)
      ? Number(pacerState.totalSec)
      : null;

  let effectiveDurationSec;
  if (exactTotalSec != null && exactTotalSec > 0) {
    effectiveDurationSec = Math.max(0.28, exactTotalSec - TRAILING_SILENCE_TRIM_SEC);
  } else {
    const prosodyEstimate = Math.max(0.55, (timeline.totalWeight || len) / DEFAULT_CHARS_PER_SEC);
    effectiveDurationSec = Math.max(prosodyEstimate, bufferedSec + 0.32, playedSec + 0.25);
  }

  const u = Math.min(1, Math.max(0, playedSec / effectiveDurationSec));
  const targetPos = prosodyTargetCharPos(timeline, u);

  const baseSpeed = len / effectiveDurationSec;
  const error = targetPos - revealPos;
  // Proportional velocity blend: speeds up gently if behind audio, slows gently
  // if slightly ahead, and never reverses direction.
  const minSpeed = error < -1.5 ? baseSpeed * 0.22 : baseSpeed * 0.42;
  const maxSpeed = baseSpeed * 2.35;
  const velocity = Math.max(minSpeed, Math.min(maxSpeed, baseSpeed + error * 5.2));
  const nextPos = Math.min(len, Math.max(revealPos, revealPos + velocity * dt));

  return {
    revealPos: nextPos,
    endedHoldSec: 0,
    shouldAdvance: false,
    fallbackToClock: false,
  };
}

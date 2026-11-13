import test from 'node:test';
import assert from 'node:assert/strict';
import {
  buildProsodyTimeline,
  prosodyTargetCharPos,
  stepAudioSyncedReveal,
} from '../src/lib/stagePacer.js';

test('prosody timeline pauses naturally across commas and em-dashes while staying monotonic', () => {
  const plain = 'Wait, do not open that door — something is breathing inside.';
  const tl = buildProsodyTimeline(plain);
  assert.equal(tl.length, plain.length);
  assert.equal(prosodyTargetCharPos(tl, 0), 0);
  assert.equal(prosodyTargetCharPos(tl, 1), plain.length);

  let prev = 0;
  for (let i = 1; i <= 20; i += 1) {
    const pos = prosodyTargetCharPos(tl, i / 20);
    assert.ok(pos >= prev, `expected monotonic pos at step ${i}: ${pos} < ${prev}`);
    prev = pos;
  }
});

test('stepAudioSyncedReveal holds during brief audio prebuffer and falls back if server stalls', () => {
  const tl = buildProsodyTimeline('Short test sentence.');
  const hold = stepAudioSyncedReveal({
    revealPos: 0,
    timeline: tl,
    dt: 0.016,
    pacerState: { active: true, state: 'loading', playedSec: 0, bufferedSec: 0.05, totalSec: null },
    waitingSec: 0.4,
    endedHoldSec: 0,
  });
  assert.equal(hold.revealPos, 0);
  assert.equal(hold.fallbackToClock, false);

  const stalled = stepAudioSyncedReveal({
    revealPos: 0,
    timeline: tl,
    dt: 0.016,
    pacerState: { active: true, state: 'loading', playedSec: 0, bufferedSec: 0, totalSec: null },
    waitingSec: 3.6,
    endedHoldSec: 0,
  });
  assert.equal(stalled.fallbackToClock, true);
});

test('stepAudioSyncedReveal smoothly tracks streaming audio and never jumps backward when exact duration arrives', () => {
  const text = 'She stepped into the lantern light, her voice barely above a whisper.';
  const tl = buildProsodyTimeline(text);
  let pos = 0;
  let endedHoldSec = 0;
  const dt = 1 / 60;

  // Phase 1: first 1.0s while audio is still streaming (totalSec unknown)
  for (let t = 0; t < 1.0; t += dt) {
    const res = stepAudioSyncedReveal({
      revealPos: pos,
      timeline: tl,
      dt,
      pacerState: {
        active: true,
        state: 'playing',
        playedSec: t,
        bufferedSec: t + 0.6,
        totalSec: null,
      },
      waitingSec: t,
      endedHoldSec,
    });
    assert.ok(res.revealPos >= pos, 'revealPos must never move backward');
    pos = res.revealPos;
  }

  // Phase 2: stream finishes and reports exact totalSec = 3.8s (even if slightly longer than initial estimate)
  for (let t = 1.0; t <= 3.8; t += dt) {
    const res = stepAudioSyncedReveal({
      revealPos: pos,
      timeline: tl,
      dt,
      pacerState: {
        active: true,
        state: 'playing',
        playedSec: t,
        bufferedSec: 3.8,
        totalSec: 3.8,
      },
      waitingSec: t,
      endedHoldSec,
    });
    assert.ok(res.revealPos >= pos, 'revealPos must stay monotonic across duration refinement');
    pos = res.revealPos;
  }

  assert.ok(pos > text.length * 0.9, `expected reveal near completion by audio end, got ${pos}/${text.length}`);

  // Phase 3: audio finishes -> finishes remaining chars and holds ~200ms before advancing
  let advanced = false;
  for (let i = 0; i < 30; i += 1) {
    const res = stepAudioSyncedReveal({
      revealPos: pos,
      timeline: tl,
      dt,
      pacerState: {
        active: true,
        state: 'ended',
        playedSec: 3.8,
        bufferedSec: 3.8,
        totalSec: 3.8,
      },
      waitingSec: 4.0,
      endedHoldSec,
    });
    pos = res.revealPos;
    endedHoldSec = res.endedHoldSec;
    if (res.shouldAdvance) {
      advanced = true;
      break;
    }
  }
  assert.equal(pos, text.length);
  assert.equal(advanced, true);
});

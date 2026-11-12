import test from 'node:test';
import assert from 'node:assert/strict';
import { splitStageSentences, stripStageHiddenBlocks } from '../src/lib/stageSegments.js';

test('splits multi-sentence prose while respecting abbreviations and tiny lead-ins', () => {
  const text = 'Dr. Vance stepped into the dim room. "Oh!" She caught her breath as the door clicked shut. Outside, rain drummed against the glass.';
  const segs = splitStageSentences(text, { isComplete: true });
  assert.equal(segs.length, 3);
  assert.equal(segs[0].text, 'Dr. Vance stepped into the dim room.');
  assert.equal(segs[1].text, '"Oh!" She caught her breath as the door clicked shut.');
  assert.equal(segs[2].text, 'Outside, rain drummed against the glass.');
  assert.ok(segs.every((s) => s.complete === true));
});

test('keeps conversational multi-sentence quotes together in one dialogue beat', () => {
  const text = '<pink>"Stay close to me. Don\'t look away."</pink> Her hand rested warmly on his chest.';
  const segs = splitStageSentences(text, { isComplete: true });
  assert.equal(segs.length, 2);
  assert.equal(segs[0].text, '<pink>"Stay close to me. Don\'t look away."</pink>');
  assert.equal(segs[1].text, 'Her hand rested warmly on his chest.');
});

test('marks trailing unfinished streaming sentence as complete=false', () => {
  const stream = 'The candle flickered once before going out. In the sudden hush, she leaned';
  const segs = splitStageSentences(stream, { isComplete: false });
  assert.equal(segs.length, 2);
  assert.equal(segs[0].complete, true);
  assert.equal(segs[0].text, 'The candle flickered once before going out.');
  assert.equal(segs[1].complete, false);
  assert.equal(segs[1].text, 'In the sudden hush, she leaned');
});

test('strips hidden details and internal_states blocks cleanly', () => {
  const raw = '<internal_states>mood: tense</internal_states>\n\nWarm candlelight spilled across the table.\n\n<details><summary>Stats</summary>HP: 10</details>';
  assert.ok(!stripStageHiddenBlocks(raw).includes('mood: tense'));
  const segs = splitStageSentences(raw, { isComplete: true });
  assert.equal(segs.length, 1);
  assert.equal(segs[0].text, 'Warm candlelight spilled across the table.');
});

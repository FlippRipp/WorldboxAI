import assert from 'node:assert/strict';
import test from 'node:test';
import { appendImportedBlocks } from '../src/lib/promptImport.js';

function block(id, text, enabled = true) {
  return { id, display_name: 'Story Mode', enabled, config: { text } };
}

test('import preserves existing blocks and every incoming block when IDs clash', () => {
  const existing = [block('st_story_mode', 'Existing rules.')];
  const imported = [
    block('st_story_mode', 'First story variant.', false),
    block('st_story_mode', 'Second story variant.', false),
    block('unique', 'Another prompt.'),
  ];
  const before = JSON.parse(JSON.stringify({ existing, imported }));
  const result = appendImportedBlocks(existing, imported);

  assert.equal(new Set(result.map((item) => item.id)).size, 4);
  assert.strictEqual(result[0], existing[0]);
  result.slice(1).forEach((item, index) => {
    assert.deepEqual(item, { ...imported[index], id: item.id });
  });
  assert.equal(result[3].id, 'unique');
  assert.deepEqual({ existing, imported }, before);
  assert.deepEqual(appendImportedBlocks(existing, imported), result);
});

test('suffix allocation respects IDs in both the draft and the incoming batch', () => {
  const result = appendImportedBlocks([
    block('story', 'Existing.'),
    block('story_2', 'Already suffixed.'),
  ], [
    block('story', 'Duplicate.'),
    block('story_3', 'Keep this ID.'),
    block('story', 'Another duplicate.'),
  ]);

  assert.deepEqual(result.map((item) => item.id),
    ['story', 'story_2', 'story_4', 'story_3', 'story_5']);
});

test('repeated imports remain valid without replacing earlier imports', () => {
  const imported = [block('story', 'Variant one.', false), block('story_2', 'Variant two.')];
  const first = appendImportedBlocks([], imported);
  const second = appendImportedBlocks(first, imported);
  const third = appendImportedBlocks(second, imported);

  assert.equal(new Set(third.map((item) => item.id)).size, 6);
  assert.deepEqual(third.slice(0, 4), second);
  assert.deepEqual(third.map((item) => item.config.text),
    ['Variant one.', 'Variant two.', 'Variant one.', 'Variant two.', 'Variant one.', 'Variant two.']);
  assert.deepEqual(third.map((item) => item.enabled), [false, true, false, true, false, true]);
});

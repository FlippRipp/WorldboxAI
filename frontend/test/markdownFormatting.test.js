import test from 'node:test';
import assert from 'node:assert/strict';
import { DIALOGUE_COLORS, dialogueColorFromStyle, normalizeDialogueColor, parseStoryMarkdown } from '../src/lib/markdownParser.js';

test('preset dialogue colors and optional tones preserve quoted Markdown', () => {
  for (const color of DIALOGUE_COLORS) {
    const html = parseStoryMarkdown(`<${color}>"**Hello**, friend."</${color}> Ordinary narration.`);
    assert.match(html, new RegExp(`story-color-${color}`));
    assert.match(html, /<strong>Hello<\/strong>/);
    assert.match(html, /<\/span> Ordinary narration/);
  }
  for (const tone of ['shout', 'whisper', 'measured', 'tremble']) {
    const html = parseStoryMarkdown(`<gold:${tone}>“Stay here.”</gold:${tone}>`);
    assert.match(html, new RegExp(`story-tone-${tone}`));
    assert.match(html, /“Stay here.”/);
  }
});

test('legacy color prefix and whitespace tone spellings use the fixed palette', () => {
  assert.match(parseStoryMarkdown('<color:cyan>"Morning."</color:cyan>'), /story-color-cyan/);
  assert.match(parseStoryMarkdown('<pink whisper>"Quiet."</pink whisper>'), /story-tone-whisper/);
  assert.doesNotMatch(parseStoryMarkdown('<red:flash>"No custom effects."</red:flash>'), /story-dialogue-color/);
});

test('streaming color tags do not consume prose after a completed quote', () => {
  assert.match(parseStoryMarkdown('<gold:measured>"Hel'), /story-color-gold[^]*"Hel/);
  assert.match(parseStoryMarkdown('<cyan>"Hello." She waves.'), /<\/span> She waves\./);
});

test('nested internal-state panels remain collapsed and parse their own Markdown', () => {
  const html = parseStoryMarkdown(`Story first.

<!-- GFX_START -->
<internal_states>
<details open onclick="alert(1)">
<summary>INTERNAL **STATES**</summary>
<details>
<summary>NPC AGENDAS</summary>
- <b>Ari</b>: **Find the key**
- <cyan>"I will look."</cyan>
</details>
</details>
</internal_states>
<!-- GFX_END -->

Story after.`);
  assert.equal((html.match(/<details\b/g) || []).length, 2);
  assert.match(html, /<summary>INTERNAL <strong>STATES<\/strong><\/summary>/);
  assert.match(html, /<li><b>Ari<\/b>: <strong>Find the key<\/strong><\/li>/);
  assert.match(html, /story-color-cyan/);
  assert.match(html, /Story after/);
  assert.doesNotMatch(html, /<details[^>]*(?:open|onclick)/);
});

test('incomplete streamed state panels keep their content inside a disclosure', () => {
  const html = parseStoryMarkdown('<details>\n<summary>NPC AGENDAS</summary>\n- Waiting for the next token');
  assert.match(html, /^<details[^]*<summary>NPC AGENDAS<\/summary>/);
  assert.match(html, /<li>Waiting for the next token<\/li>/);
  assert.match(html, /<\/details>\n$/);
});

test('code fences and inline code keep literal color and panel examples', () => {
  const html = parseStoryMarkdown('`<cyan>"literal"</cyan>`\n\n```html\n<details><summary>Example</summary>\n<gold:shout>"Stop!"</gold:shout>\n</details>\n```');
  assert.doesNotMatch(html, /story-color-|<details\b/);
  assert.match(html, /&lt;cyan&gt;/);
  assert.match(html, /class="code-copy"/);
  assert.match(html, /&lt;gold:shout&gt;/);
});

test('closing tags inside panel code examples do not end the panel', () => {
  const html = parseStoryMarkdown('<details>\n<summary>Example</summary>\n```html\n</details>\n```\n\nStill in this panel.\n</details>\n\nAfter the panel.');
  assert.match(html, /&lt;\/details&gt;/);
  assert.match(html, /Still in this panel\.<\/p>\n<\/div><\/details>\n<p>After the panel\./);
  const inline = parseStoryMarkdown('<details>\n<summary>Example</summary>\nUse `</details>` as the closing tag.\n</details>\n\nAfter.');
  assert.match(inline, /<code>&lt;\/details&gt;<\/code>/);
  assert.match(inline, /<\/details>\n<p>After\./);
  const unmatched = parseStoryMarkdown('<details>\n<summary>Example</summary>\nUnmatched ` here.\n</details>\n\nAfter.');
  assert.match(unmatched, /<\/details>\n<p>After\./);
  const after = parseStoryMarkdown('<details>\n<summary>Example</summary>\nUnmatched ` here.\n</details>\n\nAfter with `code`.');
  assert.match(after, /<\/details>\n<p>After with <code>code<\/code>/);
});

test('indented code inside a panel preserves whitespace and literal tags', () => {
  const html = parseStoryMarkdown('<details>\n<summary>Example</summary>\n\n    <cyan>"literal"</cyan>\n\n</details>');
  assert.match(html, /<pre><code>&lt;cyan&gt;/);
  assert.doesNotMatch(html, /story-color-cyan/);
});

test('ordinary Markdown, dialogue quotes, and streaming quotes retain existing formatting', () => {
  const html = parseStoryMarkdown('# Heading\n\n"A **bold** answer."\n\n- One\n- Two\n\n> A quote\n\n“unfinished');
  assert.match(html, /<h1>Heading<\/h1>/);
  assert.match(html, /<span class="text-quote">"A <strong>bold<\/strong> answer\."<\/span>/);
  assert.match(html, /<ul>/);
  assert.match(html, /<blockquote>/);
  assert.match(html, /<span class="text-quote">“unfinished<\/span>/);
});

test('color validation rejects executable, resource, and arbitrary CSS values', () => {
  assert.equal(normalizeDialogueColor(' #56B4E9 '), '#56b4e9');
  assert.equal(normalizeDialogueColor('#abc'), '#abc');
  assert.equal(normalizeDialogueColor('CYAN'), 'cyan');
  for (const unsafe of ['red', '#12345', '#12345678', 'url(https://example.com)', 'var(--evil)', 'expression(alert(1))', 'cyan;position:fixed', '#fff!important', 'rgb(1,2,3)']) {
    assert.equal(normalizeDialogueColor(unsafe), null, unsafe);
  }
  assert.equal(dialogueColorFromStyle('color:#56B4E9;position:fixed;inset:0;background:url(https://example.com)'), '#56b4e9');
  assert.equal(dialogueColorFromStyle('background:url(https://example.com); color:expression(alert(1))'), null);
  assert.equal(dialogueColorFromStyle('color:cyan; color:var(--evil)'), null);
  assert.equal(dialogueColorFromStyle('background-color:cyan'), null);
});

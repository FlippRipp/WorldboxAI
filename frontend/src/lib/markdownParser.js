import { Marked } from 'marked';

export const DIALOGUE_COLORS = ['cyan', 'pink', 'teal', 'orange', 'gold', 'violet', 'salmon', 'orchid', 'yellow', 'plum'];
const TONES = ['shout', 'whisper', 'measured', 'tremble'];
const COLOR_OPEN = new RegExp(`^<\\s*(?:color\\s*:\\s*)?(${DIALOGUE_COLORS.join('|')})(?:(?:\\s*:\\s*|\\s+)(${TONES.join('|')}))?\\s*>`, 'i');

// Accept literal colors only: no CSS functions, variables, URLs, or expressions.
export function normalizeDialogueColor(value) {
  const color = String(value || '').trim().toLowerCase();
  return /^(?:#[\da-f]{3}|#[\da-f]{6})$/.test(color) || DIALOGUE_COLORS.includes(color) ? color : null;
}

// A supported span may carry other CSS from a preset. Keep only its validated
// color declaration, never the original style string or any other property.
export function dialogueColorFromStyle(style) {
  let color = null;
  for (const declaration of String(style || '').split(';')) {
    const match = /^\s*color\s*:\s*(.*?)\s*$/i.exec(declaration);
    if (match) color = normalizeDialogueColor(match[1]);
  }
  return color;
}

const RE_STRAIGHT = /^"([^"]*)("?)/;
const RE_CURLY = /^“([^”]*)(”?)/;

const dialogueExtension = {
  name: 'dialogue',
  level: 'inline',
  start(src) { return src.match(/["“]/)?.index; },
  tokenizer(src) {
    const cap = src[0] === '"' ? RE_STRAIGHT.exec(src)
      : src[0] === '“' ? RE_CURLY.exec(src) : null;
    if (!cap) return undefined;
    const token = { type: 'dialogue', raw: cap[0], open: src[0], close: cap[2] || '', tokens: [] };
    this.lexer.inline(cap[1], token.tokens);
    return token;
  },
  renderer(token) {
    return `<span class="text-quote">${token.open}${this.parser.parseInline(token.tokens)}${token.close}</span>`;
  },
};

const coloredDialogueExtension = {
  name: 'coloredDialogue',
  level: 'inline',
  start(src) { return src.indexOf('<'); },
  tokenizer(src) {
    const opening = COLOR_OPEN.exec(src);
    if (!opening) return undefined;
    const color = opening[1].toLowerCase();
    const tone = opening[2]?.toLowerCase();
    const closing = new RegExp(`<\\/\\s*(?:color\\s*:\\s*)?${color}${tone ? `(?:(?:\\s*:\\s*|\\s+)${tone})?` : ''}\\s*>`, 'i');
    const rest = src.slice(opening[0].length);
    const end = closing.exec(rest);
    // During a stream, stop at a completed quote even if the closing tag has
    // not arrived. This also keeps an omitted closing tag from coloring prose.
    const quote = /^\s*(?:"[^"]*"|“[^”]*”)/.exec(rest);
    const content = end ? rest.slice(0, end.index) : quote ? quote[0] : rest;
    const raw = opening[0] + content + (end ? end[0] : '');
    const token = { type: 'coloredDialogue', raw, color, tone, tokens: [] };
    this.lexer.inline(content, token.tokens);
    return token;
  },
  renderer(token) {
    const classes = `story-dialogue-color story-color-${token.color}${token.tone ? ` story-tone-${token.tone}` : ''}`;
    return `<span class="${classes}">${this.parser.parseInline(token.tokens)}</span>`;
  },
};

// Match the same nested HTML tag while leaving comments, fenced code, and
// inline code alone. Missing closing tags are tolerated during generation.
function readContainer(src, tag, openingLength) {
  const tokens = /<!--[\s\S]*?(?:-->|$)|^ {0,3}(`{3,}|~{3,})([^\n]*)(?:\n|$)|(`+)|<\/?(details|internal_states)\b[^>]*>/gim;
  tokens.lastIndex = openingLength;
  let depth = 1;
  let fence = null;
  let token;
  while ((token = tokens.exec(src))) {
    if (token[1]) {
      if (fence && token[1][0] === fence[0] && token[1].length >= fence.length && !token[2].trim()) fence = null;
      else if (!fence) fence = token[1];
      continue;
    }
    if (fence) continue;
    if (token[3]) {
      // An unmatched backtick is plain Markdown text, not an open code span.
      // Only skip a span when another run of the same length closes it.
      const paragraph = src.slice(tokens.lastIndex).split(/\n[ \t]*\n/, 1)[0];
      for (const closing of paragraph.matchAll(/`+/g)) {
        if (closing[0].length === token[3].length) {
          tokens.lastIndex += closing.index + closing[0].length;
          break;
        }
      }
      continue;
    }
    if (token[4]?.toLowerCase() !== tag) continue;
    depth += token[0].startsWith('</') ? -1 : 1;
    if (depth === 0) return { raw: src.slice(0, tokens.lastIndex), content: src.slice(openingLength, token.index) };
  }
  return { raw: src, content: src.slice(openingLength) };
}

const statePanelExtension = {
  name: 'statePanel',
  level: 'block',
  start(src) { return src.match(/^ {0,3}<(?:details|internal_states)\b/im)?.index; },
  tokenizer(src) {
    const opening = /^ {0,3}<(details|internal_states)\b[^>]*>/i.exec(src);
    if (!opening) return undefined;
    const tag = opening[1].toLowerCase();
    const container = readContainer(src, tag, opening[0].length);
    let content = container.content;
    let summary = 'Details';
    if (tag === 'details') {
      const header = /^\s*<summary\b[^>]*>([\s\S]*?)<\/summary\s*>/i.exec(content);
      if (header) {
        summary = header[1];
        content = content.slice(header[0].length);
      }
    }
    return {
      type: 'statePanel', raw: container.raw, tag,
      summary: this.lexer.inlineTokens(summary),
      tokens: this.lexer.blockTokens(content.replace(/^\n+|\n+$/g, '')),
    };
  },
  renderer(token) {
    const body = this.parser.parse(token.tokens);
    return token.tag === 'internal_states'
      ? `<div class="story-internal-states">${body}</div>\n`
      : `<details class="story-state-panel"><summary>${this.parser.parseInline(token.summary)}</summary><div class="story-state-body">${body}</div></details>\n`;
  },
};

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// Pure parsing is shared with tests. Rendered output must still pass through
// DOMPurify in markdown.js before being inserted into the document.
const md = new Marked({ gfm: true, breaks: true });
md.use({
  extensions: [dialogueExtension, coloredDialogueExtension, statePanelExtension],
  renderer: {
    code(token) {
      const lang = (token.lang || '').trim().split(/\s+/)[0];
      const langClass = lang ? ` class="language-${escapeHtml(lang)}"` : '';
      return '<div class="code-block"><button type="button" class="code-copy">Copy</button>'
        + `<pre><code${langClass}>${escapeHtml(token.text)}\n</code></pre></div>`;
    },
  },
});

export function parseStoryMarkdown(text) {
  return md.parse(text);
}

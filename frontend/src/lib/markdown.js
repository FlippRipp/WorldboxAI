import { Marked } from 'marked';
import DOMPurify from 'dompurify';

// Single markdown pipeline for all story text (historical messages and the
// live stream), so the two never render differently.

// Dialogue quotes ("..." and curly "...") render as themed .text-quote spans.
// Streaming tolerance: an unclosed quote colors to the end of the received
// text, so speech stays highlighted while it is still being generated.
const RE_STRAIGHT = /^"([^"]*)("?)/;
const RE_CURLY = /^“([^”]*)(”?)/;

const dialogueExtension = {
  name: 'dialogue',
  level: 'inline',
  start(src) {
    return src.match(/["“]/)?.index;
  },
  tokenizer(src) {
    const cap = src[0] === '"' ? RE_STRAIGHT.exec(src)
      : src[0] === '“' ? RE_CURLY.exec(src)
      : null;
    if (!cap) return undefined;
    const token = {
      type: 'dialogue',
      raw: cap[0],
      open: src[0],
      close: cap[2] || '',
      tokens: [],
    };
    // Tokenize the inner text so markdown inside quotes (bold, italics) works.
    this.lexer.inline(cap[1], token.tokens);
    return token;
  },
  renderer(token) {
    const inner = this.parser.parseInline(token.tokens);
    return `<span class="text-quote">${token.open}${inner}${token.close}</span>`;
  },
};

function escapeHtml(s) {
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

// Fenced code blocks get a copy button; the click handler lives in
// MarkdownRenderer (event delegation on the container).
const renderer = {
  code(token) {
    const lang = (token.lang || '').trim().split(/\s+/)[0];
    const langClass = lang ? ` class="language-${escapeHtml(lang)}"` : '';
    return (
      '<div class="code-block">' +
      '<button type="button" class="code-copy">Copy</button>' +
      `<pre><code${langClass}>${escapeHtml(token.text)}\n</code></pre>` +
      '</div>'
    );
  },
};

const md = new Marked({ gfm: true, breaks: true });
md.use({ extensions: [dialogueExtension], renderer });

// The model can emit arbitrary HTML; only structure we render on purpose
// survives. Everything else (scripts, imgs, event handlers) is stripped.
const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    'p', 'br', 'hr', 'strong', 'em', 'del', 'code', 'pre', 'span', 'div', 'button',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote',
    'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a',
  ],
  ALLOWED_ATTR: ['class', 'href', 'start', 'type', 'align', 'colspan', 'rowspan', 'title', 'target', 'rel'],
};

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noreferrer');
  }
});

export function renderMarkdown(text) {
  if (!text) return '';
  return DOMPurify.sanitize(md.parse(text), PURIFY_CONFIG);
}

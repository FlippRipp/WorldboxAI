import DOMPurify from 'dompurify';
import { dialogueColorFromStyle, normalizeDialogueColor, parseStoryMarkdown } from './markdownParser';

// Single markdown pipeline for all story text (historical messages and the
// live stream), so the two never render differently.

// The model can emit arbitrary HTML; only structure we render on purpose
// survives. The attribute hook allows a literal text color, never arbitrary CSS.
const PURIFY_CONFIG = {
  ALLOWED_TAGS: [
    'p', 'br', 'hr', 'strong', 'em', 'b', 'i', 'del', 'code', 'pre', 'span', 'div', 'button', 'font',
    'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'ul', 'ol', 'li', 'blockquote',
    'table', 'thead', 'tbody', 'tr', 'th', 'td', 'a', 'details', 'summary',
  ],
  ALLOWED_ATTR: ['class', 'href', 'start', 'type', 'align', 'colspan', 'rowspan', 'title', 'target', 'rel', 'style', 'color'],
};

DOMPurify.addHook('uponSanitizeAttribute', (node, data) => {
  if (data.attrName === 'style') {
    const color = node.tagName === 'SPAN' ? dialogueColorFromStyle(data.attrValue) : null;
    if (color) data.attrValue = `color: ${color}`;
    else data.keepAttr = false;
  }
  if (data.attrName === 'color') {
    const color = node.tagName === 'FONT' ? normalizeDialogueColor(data.attrValue) : null;
    if (color) data.attrValue = color;
    else data.keepAttr = false;
  }
});

DOMPurify.addHook('afterSanitizeAttributes', (node) => {
  if ((node.tagName === 'SPAN' && node.hasAttribute('style')) || (node.tagName === 'FONT' && node.hasAttribute('color'))) {
    node.classList.add('story-dialogue-color');
  }
  // Open is intentionally not allowed: state panels start collapsed, and the
  // browser supplies their keyboard and touch interaction after rendering.
  if (node.tagName === 'DETAILS') node.removeAttribute('open');
  if (node.tagName === 'A') {
    node.setAttribute('target', '_blank');
    node.setAttribute('rel', 'noreferrer');
  }
});

export function renderMarkdown(text) {
  if (!text) return '';
  return DOMPurify.sanitize(parseStoryMarkdown(text), PURIFY_CONFIG);
}

// Splits narrative prose into individual sentence beats for Paced Stage mode,
// working identically on live-streaming prefixes and finished turns while
// preserving dialogue quotes and <color:...> tags when splitting multi-sentence
// quotes across cards.

const STATE_BLOCK_RE =
  /<!--[\s\S]*?(?:-->|$)|^ {0,3}<(details|internal_states)\b[^>]*>[\s\S]*?(?:<\/\1\s*>|$)/gim;

const ABBREVIATIONS = new Set([
  'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'st', 'vs', 'etc', 'inc', 'ltd',
  'capt', 'sgt', 'col', 'gen', 'lt', 'cmdr', 'gov', 'sen', 'rep', 'rev', 'no',
]);

const OPEN_COLOR_TAG_RE =
  /^<\s*((?:color\s*:\s*)?[a-z]+)(?:(?:\s*:\s*|\s+)[a-z]+)?\s*>/i;
const CLOSE_COLOR_TAG_RE =
  /^<\/\s*((?:color\s*:\s*)?[a-z]+)(?:(?:\s*:\s*|\s+)[a-z]+)?\s*>/i;
const NON_COLOR_HTML_TAGS = new Set([
  'p', 'br', 'hr', 'em', 'i', 'strong', 'b', 'u', 's', 'del', 'code', 'pre',
  'blockquote', 'ul', 'ol', 'li', 'span', 'div', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
  'details', 'summary', 'internal_states', 'table', 'thead', 'tbody', 'tr', 'th', 'td',
]);

// Remove collapsed state/tracker containers (<details>, <internal_states>) so
// raw tracker markup never plays across the centered reading stage; those blocks
// remain inspectable in the History log.
export function stripStageHiddenBlocks(raw) {
  if (!raw) return '';
  return String(raw).replace(STATE_BLOCK_RE, '\n\n').replace(/\r\n/g, '\n');
}

function endsWithAbbreviation(sliceBeforeDot) {
  const m = /(?:^|[\s(["“'])([A-Za-z.]{1,8})$/.exec(sliceBeforeDot);
  if (!m) return false;
  const word = m[1].replace(/\./g, '').toLowerCase();
  if (!word) return false;
  if (word.length === 1) return true; // Single initial, e.g. "J. R."
  return ABBREVIATIONS.has(word);
}

// Visible character count (excluding markdown markers and color tags) used to
// decide whether a tiny exclamation ("Oh!") should stay paired with the next
// clause instead of flashing by on its own.
export function visibleLength(text) {
  if (!text) return 0;
  return String(text)
    .replace(/<\/?\s*(?:color\s*:\s*)?[a-z]+(?:(?:\s*:\s*|\s+)[a-z]+)?\s*>/gi, '')
    .replace(/[*_~`#>]/g, '')
    .trim().length;
}

function makeCloseColorTag(openTagFull, tagHead) {
  if (!openTagFull) return '';
  const normalizedHead = String(tagHead || '').replace(/\s+/g, '');
  if (/^color:/i.test(normalizedHead)) {
    return '</color>';
  }
  return `</${normalizedHead}>`;
}

/**
 * Split narrative text into ordered single-sentence stage beats:
 *   [{ index, charStart, charEnd, text, complete }]
 *
 * Multi-sentence spoken quotes split cleanly sentence-by-sentence while
 * re-wrapping each part in any active quote mark and <color> tag so dialogue
 * styling renders identically on every card.
 */
export function splitStageSentences(rawText, { isComplete = true } = {}) {
  const cleaned = stripStageHiddenBlocks(rawText);
  if (!cleaned.trim()) return [];

  const segments = [];
  const n = cleaned.length;
  let segStart = 0;
  while (segStart < n && /\s/.test(cleaned[segStart])) segStart += 1;

  let i = segStart;
  let inStraightQuote = false;
  let inCurlyQuote = false;
  let activeColorOpen = '';
  let activeColorClose = '';

  let segInheritedQuote = ''; // '"' or '“' if segment started mid-quote
  let segInheritedColorOpen = '';
  let segInheritedColorClose = '';

  const pushSegment = (endIdx, complete, endStraight, endCurly, endColorClose) => {
    let s = segStart;
    let e = endIdx;
    while (s < e && /\s/.test(cleaned[s])) s += 1;
    while (e > s && /\s/.test(cleaned[e - 1])) e -= 1;
    if (e <= s) return;

    let slice = cleaned.slice(s, e);
    if (segInheritedQuote && !slice.startsWith('"') && !slice.startsWith('“')) {
      slice = segInheritedQuote + slice;
    }
    if (endStraight && !slice.endsWith('"')) {
      slice = `${slice}"`;
    } else if (endCurly && !slice.endsWith('”')) {
      slice = `${slice}”`;
    }
    if (segInheritedColorOpen && !OPEN_COLOR_TAG_RE.test(slice)) {
      slice = segInheritedColorOpen + slice;
    }
    const closeNeeded = endColorClose || segInheritedColorClose;
    if (
      closeNeeded &&
      (endColorClose || segInheritedColorOpen) &&
      !/<\/\s*(?:color\s*:\s*)?[a-z]+(?:(?:\s*:\s*|\s+)[a-z]+)?\s*>\s*$/i.test(slice)
    ) {
      slice = slice + closeNeeded;
    }

    segments.push({
      index: segments.length,
      charStart: s,
      charEnd: e,
      text: slice,
      complete,
    });
  };

  while (i < n) {
    const ch = cleaned[i];

    // Track custom <pink> / <color:pink:whisper> dialogue tags across mid-quote splits
    if (ch === '<') {
      const closeMatch = CLOSE_COLOR_TAG_RE.exec(cleaned.slice(i));
      if (closeMatch) {
        const tagBase = closeMatch[1].replace(/^color\s*:\s*/i, '').toLowerCase();
        if (!NON_COLOR_HTML_TAGS.has(tagBase)) {
          activeColorOpen = '';
          activeColorClose = '';
          i += closeMatch[0].length;
          continue;
        }
      } else {
        const openMatch = OPEN_COLOR_TAG_RE.exec(cleaned.slice(i));
        if (openMatch) {
          const tagBase = openMatch[1].replace(/^color\s*:\s*/i, '').toLowerCase();
          if (!NON_COLOR_HTML_TAGS.has(tagBase)) {
            activeColorOpen = openMatch[0];
            activeColorClose = makeCloseColorTag(openMatch[0], openMatch[1]);
            i += openMatch[0].length;
            continue;
          }
        }
      }
    }

    if (ch === '"') {
      inStraightQuote = !inStraightQuote;
    } else if (ch === '“') {
      inCurlyQuote = true;
    } else if (ch === '”') {
      inCurlyQuote = false;
    }

    // 1. Paragraph break (\n\s*\n) always closes a beat and resets unclosed quote state
    if (ch === '\n') {
      let j = i + 1;
      let sawSecondNewline = false;
      while (j < n && /[ \t\n]/.test(cleaned[j])) {
        if (cleaned[j] === '\n') sawSecondNewline = true;
        j += 1;
      }
      if (sawSecondNewline) {
        pushSegment(i, true, false, false, activeColorClose);
        inStraightQuote = false;
        inCurlyQuote = false;
        activeColorOpen = '';
        activeColorClose = '';
        segInheritedQuote = '';
        segInheritedColorOpen = '';
        segInheritedColorClose = '';
        segStart = j;
        i = j;
        continue;
      }
    }

    // 2. Sentence-ending punctuation (. ! ? …)
    if (ch === '.' || ch === '!' || ch === '?' || ch === '…') {
      let pEnd = i + 1;
      while (pEnd < n && (cleaned[pEnd] === '.' || cleaned[pEnd] === '!' || cleaned[pEnd] === '?' || cleaned[pEnd] === '…')) {
        pEnd += 1;
      }
      // Consume trailing closing quotes, parens, emphasis markers, or </color> tags
      let after = pEnd;
      let straightAfter = inStraightQuote;
      let curlyAfter = inCurlyQuote;
      let colorOpenAfter = activeColorOpen;
      let colorCloseAfter = activeColorClose;

      while (after < n) {
        const cAfter = cleaned[after];
        if (cAfter === '"') {
          straightAfter = !straightAfter;
          after += 1;
          continue;
        }
        if (cAfter === '”') {
          curlyAfter = false;
          after += 1;
          continue;
        }
        if (/['’)\]*_]/.test(cAfter)) {
          after += 1;
          continue;
        }
        const closeTag = CLOSE_COLOR_TAG_RE.exec(cleaned.slice(after));
        if (closeTag) {
          colorOpenAfter = '';
          colorCloseAfter = '';
          after += closeTag[0].length;
          continue;
        }
        break;
      }

      const isSingleDot = ch === '.' && pEnd === i + 1;
      const isAbbr = isSingleDot && endsWithAbbreviation(cleaned.slice(segStart, i));
      const atEnd = after >= n;
      const followedBySpace = !atEnd && /\s/.test(cleaned[after]);
      const candidateLen = visibleLength(cleaned.slice(segStart, after));

      if (!isAbbr && (followedBySpace || (atEnd && isComplete))) {
        let nextNonSpace = after;
        let newlineCount = 0;
        while (nextNonSpace < n && /\s/.test(cleaned[nextNonSpace])) {
          if (cleaned[nextNonSpace] === '\n') newlineCount += 1;
          nextNonSpace += 1;
        }
        const hasParaBreak = newlineCount >= 2;

        if (candidateLen >= 14 || hasParaBreak || (atEnd && isComplete)) {
          const midStraight = !hasParaBreak && straightAfter;
          const midCurly = !hasParaBreak && curlyAfter;
          const midColorClose = !hasParaBreak ? colorCloseAfter : '';

          pushSegment(after, true, midStraight, midCurly, midColorClose);

          inStraightQuote = midStraight;
          inCurlyQuote = midCurly;
          activeColorOpen = hasParaBreak ? '' : colorOpenAfter;
          activeColorClose = hasParaBreak ? '' : colorCloseAfter;

          segInheritedQuote = midStraight ? '"' : midCurly ? '“' : '';
          segInheritedColorOpen = activeColorOpen;
          segInheritedColorClose = activeColorClose;

          segStart = nextNonSpace;
          i = nextNonSpace;
          continue;
        }
      }
      inStraightQuote = straightAfter;
      inCurlyQuote = curlyAfter;
      activeColorOpen = colorOpenAfter;
      activeColorClose = colorCloseAfter;
      i = Math.max(i + 1, after);
      continue;
    }

    i += 1;
  }

  // Remaining trailing text (either final unpunctuated clause or in-flight stream)
  if (segStart < n) {
    const rem = cleaned.slice(segStart);
    if (!isComplete && visibleLength(rem) > 240) {
      const clauseMatch = /^([\s\S]{140,240}?[,;—:])\s+/.exec(rem);
      if (clauseMatch) {
        const cut = segStart + clauseMatch[1].length;
        pushSegment(cut, true, inStraightQuote, inCurlyQuote, activeColorClose);
        segInheritedQuote = inStraightQuote ? '"' : inCurlyQuote ? '“' : '';
        segInheritedColorOpen = activeColorOpen;
        segInheritedColorClose = activeColorClose;
        segStart = cut;
        while (segStart < n && /\s/.test(cleaned[segStart])) segStart += 1;
      }
    }
    if (segStart < n) {
      pushSegment(n, isComplete, isComplete && inStraightQuote, isComplete && inCurlyQuote, isComplete ? activeColorClose : '');
    }
  }

  return segments;
}

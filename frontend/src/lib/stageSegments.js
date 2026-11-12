// Splits narrative prose into comfortable sentence-sized stage beats for Paced
// Stage mode, working identically on live-streaming prefixes and finished turns.

const STATE_BLOCK_RE =
  /<!--[\s\S]*?(?:-->|$)|^ {0,3}<(details|internal_states)\b[^>]*>[\s\S]*?(?:<\/\1\s*>|$)/gim;

const ABBREVIATIONS = new Set([
  'mr', 'mrs', 'ms', 'dr', 'prof', 'sr', 'jr', 'st', 'vs', 'etc', 'inc', 'ltd',
  'capt', 'sgt', 'col', 'gen', 'lt', 'cmdr', 'gov', 'sen', 'rep', 'rev', 'no',
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

/**
 * Split narrative text into ordered stage beats:
 *   [{ index, charStart, charEnd, text, complete }]
 *
 * When `isComplete` is false (live token stream), the final segment is marked
 * `complete: false` until a sentence boundary or paragraph break arrives so the
 * stage waits for the full sentence before laying it out.
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

  const pushSegment = (endIdx, complete) => {
    let s = segStart;
    let e = endIdx;
    while (s < e && /\s/.test(cleaned[s])) s += 1;
    while (e > s && /\s/.test(cleaned[e - 1])) e -= 1;
    if (e <= s) return;
    segments.push({
      index: segments.length,
      charStart: s,
      charEnd: e,
      text: cleaned.slice(s, e),
      complete,
    });
  };

  while (i < n) {
    const ch = cleaned[i];

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
        pushSegment(i, true);
        inStraightQuote = false;
        inCurlyQuote = false;
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
        const closeTag = /^<\/\s*(?:color\s*:\s*)?[a-z]+(?:(?:\s*:\s*|\s+)[a-z]+)?\s*>/i.exec(cleaned.slice(after));
        if (closeTag) {
          after += closeTag[0].length;
          continue;
        }
        break;
      }

      const isSingleDot = ch === '.' && pEnd === i + 1;
      const isAbbr = isSingleDot && endsWithAbbreviation(cleaned.slice(segStart, i));
      const atEnd = after >= n;
      const followedBySpace = !atEnd && /\s/.test(cleaned[after]);
      const stillInQuote = straightAfter || curlyAfter;
      const candidateLen = visibleLength(cleaned.slice(segStart, after));

      // Keep normal-length spoken quotes ("Stay here. Don't move.") together in a
      // single dialogue box unless the quote stretches into a long monologue (> 150 chars).
      if (!isAbbr && (followedBySpace || (atEnd && isComplete)) && (!stillInQuote || candidateLen >= 150)) {
        let nextNonSpace = after;
        let newlineCount = 0;
        while (nextNonSpace < n && /\s/.test(cleaned[nextNonSpace])) {
          if (cleaned[nextNonSpace] === '\n') newlineCount += 1;
          nextNonSpace += 1;
        }
        const hasParaBreak = newlineCount >= 2;

        if (candidateLen >= 14 || hasParaBreak || (atEnd && isComplete)) {
          pushSegment(after, true);
          inStraightQuote = hasParaBreak ? false : straightAfter;
          inCurlyQuote = hasParaBreak ? false : curlyAfter;
          segStart = nextNonSpace;
          i = nextNonSpace;
          continue;
        }
      }
      inStraightQuote = straightAfter;
      inCurlyQuote = curlyAfter;
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
        pushSegment(cut, true);
        segStart = cut;
        while (segStart < n && /\s/.test(cleaned[segStart])) segStart += 1;
      }
    }
    if (segStart < n) {
      pushSegment(n, isComplete);
    }
  }

  return segments;
}

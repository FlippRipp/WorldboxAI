import { createElement, useState, useEffect, useMemo, useCallback, useRef } from 'react';
import { renderMarkdown } from '../../lib/markdown';
import { splitStageSentences } from '../../lib/stageSegments';
import { ChatFeed } from './ChatFeed';
import SlotRenderer from '../Slots/SlotRenderer';

let globalTurnSeq = 0;

function emitReaderSync(detail) {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent('wb:reader-sync', { detail }));
}

function estimateSentenceDurationMs(plainText, speed) {
  const s = Math.max(15, Number(speed) || 38);
  const pauseScale = Math.min(1.75, Math.max(0.45, 38 / s));
  const text = String(plainText || '');
  const total = text.length;
  let holdsMs = 0;
  for (let i = 0; i < total; i += 1) {
    const ch = text[i];
    const nextCh = text[i + 1] || '';
    if ((ch === ',' || ch === ';' || ch === ':') && /\s/.test(nextCh)) {
      holdsMs += 155 * pauseScale;
    } else if (ch === '—' || ch === '…') {
      holdsMs += 220 * pauseScale;
    } else if ((ch === '.' || ch === '!' || ch === '?') && /\s/.test(nextCh)) {
      holdsMs += 300 * pauseScale;
    }
  }
  const revealMs = (total / s) * 1000;
  const doneHoldMs = Math.min(1350, Math.max(520, 410 + total * 3.2)) * pauseScale;
  return Math.round(Math.max(1200, revealMs + holdsMs + doneHoldMs));
}

function attrsToReactProps(el, key) {
  const props = { key };
  for (const attr of Array.from(el.attributes || [])) {
    if (attr.name === 'class') {
      props.className = attr.value;
    } else if (attr.name === 'style') {
      const styleObj = {};
      for (const part of attr.value.split(';')) {
        const idx = part.indexOf(':');
        if (idx > 0) {
          const prop = part
            .slice(0, idx)
            .trim()
            .replace(/-([a-z])/g, (_, c) => c.toUpperCase());
          const val = part.slice(idx + 1).trim();
          if (prop && val) styleObj[prop] = val;
        }
      }
      props.style = styleObj;
    } else if (attr.name === 'for') {
      props.htmlFor = attr.value;
    } else {
      props[attr.name] = attr.value;
    }
  }
  return props;
}

function parseSentenceDom(sentenceText) {
  const html = renderMarkdown(sentenceText || '');
  if (!html || typeof DOMParser === 'undefined') {
    const fallback = String(sentenceText || '');
    return {
      nodes: fallback ? [{ kind: 'text', text: fallback, start: 0, end: fallback.length }] : [],
      plainText: fallback,
    };
  }
  const doc = new DOMParser().parseFromString(html, 'text/html');
  let offset = 0;
  let plainText = '';

  const walk = (domNode, keyPrefix) => {
    if (domNode.nodeType === 3) {
      const text = domNode.nodeValue || '';
      if (!text) return null;
      const start = offset;
      offset += text.length;
      plainText += text;
      return { kind: 'text', text, start, end: offset, key: keyPrefix };
    }
    if (domNode.nodeType !== 1) return null;
    const tag = domNode.tagName.toLowerCase();
    if (tag === 'br') {
      return { kind: 'void', tag: 'br', props: { key: keyPrefix } };
    }
    if (tag === 'hr') {
      return { kind: 'void', tag: 'hr', props: { key: keyPrefix } };
    }
    const children = [];
    Array.from(domNode.childNodes).forEach((child, idx) => {
      const parsed = walk(child, `${keyPrefix}-${idx}`);
      if (parsed) children.push(parsed);
    });
    return {
      kind: 'el',
      tag,
      props: attrsToReactProps(domNode, keyPrefix),
      children,
    };
  };

  const topNodes = [];
  Array.from(doc.body.childNodes).forEach((child, idx) => {
    const parsed = walk(child, `n-${idx}`);
    if (parsed) topNodes.push(parsed);
  });
  return { nodes: topNodes, plainText };
}

function renderRevealedNode(node, revealPos, fullyRevealed) {
  if (node.kind === 'void') {
    return createElement(node.tag, node.props);
  }
  if (node.kind === 'text') {
    if (fullyRevealed || revealPos >= node.end) {
      return node.text;
    }
    if (revealPos <= node.start) {
      return (
        <span key={node.key} className="opacity-0 select-none pointer-events-none" aria-hidden="true">
          {node.text}
        </span>
      );
    }
    const rel = revealPos - node.start;
    const solidEnd = Math.max(0, Math.floor(rel) - 2);
    const leadEnd = Math.min(node.text.length, Math.ceil(rel) + 1);
    const solid = node.text.slice(0, solidEnd);
    const leadSlice = node.text.slice(solidEnd, leadEnd);
    const hidden = node.text.slice(leadEnd);

    return (
      <span key={node.key}>
        {solid}
        {Array.from(leadSlice).map((ch, idx) => {
          const charPos = solidEnd + idx;
          const alpha = Math.min(1, Math.max(0.18, (rel - charPos + 1) / 2.6));
          return (
            <span key={idx} style={{ opacity: alpha }}>
              {ch}
            </span>
          );
        })}
        {hidden && (
          <span className="opacity-0 select-none pointer-events-none" aria-hidden="true">
            {hidden}
          </span>
        )}
      </span>
    );
  }
  return createElement(
    node.tag,
    node.props,
    node.children.map((child) => renderRevealedNode(child, revealPos, fullyRevealed))
  );
}

// Pre-laid-out sentence renderer: the full sentence occupies its exact final
// line wraps from frame 0 while characters glide smoothly from transparent to
// visible, preventing mid-word line-wrap jumps.
function RevealedSentence({ text, revealPos, fullyRevealed }) {
  const parsed = useMemo(() => parseSentenceDom(text), [text]);
  if (!text) return null;
  return (
    <div className="md-content max-w-none stage-sentence">
      {parsed.nodes.map((node) => renderRevealedNode(node, revealPos, fullyRevealed))}
    </div>
  );
}

function findLastAssistantInfo(messages) {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    const m = messages[i];
    if (m.role === 'assistant' || m.role === 'system') {
      const beats = m.error
        ? [{ index: 0, charStart: 0, charEnd: (m.content || '').length, text: m.content || '', complete: true }]
        : splitStageSentences(m.content || '', { isComplete: true });
      return { message: m, index: i, beats };
    }
  }
  return null;
}

function findLastUserPrompt(messages) {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === 'user') return messages[i].content;
  }
  return null;
}

function StageSwipes({ swipes, busy, onPrev, onNext, onRegenerate }) {
  if (!swipes || swipes.count < 1) return null;
  const active = swipes.active ?? 0;
  const count = swipes.count ?? 1;
  return (
    <div className="flex items-center gap-1 text-gray-400">
      <button
        type="button"
        onClick={onPrev}
        disabled={busy || active <= 0}
        className="p-1.5 rounded hover:text-gray-100 hover:bg-white/10 disabled:opacity-30 transition-colors"
        title="Previous variant"
      >
        ‹
      </button>
      <span className="text-xs font-mono tabular-nums px-1">{active + 1}/{count}</span>
      <button
        type="button"
        onClick={onNext}
        disabled={busy}
        className="p-1.5 rounded hover:text-gray-100 hover:bg-white/10 disabled:opacity-30 transition-colors"
        title={active < count - 1 ? 'Next variant' : 'Regenerate'}
      >
        ›
      </button>
      <button
        type="button"
        onClick={onRegenerate}
        disabled={busy}
        className="p-1.5 rounded hover:text-gray-100 hover:bg-white/10 disabled:opacity-30 transition-colors text-xs"
        title="Regenerate response"
      >
        ↻
      </button>
    </div>
  );
}

export default function StageReader({
  messages,
  currentStream,
  currentReasoning,
  swipes,
  busy,
  postProcessing,
  pipelineStatus,
  editRequest,
  currentTurn,
  density = 'comfortable',
  scrollControlRef,
  onUserScroll,
  onBranchMessage,
  onRegenerate,
  onSwipe,
  onEditMessage,
  onDeleteMessage,
  modules,
  slotState,
  moduleConfigs,
  readerSpeed = 38,
  onChangeReaderSpeed,
  historyOpen = false,
  onOpenHistory,
  onCloseHistory,
}) {
  const saveId = slotState?.active_save_id ?? null;

  const [view, setView] = useState({
    playing: false,
    waitingBuffer: false,
    paused: false,
    sentenceText: '',
    sentenceIdx: 0,
    totalSentences: 0,
    revealPos: 0,
    totalChars: 0,
    revealedCharEnd: null,
    revealAllHistory: false,
  });
  const [idleBeatOffset, setIdleBeatOffset] = useState(null);
  const [dragX, setDragX] = useState(0);

  const cardRef = useRef(null);
  const touchRef = useRef(null);
  const lastSwipeAtRef = useRef(0);

  const propsRef = useRef({
    messages,
    currentStream,
    postProcessing,
    readerSpeed,
    saveId,
  });

  const engineRef = useRef({
    mode: 'idle',
    turnId: 0,
    saveId: null,
    baselineMsgCount: 0,
    startedAt: 0,
    firstCompleteAt: 0,
    streamDone: false,
    rawText: '',
    segments: [],
    sentenceIdx: -1,
    sentencePlain: '',
    revealPos: 0,
    lastCharIdx: -1,
    holdUntil: 0,
    doneHoldUntil: 0,
    paused: false,
    revealAllHistory: false,
    lastTick: 0,
  });

  useEffect(() => {
    propsRef.current = {
      messages,
      currentStream,
      postProcessing,
      readerSpeed,
      saveId,
    };
  }, [messages, currentStream, postProcessing, readerSpeed, saveId]);

  // Auto-open History overlay if user triggers editLast (ArrowUp in composer)
  useEffect(() => {
    if (editRequest && onOpenHistory) {
      onOpenHistory();
    }
  }, [editRequest, onOpenHistory]);

  // Main 60fps stage playback ticker
  useEffect(() => {
    let rafId = 0;

    const pushView = (eng) => {
      const seg = eng.sentenceIdx >= 0 ? eng.segments[eng.sentenceIdx] : null;
      const nextView = {
        playing: eng.mode === 'playing',
        waitingBuffer: eng.mode === 'playing' && eng.sentenceIdx < 0,
        paused: eng.paused,
        sentenceText: seg ? seg.text : '',
        sentenceIdx: Math.max(0, eng.sentenceIdx),
        totalSentences: eng.segments.length,
        revealPos: eng.revealPos,
        totalChars: eng.sentencePlain.length,
        revealedCharEnd: seg ? seg.charEnd : 0,
        revealAllHistory: eng.revealAllHistory,
      };
      setView((prev) => {
        if (
          prev.playing === nextView.playing &&
          prev.waitingBuffer === nextView.waitingBuffer &&
          prev.paused === nextView.paused &&
          prev.sentenceText === nextView.sentenceText &&
          prev.sentenceIdx === nextView.sentenceIdx &&
          prev.totalSentences === nextView.totalSentences &&
          Math.abs(prev.revealPos - nextView.revealPos) < 0.15 &&
          prev.totalChars === nextView.totalChars &&
          prev.revealedCharEnd === nextView.revealedCharEnd &&
          prev.revealAllHistory === nextView.revealAllHistory
        ) {
          return prev;
        }
        return nextView;
      });
    };

    const enterSentence = (eng, idx, now, speed) => {
      const seg = eng.segments[idx];
      if (!seg) return;
      const parsed = parseSentenceDom(seg.text);
      eng.sentenceIdx = idx;
      eng.sentencePlain = parsed.plainText;
      eng.revealPos = 0;
      eng.lastCharIdx = -1;
      eng.holdUntil = 0;
      eng.doneHoldUntil = 0;
      emitReaderSync({
        phase: 'sentence',
        turnId: eng.turnId,
        sentenceIndex: idx,
        totalSentences: eng.segments.length,
        charStart: seg.charStart,
        charEnd: seg.charEnd,
        text: seg.text,
        isStreamComplete: eng.streamDone,
        speed,
        durationMs: estimateSentenceDurationMs(parsed.plainText, speed),
      });
    };

    const step = (now) => {
      const p = propsRef.current;
      const eng = engineRef.current;
      const dt = eng.lastTick ? Math.min(0.08, Math.max(0, (now - eng.lastTick) / 1000)) : 0.016;
      eng.lastTick = now;

      // Reset cleanly on save switch
      if (eng.saveId !== p.saveId) {
        if (eng.mode === 'playing') {
          emitReaderSync({ phase: 'turn_end', turnId: eng.turnId, reason: 'stopped' });
        }
        eng.mode = 'idle';
        eng.saveId = p.saveId;
        eng.rawText = '';
        eng.segments = [];
        eng.sentenceIdx = -1;
        setIdleBeatOffset(null);
        pushView(eng);
      }

      // Detect start of a fresh streamed turn
      if (p.currentStream != null && eng.mode !== 'playing') {
        globalTurnSeq += 1;
        eng.mode = 'playing';
        eng.turnId = globalTurnSeq;
        eng.saveId = p.saveId;
        eng.baselineMsgCount = p.messages.length;
        eng.startedAt = now;
        eng.firstCompleteAt = 0;
        eng.streamDone = false;
        eng.rawText = p.currentStream;
        eng.segments = splitStageSentences(p.currentStream, { isComplete: false });
        eng.sentenceIdx = -1;
        eng.sentencePlain = '';
        eng.revealPos = 0;
        eng.lastCharIdx = -1;
        eng.holdUntil = 0;
        eng.doneHoldUntil = 0;
        eng.paused = false;
        eng.revealAllHistory = false;
        setIdleBeatOffset(null);
        emitReaderSync({ phase: 'turn_start', turnId: eng.turnId, speed: p.readerSpeed });
      }

      if (eng.mode === 'playing') {
        let textChanged = false;
        if (p.currentStream != null) {
          if (p.currentStream !== eng.rawText) {
            eng.rawText = p.currentStream;
            textChanged = true;
          }
        } else {
          const tail = p.messages[p.messages.length - 1];
          const hasNewAssistant =
            p.messages.length > eng.baselineMsgCount &&
            tail &&
            tail.role === 'assistant' &&
            !tail.error;

          if (hasNewAssistant) {
            const finalContent = tail.content || eng.rawText;
            if (finalContent !== eng.rawText || !eng.streamDone) {
              eng.rawText = finalContent;
              eng.streamDone = true;
              textChanged = true;
            }
          } else if (!p.postProcessing) {
            // Turn stopped or errored before producing an assistant reply
            eng.mode = 'idle';
            eng.sentenceIdx = -1;
            emitReaderSync({ phase: 'turn_end', turnId: eng.turnId, reason: 'stopped' });
            pushView(eng);
          }
        }

        if (eng.mode === 'playing') {
          if (textChanged) {
            eng.segments = splitStageSentences(eng.rawText, { isComplete: eng.streamDone });
          }

          if (eng.segments[0]?.complete && !eng.firstCompleteAt) {
            eng.firstCompleteAt = now;
          }

          // Initial head-start buffer (~550ms after Sentence 0 finishes streaming)
          // so downstream listeners (e.g. JEV) have Sentence 0 ready before reveal.
          if (eng.sentenceIdx < 0) {
            const headStartReady =
              (eng.segments[0]?.complete && now - eng.firstCompleteAt >= 550) ||
              (eng.streamDone && eng.segments.length > 0 && now - eng.startedAt >= 450) ||
              (eng.segments[0]?.complete && now - eng.startedAt >= 1100);

            if (headStartReady) {
              enterSentence(eng, 0, now, p.readerSpeed);
            } else if (eng.streamDone && eng.segments.length === 0) {
              eng.mode = 'idle';
              emitReaderSync({ phase: 'turn_end', turnId: eng.turnId, reason: 'completed' });
            }
          } else {
            const speed = Math.max(15, p.readerSpeed || 38);
            const pauseScale = Math.min(1.75, Math.max(0.45, 38 / speed));
            const total = eng.sentencePlain.length;

            if (!eng.paused && now >= eng.holdUntil) {
              if (eng.revealPos < total) {
                eng.revealPos = Math.min(total, eng.revealPos + dt * speed);
                const crossed = Math.floor(eng.revealPos) - 1;
                while (eng.lastCharIdx < crossed) {
                  eng.lastCharIdx += 1;
                  const ch = eng.sentencePlain[eng.lastCharIdx];
                  const nextCh = eng.sentencePlain[eng.lastCharIdx + 1] || '';
                  if ((ch === ',' || ch === ';' || ch === ':') && /\s/.test(nextCh)) {
                    eng.holdUntil = Math.max(eng.holdUntil, now + 155 * pauseScale);
                  } else if (ch === '—' || ch === '…') {
                    eng.holdUntil = Math.max(eng.holdUntil, now + 220 * pauseScale);
                  } else if ((ch === '.' || ch === '!' || ch === '?') && /\s/.test(nextCh)) {
                    eng.holdUntil = Math.max(eng.holdUntil, now + 300 * pauseScale);
                  }
                }
                if (eng.revealPos >= total && !eng.doneHoldUntil) {
                  const holdMs = Math.min(1350, Math.max(520, 410 + total * 3.2)) * pauseScale;
                  eng.doneHoldUntil = now + holdMs;
                }
              } else if (now >= eng.doneHoldUntil) {
                const nextIdx = eng.sentenceIdx + 1;
                if (nextIdx < eng.segments.length) {
                  if (eng.segments[nextIdx].complete) {
                    enterSentence(eng, nextIdx, now, speed);
                  }
                } else if (eng.streamDone) {
                  eng.mode = 'idle';
                  setIdleBeatOffset(null);
                  emitReaderSync({ phase: 'turn_end', turnId: eng.turnId, reason: 'completed' });
                }
              }
            }
          }
          pushView(eng);
        }
      }

      rafId = requestAnimationFrame(step);
    };

    rafId = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(rafId);
    };
  }, []);

  // Derive idle stage content (most recent assistant message's beats) when not actively playing
  const lastAssistantInfo = useMemo(() => findLastAssistantInfo(messages), [messages]);
  const lastUserPrompt = useMemo(() => findLastUserPrompt(messages), [messages]);

  const idleBeats = lastAssistantInfo?.beats || [];
  const resolvedIdleIdx = idleBeats.length
    ? Math.min(idleBeats.length - 1, Math.max(0, idleBeatOffset ?? idleBeats.length - 1))
    : 0;

  const animatePartSwap = useCallback((dir) => {
    const el = cardRef.current;
    if (!el || typeof el.animate !== 'function') return;
    const fromX = dir > 0 ? 28 : -28;
    el.animate(
      [
        { opacity: 0.45, transform: `translate3d(${fromX}px, 0, 0)` },
        { opacity: 1, transform: 'translate3d(0, 0, 0)' },
      ],
      { duration: 185, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' }
    );
  }, []);

  // Interactive transport controls
  const togglePause = useCallback(() => {
    const eng = engineRef.current;
    if (eng.mode !== 'playing') return;
    eng.paused = !eng.paused;
    emitReaderSync({ phase: eng.paused ? 'pause' : 'resume', turnId: eng.turnId, paused: eng.paused });
    setView((v) => ({ ...v, paused: eng.paused }));
  }, []);

  const stepPrevBeat = useCallback(() => {
    const eng = engineRef.current;
    if (eng.mode === 'playing') {
      if (eng.sentenceIdx > 0) {
        const prevIdx = eng.sentenceIdx - 1;
        const seg = eng.segments[prevIdx];
        const parsed = parseSentenceDom(seg.text);
        eng.sentenceIdx = prevIdx;
        eng.sentencePlain = parsed.plainText;
        eng.revealPos = parsed.plainText.length;
        eng.doneHoldUntil = performance.now() + 1800;
        emitReaderSync({
          phase: 'sentence',
          turnId: eng.turnId,
          sentenceIndex: prevIdx,
          totalSentences: eng.segments.length,
          charStart: seg.charStart,
          charEnd: seg.charEnd,
          text: seg.text,
          isStreamComplete: eng.streamDone,
          speed: propsRef.current.readerSpeed,
          durationMs: 1800,
        });
        animatePartSwap(-1);
        return true;
      }
      return false;
    }
    if (idleBeats.length > 1) {
      const curIdx = Math.min(idleBeats.length - 1, Math.max(0, idleBeatOffset ?? idleBeats.length - 1));
      if (curIdx > 0) {
        setIdleBeatOffset(curIdx - 1);
        animatePartSwap(-1);
        return true;
      }
    }
    return false;
  }, [idleBeats.length, idleBeatOffset, animatePartSwap]);

  const advanceToNextSentence = useCallback(
    (eng) => {
      const nextIdx = eng.sentenceIdx + 1;
      if (nextIdx < eng.segments.length && eng.segments[nextIdx].complete) {
        const seg = eng.segments[nextIdx];
        const parsed = parseSentenceDom(seg.text);
        eng.sentenceIdx = nextIdx;
        eng.sentencePlain = parsed.plainText;
        eng.revealPos = 0;
        eng.lastCharIdx = -1;
        eng.holdUntil = 0;
        eng.doneHoldUntil = 0;
        emitReaderSync({
          phase: 'sentence',
          turnId: eng.turnId,
          sentenceIndex: nextIdx,
          totalSentences: eng.segments.length,
          charStart: seg.charStart,
          charEnd: seg.charEnd,
          text: seg.text,
          isStreamComplete: eng.streamDone,
          speed: propsRef.current.readerSpeed,
          durationMs: estimateSentenceDurationMs(parsed.plainText, propsRef.current.readerSpeed),
        });
        animatePartSwap(1);
        return true;
      }
      if (eng.streamDone && nextIdx >= eng.segments.length) {
        eng.mode = 'idle';
        setIdleBeatOffset(null);
        emitReaderSync({ phase: 'turn_end', turnId: eng.turnId, reason: 'completed' });
        return true;
      }
      return false;
    },
    [animatePartSwap]
  );

  const stepNextBeat = useCallback(() => {
    const eng = engineRef.current;
    const now = performance.now();
    if (eng.mode === 'playing') {
      // First tap finishes revealing the current sentence; second tap jumps to next sentence
      if (eng.revealPos < eng.sentencePlain.length) {
        eng.revealPos = eng.sentencePlain.length;
        eng.doneHoldUntil = now + 650;
        return true;
      }
      return advanceToNextSentence(eng);
    }
    if (idleBeats.length > 1) {
      const curIdx = Math.min(idleBeats.length - 1, Math.max(0, idleBeatOffset ?? idleBeats.length - 1));
      if (curIdx < idleBeats.length - 1) {
        setIdleBeatOffset(curIdx + 1);
        animatePartSwap(1);
        return true;
      }
    }
    return false;
  }, [idleBeats.length, idleBeatOffset, advanceToNextSentence, animatePartSwap]);

  // Explicit horizontal swipe left swaps directly to the next part even if the
  // current sentence is still mid-reveal (falling back to completing reveal if
  // the next streamed sentence hasn't arrived yet).
  const swipeNextPart = useCallback(() => {
    const eng = engineRef.current;
    const now = performance.now();
    if (eng.mode === 'playing') {
      if (advanceToNextSentence(eng)) {
        return true;
      }
      if (eng.revealPos < eng.sentencePlain.length) {
        eng.revealPos = eng.sentencePlain.length;
        eng.doneHoldUntil = now + 650;
        return true;
      }
      return false;
    }
    if (idleBeats.length > 1) {
      const curIdx = Math.min(idleBeats.length - 1, Math.max(0, idleBeatOffset ?? idleBeats.length - 1));
      if (curIdx < idleBeats.length - 1) {
        setIdleBeatOffset(curIdx + 1);
        animatePartSwap(1);
        return true;
      }
    }
    return false;
  }, [idleBeats.length, idleBeatOffset, advanceToNextSentence, animatePartSwap]);

  const skipToEnd = useCallback(() => {
    const eng = engineRef.current;
    if (eng.mode !== 'playing') return;
    if (eng.streamDone) {
      const lastSeg = eng.segments[eng.segments.length - 1];
      if (lastSeg) {
        emitReaderSync({
          phase: 'sentence',
          turnId: eng.turnId,
          sentenceIndex: eng.segments.length - 1,
          totalSentences: eng.segments.length,
          charStart: lastSeg.charStart,
          charEnd: lastSeg.charEnd,
          text: lastSeg.text,
          isStreamComplete: true,
          speed: propsRef.current.readerSpeed,
        });
      }
      eng.mode = 'idle';
      setIdleBeatOffset(null);
      emitReaderSync({ phase: 'turn_end', turnId: eng.turnId, reason: 'skipped' });
    } else if (eng.segments.length > 0) {
      // Stream still arriving: jump to the newest complete sentence so far
      let target = eng.segments.length - 1;
      while (target > 0 && !eng.segments[target].complete) target -= 1;
      const seg = eng.segments[target];
      if (seg) {
        const parsed = parseSentenceDom(seg.text);
        eng.sentenceIdx = target;
        eng.sentencePlain = parsed.plainText;
        eng.revealPos = parsed.plainText.length;
        eng.doneHoldUntil = performance.now() + 300;
      }
    }
  }, []);

  const revealFullInHistory = useCallback(() => {
    engineRef.current.revealAllHistory = true;
    setView((v) => ({ ...v, revealAllHistory: true }));
  }, []);

  // Build anti-spoiler message list for History overlay if playback is still in progress
  const historyMessages = useMemo(() => {
    if (!view.playing || view.revealAllHistory) return messages;
    if (currentStream != null) return messages;
    // Stream finished (`messages` already contains the full turn at tail), so clip
    // the last assistant message to what the Stage has revealed so far.
    if (!messages.length) return messages;
    const last = messages[messages.length - 1];
    if (last.role !== 'assistant') return messages;
    const clipped = (last.content || '').slice(0, view.revealedCharEnd || 0);
    if (!clipped) return messages.slice(0, -1);
    return [...messages.slice(0, -1), { ...last, content: clipped }];
  }, [messages, view.playing, view.revealAllHistory, view.revealedCharEnd, currentStream]);

  const historyStream = useMemo(() => {
    if (!view.playing || currentStream == null) return null;
    if (view.revealAllHistory) return currentStream;
    return currentStream.slice(0, view.revealedCharEnd || 0);
  }, [view.playing, view.revealAllHistory, view.revealedCharEnd, currentStream]);

  const approxWpm = Math.round((readerSpeed * 60) / 5.2);
  const activeBeatIdx = view.playing ? view.sentenceIdx : resolvedIdleIdx;
  const totalBeats = view.playing ? view.totalSentences : idleBeats.length;
  const displayedText = view.playing ? view.sentenceText : (idleBeats[resolvedIdleIdx]?.text || '');
  const isErrorCard = !view.playing && lastAssistantInfo?.message?.error;

  const canPrev = activeBeatIdx > 0;
  const canNext = view.playing
    ? view.revealPos < view.totalChars || activeBeatIdx + 1 < totalBeats || Boolean(view.sentenceText)
    : activeBeatIdx + 1 < totalBeats;

  const handleCardClick = useCallback(() => {
    if (!view.playing) return;
    if (performance.now() - lastSwipeAtRef.current < 420) return;
    if (typeof window !== 'undefined' && window.getSelection?.()?.toString()) return;
    stepNextBeat();
  }, [view.playing, stepNextBeat]);

  const handleTouchStart = useCallback((e) => {
    if (e.touches.length !== 1) {
      touchRef.current = null;
      return;
    }
    const target = e.target;
    if (
      target?.closest?.(
        'button, a, input, textarea, select, summary, [role="button"], [role="slider"]'
      )
    ) {
      touchRef.current = null;
      return;
    }
    const t = e.touches[0];
    touchRef.current = {
      startX: t.clientX,
      startY: t.clientY,
      startTime: performance.now(),
      axis: null,
    };
  }, []);

  const handleTouchMove = useCallback(
    (e) => {
      const state = touchRef.current;
      if (!state || e.touches.length !== 1) return;
      const t = e.touches[0];
      const dx = t.clientX - state.startX;
      const dy = t.clientY - state.startY;

      if (!state.axis) {
        if (Math.abs(dx) < 10 && Math.abs(dy) < 10) return;
        if (Math.abs(dy) > Math.abs(dx) * 1.15) {
          state.axis = 'y';
          return;
        }
        if (Math.abs(dx) > Math.abs(dy) * 1.25) {
          state.axis = 'x';
        } else {
          return;
        }
      }

      if (state.axis !== 'x') return;

      const hasTarget = dx < 0 ? canNext : canPrev;
      const damped = hasTarget
        ? Math.max(-88, Math.min(88, dx * 0.45))
        : Math.max(-22, Math.min(22, dx * 0.14));
      setDragX(damped);
    },
    [canNext, canPrev]
  );

  const handleTouchEnd = useCallback(
    (e) => {
      const state = touchRef.current;
      touchRef.current = null;
      setDragX(0);
      if (!state || state.axis === 'y') return;

      const t = e.changedTouches?.[0];
      if (!t) return;
      const dx = t.clientX - state.startX;
      const dy = t.clientY - state.startY;
      const elapsed = Math.max(1, performance.now() - state.startTime);

      const isHorizontal = Math.abs(dx) > Math.abs(dy) * 1.45;
      const isLongEnough = Math.abs(dx) >= 48 || (Math.abs(dx) >= 30 && elapsed < 260);
      if (!isHorizontal || !isLongEnough) return;

      lastSwipeAtRef.current = performance.now();
      if (dx < 0) {
        swipeNextPart();
      } else {
        stepPrevBeat();
      }
    },
    [swipeNextPart, stepPrevBeat]
  );

  const handleTouchCancel = useCallback(() => {
    touchRef.current = null;
    setDragX(0);
  }, []);

  return (
    <div className="relative flex-1 min-h-0 flex flex-col bg-gray-900 select-text">
      {/* Subtle top stage toolbar: last player cue + History button */}
      <div className="shrink-0 max-w-3xl w-full mx-auto px-4 sm:px-8 pt-3 pb-1 flex items-center justify-between gap-3">
        <div className="min-w-0 flex items-center gap-2 text-xs text-gray-500 truncate">
          {(currentStream != null || postProcessing) && (
            <span className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full bg-purple-500/10 border border-purple-500/30 text-purple-300 shrink-0">
              <span className="w-1.5 h-1.5 rounded-full bg-purple-400 animate-pulse" />
              <span>{pipelineStatus?.label || 'Streaming ahead…'}</span>
            </span>
          )}
          {lastUserPrompt && (
            <span className="truncate italic text-gray-500" title={lastUserPrompt}>
              You: {lastUserPrompt}
            </span>
          )}
        </div>

        <button
          type="button"
          onClick={onOpenHistory}
          className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-gray-800/90 hover:bg-gray-700 border border-gray-700 text-xs text-gray-200 transition-colors"
          title="Open full story history log"
        >
          <svg className="w-3.5 h-3.5 text-purple-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 6.253v13m0-13C10.832 5.477 9.246 5 7.5 5S4.168 5.477 3 6.253v13C4.168 18.477 5.754 18 7.5 18s3.332.477 4.5 1.253m0-13C13.168 5.477 14.754 5 16.5 5c1.747 0 3.332.477 4.5 1.253v13C19.832 18.477 18.247 18 16.5 18c-1.746 0-3.332.477-4.5 1.253" />
          </svg>
          <span>History</span>
          {messages.length > 0 && (
            <span className="font-mono text-[11px] text-gray-400">({messages.length})</span>
          )}
        </button>
      </div>

      {/* Centered reading stage (swipe left/right on touch to swap between parts) */}
      <div
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={handleTouchEnd}
        onTouchCancel={handleTouchCancel}
        className="flex-1 min-h-0 flex items-center justify-center px-4 sm:px-8 py-4 overflow-y-auto overflow-x-hidden touch-pan-y book-scroll"
      >
        <div className="w-full max-w-2xl">
          {messages.length === 0 && !view.playing && currentStream == null ? (
            <div className="text-center text-gray-500 text-lg py-12">
              Welcome to WorldBox. Start your adventure!
            </div>
          ) : view.waitingBuffer ? (
            <div className="rounded-2xl border border-gray-800 bg-gray-850/70 px-6 py-12 sm:px-10 sm:py-14 text-center shadow-xl">
              <div className="inline-flex items-center gap-2.5 text-sm text-purple-300">
                <span className="w-2 h-2 rounded-full bg-purple-400 animate-ping" />
                <span>{pipelineStatus?.label || 'Preparing scene…'}</span>
              </div>
            </div>
          ) : (
            <div
              ref={cardRef}
              onClick={view.playing ? handleCardClick : undefined}
              style={
                dragX
                  ? { transform: `translate3d(${dragX}px, 0, 0)`, transition: 'none' }
                  : { transition: 'transform 180ms cubic-bezier(0.22, 1, 0.36, 1)' }
              }
              className={`rounded-2xl border px-6 py-8 sm:px-10 sm:py-11 shadow-2xl transition-colors will-change-transform ${
                isErrorCard
                  ? 'border-red-800/60 bg-red-950/30 text-red-200'
                  : 'border-gray-700/70 bg-gray-850/90 text-gray-100'
              } ${view.playing ? 'cursor-pointer' : ''}`}
              title={view.playing ? 'Tap or swipe to advance part' : totalBeats > 1 ? 'Swipe left/right to swap parts' : undefined}
            >
              <div className="text-xl sm:text-2xl leading-relaxed tracking-[0.01em] min-h-[4.5rem] flex items-center">
                <div className="w-full">
                  <RevealedSentence
                    text={displayedText}
                    revealPos={view.playing ? view.revealPos : 1e9}
                    fullyRevealed={!view.playing || view.revealPos >= view.totalChars}
                  />
                </div>
              </div>

              {/* Per-message module footer (e.g. scene illustration) once turn is resting */}
              {!view.playing && lastAssistantInfo && !isErrorCard && modules?.length > 0 && (
                <div className="mt-6 pt-4 border-t border-gray-800/80">
                  <SlotRenderer
                    slotName="slot_message_footer"
                    modules={modules}
                    state={slotState}
                    config={moduleConfigs}
                    slotProps={{ message: lastAssistantInfo.message, messageTurn: currentTurn }}
                    skeleton={false}
                  />
                </div>
              )}

              {/* Subtle beat progress bar */}
              {totalBeats > 1 && (
                <div className="mt-6 flex items-center gap-2">
                  <div className="flex-1 h-1 bg-gray-800 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-purple-500/75 transition-[width] duration-200"
                      style={{ width: `${Math.round(((activeBeatIdx + 1) / Math.max(1, totalBeats)) * 100)}%` }}
                    />
                  </div>
                  <span className="text-[11px] font-mono text-gray-500 tabular-nums">
                    {activeBeatIdx + 1}/{totalBeats}
                  </span>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Bottom stage controls: beat steppers, pause/skip, swipes, and live speed slider */}
      <div className="shrink-0 max-w-3xl w-full mx-auto px-4 sm:px-8 pb-2.5 pt-1 flex flex-wrap items-center justify-between gap-3 text-xs text-gray-400">
        <div className="flex items-center gap-1.5">
          <button
            type="button"
            onClick={stepPrevBeat}
            disabled={!canPrev}
            className="px-2.5 py-1 rounded-md bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 disabled:opacity-35 transition-colors"
            title="Previous sentence"
          >
            ‹ Prev
          </button>

          {view.playing && (
            <button
              type="button"
              onClick={togglePause}
              className={`px-2.5 py-1 rounded-md border transition-colors ${
                view.paused
                  ? 'bg-purple-600/30 border-purple-500/60 text-purple-200'
                  : 'bg-gray-800 hover:bg-gray-700 border-gray-700 text-gray-300'
              }`}
              title={view.paused ? 'Resume auto-reading' : 'Pause auto-reading'}
            >
              {view.paused ? '▶ Resume' : '⏸ Pause'}
            </button>
          )}

          <button
            type="button"
            onClick={stepNextBeat}
            disabled={!canNext}
            className="px-2.5 py-1 rounded-md bg-gray-800 hover:bg-gray-700 border border-gray-700 text-gray-300 disabled:opacity-35 transition-colors"
            title="Next sentence"
          >
            Next ›
          </button>

          {view.playing && (
            <button
              type="button"
              onClick={skipToEnd}
              className="px-2.5 py-1 rounded-md bg-gray-800 hover:bg-gray-700 border border-gray-700 text-purple-300 transition-colors"
              title="Skip ahead to latest sentence"
            >
              ⏭ Skip
            </button>
          )}

          {!view.playing && swipes && swipes.count >= 1 && (
            <div className="ml-1 pl-2 border-l border-gray-700/80">
              <StageSwipes
                swipes={swipes}
                busy={busy}
                onPrev={() => {
                  if ((swipes.active ?? 0) > 0) {
                    setIdleBeatOffset(0);
                    onSwipe(swipes.active - 1);
                  }
                }}
                onNext={() => {
                  if ((swipes.active ?? 0) < (swipes.count ?? 1) - 1) {
                    setIdleBeatOffset(0);
                    onSwipe((swipes.active ?? 0) + 1);
                  } else {
                    onRegenerate();
                  }
                }}
                onRegenerate={onRegenerate}
              />
            </div>
          )}
        </div>

        {/* Live reading speed slider */}
        <div className="flex items-center gap-2 ml-auto">
          <span className="text-[11px] text-gray-500">Speed</span>
          <input
            type="range"
            min="15"
            max="95"
            step="1"
            value={readerSpeed}
            onChange={(e) => onChangeReaderSpeed?.(Number(e.target.value))}
            className="w-24 sm:w-32 accent-purple-500 cursor-pointer"
            aria-label="Reading speed"
            title={`Stage reading speed (~${approxWpm} WPM)`}
          />
          <span className="font-mono text-[11px] text-gray-400 w-16 text-right tabular-nums">
            ~{approxWpm} wpm
          </span>
        </div>
      </div>

      {/* Full Story History Modal / Drawer */}
      {historyOpen && (
        <div className="fixed inset-0 z-50 flex flex-col bg-black/75 backdrop-blur-xs">
          <div className="shrink-0 h-13 px-4 sm:px-6 bg-gray-900 border-b border-gray-700 flex items-center justify-between gap-3">
            <div className="flex items-center gap-3 min-w-0">
              <h2 className="text-sm font-semibold text-gray-100">Story History</h2>
              {view.playing && !view.revealAllHistory && (
                <button
                  type="button"
                  onClick={revealFullInHistory}
                  className="text-xs px-2.5 py-1 rounded bg-purple-900/40 hover:bg-purple-800/50 border border-purple-600/40 text-purple-200 transition-colors"
                >
                  Reveal buffered text
                </button>
              )}
            </div>
            <button
              type="button"
              onClick={onCloseHistory}
              className="px-3 py-1.5 rounded-lg bg-gray-800 hover:bg-gray-700 border border-gray-700 text-xs text-gray-200 transition-colors"
            >
              ✕ Back to Stage
            </button>
          </div>
          <div className="flex-1 min-h-0 flex flex-col bg-gray-900">
            <ChatFeed
              messages={historyMessages}
              currentStream={historyStream}
              currentReasoning={currentReasoning}
              swipes={swipes}
              busy={busy}
              postProcessing={postProcessing}
              pipelineStatus={pipelineStatus}
              editRequest={editRequest}
              currentTurn={currentTurn}
              density={density}
              scrollControlRef={scrollControlRef}
              onUserScroll={onUserScroll}
              onBranchMessage={onBranchMessage}
              onRegenerate={onRegenerate}
              onSwipe={onSwipe}
              onEditMessage={onEditMessage}
              onDeleteMessage={onDeleteMessage}
              modules={modules}
              slotState={slotState}
              moduleConfigs={moduleConfigs}
            />
          </div>
        </div>
      )}
    </div>
  );
}

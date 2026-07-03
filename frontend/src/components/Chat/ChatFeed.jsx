import { useRef, useEffect, useCallback, useState } from 'react';
import MarkdownRenderer from '../shared/MarkdownRenderer';
import { useStickToBottom } from '../../hooks/useStickToBottom';

const MESSAGE_STYLES = {
  assistant: { bg: 'bg-gray-850/80', text: 'text-gray-200' },
  ai:        { bg: 'bg-gray-850/80', text: 'text-gray-200' },
  user:      { bg: 'bg-gray-800/80', text: 'text-gray-200' },
  system:    { bg: 'bg-red-950/40',  text: 'text-red-200' },
};

// Collapsed-by-default disclosure for the model's chain-of-thought.
function ThoughtProcess({ reasoning, live }) {
  const [open, setOpen] = useState(false);
  // While thinking streams, keep the box pinned to the latest line unless the
  // user scrolls up. Only stick when live+open so expanding a finished thought
  // starts at the top for reading.
  const think = useStickToBottom([reasoning, open], { enabled: live && open });
  if (!reasoning) return null;
  return (
    <div className="mb-3 border border-gray-700/60 rounded-lg bg-black/20">
      <button
        onClick={() => setOpen(v => !v)}
        className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-xs text-gray-400 hover:text-gray-200 transition-colors"
      >
        <span>{live ? '🧠 Thinking…' : '🧠 Thought process'}</span>
        {live && <span className="w-1.5 h-1.5 rounded-full bg-purple-400 animate-pulse" />}
        <svg className={`w-3 h-3 ml-auto transition-transform ${open ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>
      {open && (
        <pre
          ref={think.ref}
          onScroll={think.onScroll}
          className="px-3 pb-3 pt-0 text-xs text-gray-400 whitespace-pre-wrap break-words font-mono leading-relaxed max-h-64 overflow-y-auto book-scroll"
        >
          {reasoning}
        </pre>
      )}
    </div>
  );
}

const ACTION_BTN =
  'p-1.5 rounded text-gray-500 hover:text-gray-200 hover:bg-white/10 disabled:opacity-30 transition-colors';

// Per-message icon action bar: Copy / Branch / Edit / Delete (with inline
// confirm). Hidden until hover on desktop; always visible on touch
// (see .msg-actions CSS).
function MessageActions({ content, onEdit, onDelete, onBranch, disabled }) {
  const [copied, setCopied] = useState(false);
  const [confirming, setConfirming] = useState(false);

  const copy = () => {
    navigator.clipboard.writeText(content).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    });
  };

  return (
    <div className="relative flex items-center gap-0.5 bg-gray-900/80 border border-gray-700/60 rounded-lg px-1 py-0.5">
      <button onClick={copy} disabled={disabled} className={ACTION_BTN} aria-label="Copy message" title="Copy raw text">
        {copied ? (
          <svg className="w-4 h-4 text-green-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
          </svg>
        ) : (
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z" />
          </svg>
        )}
      </button>
      {onBranch && (
        <button onClick={onBranch} disabled={disabled} className={ACTION_BTN} aria-label="Branch from here" title="Branch the story from this point">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <circle cx="6" cy="5" r="2.2" strokeWidth={2} />
            <circle cx="6" cy="19" r="2.2" strokeWidth={2} />
            <circle cx="18" cy="12" r="2.2" strokeWidth={2} />
            <path strokeLinecap="round" strokeWidth={2} d="M6 7.2v9.6M6 12c0-2.5 4-2.5 9.8-.2" />
          </svg>
        </button>
      )}
      <button onClick={onEdit} disabled={disabled} className={ACTION_BTN} aria-label="Edit message" title="Edit">
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
        </svg>
      </button>
      <button
        onClick={() => setConfirming(true)}
        disabled={disabled}
        className={`${ACTION_BTN} hover:text-red-300`}
        aria-label="Delete message"
        title="Delete"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
        </svg>
      </button>
      {confirming && (
        <>
          <div className="fixed inset-0 z-10" onClick={() => setConfirming(false)} />
          <div className="absolute right-0 top-full mt-1 z-20 w-40 bg-gray-800 border border-gray-700 rounded-lg shadow-xl px-3 py-2 space-y-2">
            <div className="text-xs text-gray-400">Delete this message?</div>
            <div className="flex gap-2">
              <button
                onClick={() => { setConfirming(false); onDelete(); }}
                className="flex-1 px-2 py-1 text-xs rounded bg-red-600 hover:bg-red-500 text-white"
              >
                Delete
              </button>
              <button onClick={() => setConfirming(false)} className="flex-1 px-2 py-1 text-xs rounded bg-gray-700 hover:bg-gray-600">
                Cancel
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}

// Subtle metadata footer: turn number, model, token usage, timestamp.
// All parts are optional — old saves have no meta at all.
function MessageMeta({ meta, turn }) {
  const parts = [];
  if (turn != null) parts.push(`Turn ${turn}`);
  if (meta?.model) parts.push(meta.model.split('/').pop());
  if (meta?.tokens) {
    const t = meta.tokens;
    parts.push([t.in != null ? `${t.in}↑` : null, t.out != null ? `${t.out}↓` : null].filter(Boolean).join(' '));
  }
  if (meta?.ts) {
    const d = new Date(meta.ts);
    if (!isNaN(d)) parts.push(d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }));
  }
  if (!parts.length) return null;
  return <div className="text-xs text-gray-600">{parts.join(' · ')}</div>;
}

// ‹ i/n › + regenerate on the last AI message. Swiping past the end regenerates.
function SwipeControls({ swipes, busy, onPrev, onNext, onRegenerate }) {
  const active = swipes?.active ?? 0;
  const count = swipes?.count ?? 1;
  const atStart = active <= 0;
  return (
    <div className="flex items-center gap-1 text-gray-400">
      <button
        onClick={onPrev}
        disabled={busy || atStart}
        className="p-1 rounded hover:text-gray-100 hover:bg-white/10 disabled:opacity-30 transition-colors"
        aria-label="Previous generation"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
        </svg>
      </button>
      <span className="text-xs tabular-nums w-8 text-center">{active + 1}/{count}</span>
      <button
        onClick={onNext}
        disabled={busy}
        className="p-1 rounded hover:text-gray-100 hover:bg-white/10 disabled:opacity-30 transition-colors"
        aria-label="Next generation"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5l7 7-7 7" />
        </svg>
      </button>
      <button
        onClick={onRegenerate}
        disabled={busy}
        className="p-1 ml-1 rounded hover:text-gray-100 hover:bg-white/10 disabled:opacity-30 transition-colors"
        aria-label="Regenerate"
        title="Regenerate"
      >
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
      </button>
    </div>
  );
}

function MessageBlock({ message, index, isLastAssistant, swipes, busy, editRequest, branchTurn, onBranchMessage, onRegenerate, onSwipe, onEditMessage, onDeleteMessage }) {
  const isUser = message.role === 'user';
  const isSystem = message.role === 'system' || message.error;
  const styleKey = isSystem ? 'system' : isUser ? 'user' : 'assistant';
  const style = MESSAGE_STYLES[styleKey] || MESSAGE_STYLES.assistant;

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(message.content);
  const touchStartX = useRef(null);

  // External edit request (e.g. ArrowUp in the empty composer targets the last
  // user message). `at` distinguishes repeated requests for the same index.
  useEffect(() => {
    if (editRequest && editRequest.index === index && !isSystem) {
      setDraft(message.content);
      setEditing(true);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editRequest]);

  const active = swipes?.active ?? 0;
  const count = swipes?.count ?? 1;
  const showSwipes = isLastAssistant && !isSystem && !editing && swipes && swipes.count >= 1;

  const goPrev = useCallback(() => { if (active > 0) onSwipe(active - 1); }, [active, onSwipe]);
  const goNext = useCallback(() => {
    if (active < count - 1) onSwipe(active + 1);
    else onRegenerate();
  }, [active, count, onSwipe, onRegenerate]);

  const onTouchEnd = (e) => {
    if (touchStartX.current == null || busy) { touchStartX.current = null; return; }
    const dx = e.changedTouches[0].clientX - touchStartX.current;
    touchStartX.current = null;
    if (Math.abs(dx) < 50) return;
    if (dx < 0) goNext(); else goPrev();
  };

  const saveEdit = () => { setEditing(false); if (draft !== message.content) onEditMessage(index, draft); };
  const cancelEdit = () => { setEditing(false); setDraft(message.content); };

  return (
    <div
      className={`group ${style.bg} ${style.text}`}
      onTouchStart={showSwipes ? (e) => { touchStartX.current = e.touches[0].clientX; } : undefined}
      onTouchEnd={showSwipes ? onTouchEnd : undefined}
    >
      <div className="max-w-[720px] mx-auto px-8 py-6 relative">
        {/* Action bar — hidden until hover on desktop, always visible on touch */}
        {!isSystem && (
          <div className="msg-actions absolute top-3 right-3 opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity">
            <MessageActions
              content={message.content}
              disabled={busy || editing}
              onEdit={() => { setDraft(message.content); setEditing(true); }}
              onDelete={() => onDeleteMessage(index)}
              onBranch={branchTurn != null && onBranchMessage ? () => onBranchMessage(branchTurn) : null}
            />
          </div>
        )}

        {message.reasoning && !isUser && <ThoughtProcess reasoning={message.reasoning} />}

        {editing ? (
          <div className="space-y-2">
            <textarea
              value={draft}
              autoFocus
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Escape') { e.preventDefault(); cancelEdit(); }
                else if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); saveEdit(); }
              }}
              rows={Math.min(16, Math.max(3, draft.split('\n').length + 1))}
              className="w-full bg-gray-900 border border-gray-700 rounded p-3 text-gray-200 text-base focus:border-purple-500 focus:outline-none"
            />
            <div className="flex gap-2 justify-end items-center">
              <span className="text-xs text-gray-600 mr-auto">Esc to cancel · Ctrl+Enter to save</span>
              <button onClick={cancelEdit} className="px-3 py-1.5 text-sm rounded bg-gray-700 hover:bg-gray-600">Cancel</button>
              <button onClick={saveEdit} className="px-3 py-1.5 text-sm rounded bg-purple-600 hover:bg-purple-500 text-white">Save</button>
            </div>
          </div>
        ) : isUser ? (
          <p className="text-lg leading-relaxed whitespace-pre-wrap">{message.content}</p>
        ) : isSystem ? (
          <p className="whitespace-pre-wrap text-sm">{message.content}</p>
        ) : (
          <div
            // Re-key on the active swipe so switching variants crossfades
            // instead of snapping.
            key={showSwipes ? `swipe-${swipes.active}` : undefined}
            className={`text-lg leading-relaxed${showSwipes ? ' swipe-fade' : ''}`}
          >
            <MarkdownRenderer content={message.content} />
          </div>
        )}

        {/* Footer: swipe controls + turn/model/tokens/time */}
        {(showSwipes || message.turn != null || message.meta) && !editing && (
          <div className="flex items-center justify-between mt-6">
            <div>{showSwipes && (
              <SwipeControls swipes={swipes} busy={busy} onPrev={goPrev} onNext={goNext} onRegenerate={onRegenerate} />
            )}</div>
            <MessageMeta meta={message.meta} turn={message.turn} />
          </div>
        )}
      </div>
    </div>
  );
}

function StreamingBlock({ content, reasoning, status }) {
  if (content == null && !reasoning) return null;

  return (
    <div className="bg-gray-850/80 text-gray-200">
      <div className="max-w-[720px] mx-auto px-8 py-6">
        {reasoning && <ThoughtProcess reasoning={reasoning} live />}
        <div className="flex items-center gap-2 mb-3">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-purple-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-purple-500" />
          </span>
          <span className="text-xs text-gray-500 animate-pulse">{status?.label || 'AI is writing…'}</span>
        </div>
        <div className="text-lg leading-relaxed">
          <MarkdownRenderer content={content || ''} streaming />
        </div>
      </div>
    </div>
  );
}

// Shown after the narration finishes but before the turn resolves, while the
// reader/librarian run server-side. Previously this window was a silent
// blocked-input dead zone.
function PostProcessingLine({ status }) {
  return (
    <div className="max-w-[720px] mx-auto px-8 py-3 flex items-center gap-2">
      <span className="relative flex h-2 w-2">
        <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-purple-400 opacity-75" />
        <span className="relative inline-flex rounded-full h-2 w-2 bg-purple-500" />
      </span>
      <span className="text-xs text-gray-500 animate-pulse">{status?.label || 'Finishing up…'}</span>
    </div>
  );
}

export function ChatFeed({ messages, currentStream, currentReasoning, swipes, busy, postProcessing, pipelineStatus, editRequest, currentTurn, density = 'comfortable', scrollControlRef, onUserScroll, onBranchMessage, onRegenerate, onSwipe, onEditMessage, onDeleteMessage }) {
  // Auto-scroll the feed as messages/tokens grow, but only while the user is at
  // the bottom; scrolling up cancels it until they return to the bottom.
  const feed = useStickToBottom([messages, currentStream, currentReasoning], { onUserScroll });

  // Expose scroll controls to the parent (e.g. focusing the composer on mobile
  // scrolls the feed to the bottom).
  useEffect(() => {
    if (scrollControlRef) scrollControlRef.current = { pin: feed.pin, scrollToBottom: feed.scrollToBottom };
  });

  // Re-pin to the bottom whenever a fresh stream starts, so each new turn
  // follows along even if the user had scrolled up during the previous one.
  const wasStreaming = useRef(false);
  useEffect(() => {
    const streaming = currentStream != null;
    if (streaming && !wasStreaming.current) feed.pin();
    wasStreaming.current = streaming;
  }, [currentStream, feed]);

  // While the user is scrolled up, note content arriving below so the
  // scroll-to-bottom pill can flag it; clears when they return to the bottom.
  const [hasNew, setHasNew] = useState(false);
  useEffect(() => {
    if (!feed.pinned) setHasNew(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, currentStream]);
  useEffect(() => {
    if (feed.pinned) setHasNew(false);
  }, [feed.pinned]);

  const isEmpty = messages.length === 0 && currentStream == null;

  // The last assistant message is the swipeable/regeneratable one.
  let lastAssistantIdx = -1;
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i].role === 'assistant') { lastAssistantIdx = i; break; }
  }
  const streaming = currentStream != null;

  // Map each AI message to the turn it ended, anchored at the end: the last AI
  // message is `currentTurn`, walking backwards one turn per AI message. Older
  // messages resolve to negative turns after transcript edits — those (and
  // anything without a known current turn) simply don't offer branching.
  const branchTurns = new Array(messages.length).fill(null);
  if (currentTurn != null && onBranchMessage) {
    let turn = currentTurn;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant' && !messages[i].error) {
        branchTurns[i] = turn >= 0 ? turn : null;
        turn -= 1;
      }
    }
  }

  return (
    <div className={`relative flex-1 min-h-0${density === 'compact' ? ' chat-compact' : ''}`}>
      <div
        ref={feed.ref}
        onScroll={feed.onScroll}
        className="h-full overflow-y-auto book-scroll"
        role="log"
        aria-live="polite"
      >
        {isEmpty && (
          <div className="flex items-center justify-center h-full">
            <p className="text-gray-600 text-lg">
              Welcome to WorldBox. Start your adventure!
            </p>
          </div>
        )}

        {messages.map((msg, idx) => (
          <MessageBlock
            key={idx}
            message={msg}
            index={idx}
            isLastAssistant={idx === lastAssistantIdx && !streaming}
            swipes={swipes}
            busy={busy}
            editRequest={editRequest}
            branchTurn={branchTurns[idx]}
            onBranchMessage={onBranchMessage}
            onRegenerate={onRegenerate}
            onSwipe={onSwipe}
            onEditMessage={onEditMessage}
            onDeleteMessage={onDeleteMessage}
          />
        ))}

        <StreamingBlock content={currentStream} reasoning={currentReasoning} status={pipelineStatus} />

        {postProcessing && !streaming && <PostProcessingLine status={pipelineStatus} />}
      </div>

      {!feed.pinned && !isEmpty && (
        <button
          onClick={feed.scrollToBottom}
          className="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-2 px-3 py-1.5 rounded-full bg-gray-800/95 border border-gray-600 shadow-lg text-xs text-gray-300 hover:text-gray-100 hover:border-gray-500 transition-colors"
          aria-label="Scroll to bottom"
        >
          {hasNew && <span className="w-2 h-2 rounded-full bg-purple-400 animate-pulse" aria-label="New content below" />}
          {hasNew ? 'New content' : 'Scroll to bottom'}
          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 14l-7 7m0 0l-7-7m7 7V3" />
          </svg>
        </button>
      )}
    </div>
  );
}

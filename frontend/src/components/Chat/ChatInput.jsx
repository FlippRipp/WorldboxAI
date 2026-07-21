import { useState, useCallback, useEffect, useRef, useLayoutEffect } from 'react';
import { storage } from '../../lib/storage';

// Grow with content up to this cap, then scroll internally. Matches max-h-32.
const MAX_HEIGHT = 128;

// Unsent composer text, mirrored to localStorage so it survives Android
// killing the backgrounded PWA (relaunching is a full reload).
const DRAFT_KEY = 'wb_draft';

// The composer is still "typing a command name" while the text is a single
// token that starts with "/" (no space yet). Once a space is typed the player
// has moved on to arguments and the menu closes.
const commandQuery = (text) => (/^\/\S*$/.test(text) ? text.toLowerCase() : null);

export default function ChatInput({ commands = [], onSend, onContinue, onStop, onEditLast, onSwipePrev, onSwipeNext, onComposerFocus, restoredInput, busy, disabled }) {
  const [inputValue, setInputValue] = useState(() => storage.getItem(DRAFT_KEY) || '');
  const [dismissed, setDismissed] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);
  const taRef = useRef(null);
  const isEmpty = !inputValue.trim();
  const blocked = disabled || busy;

  const query = commandQuery(inputValue);
  const suggestions = query != null
    ? commands.filter((c) => c.command.toLowerCase().startsWith(query))
    : [];
  const menuOpen = !disabled && !dismissed && suggestions.length > 0;

  // Reset highlight (and any prior dismissal) whenever the query changes, so a
  // fresh set of matches always starts at the top and re-shows the menu.
  useEffect(() => {
    setActiveIndex(0);
    setDismissed(false);
  }, [query]);

  // When a turn is stopped, the server echoes the discarded input back so the
  // player can tweak and resend it instead of retyping.
  useEffect(() => {
    if (restoredInput?.text) setInputValue(restoredInput.text);
  }, [restoredInput]);

  // Keep the draft mirror current; sending clears the value, which removes it.
  useEffect(() => {
    if (inputValue) storage.setItem(DRAFT_KEY, inputValue);
    else storage.removeItem(DRAFT_KEY);
  }, [inputValue]);

  // Auto-grow: follow the content height. Keyed on the value so it covers
  // typing, restored input, and the reset to one row after send.
  useLayoutEffect(() => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, MAX_HEIGHT)}px`;
  }, [inputValue]);

  const handleSend = useCallback(() => {
    if (blocked) return;
    if (isEmpty) {
      onContinue?.();
      return;
    }
    onSend(inputValue);
    setInputValue('');
  }, [inputValue, isEmpty, blocked, onSend, onContinue]);

  const applyCommand = useCallback((cmd) => {
    setInputValue(`${cmd} `);
    setDismissed(true);
    taRef.current?.focus();
  }, []);

  const handleKeyDown = useCallback((e) => {
    if (e.isComposing || e.keyCode === 229) return;
    // While the command menu is open, arrows/Enter/Tab/Escape drive it and take
    // precedence over the composer's own arrow shortcuts.
    if (menuOpen) {
      if (e.key === 'ArrowDown') {
        e.preventDefault();
        setActiveIndex((i) => (i + 1) % suggestions.length);
        return;
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault();
        setActiveIndex((i) => (i - 1 + suggestions.length) % suggestions.length);
        return;
      }
      if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        applyCommand(suggestions[activeIndex].command);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        setDismissed(true);
        return;
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    } else if (e.key === 'ArrowUp' && isEmpty && !blocked) {
      e.preventDefault();
      onEditLast?.();
    } else if (e.key === 'ArrowLeft' && isEmpty && !blocked) {
      e.preventDefault();
      onSwipePrev?.();
    } else if (e.key === 'ArrowRight' && isEmpty && !blocked) {
      e.preventDefault();
      onSwipeNext?.();
    }
  }, [menuOpen, suggestions, activeIndex, applyCommand, handleSend, isEmpty, blocked, onEditLast, onSwipePrev, onSwipeNext]);

  return (
    <div className="p-4 border-t border-gray-700 bg-gray-800">
      <div className="max-w-[720px] mx-auto relative">
        {menuOpen && (
          <ul className="absolute bottom-full mb-2 left-0 right-0 z-10 max-h-64 overflow-y-auto bg-gray-900 border border-gray-600 rounded-xl shadow-lg py-1">
            {suggestions.map((s, i) => (
              <li key={s.command}>
                <button
                  type="button"
                  // Keep textarea focus so typing continues after a click.
                  onMouseDown={(e) => { e.preventDefault(); applyCommand(s.command); }}
                  onMouseEnter={() => setActiveIndex(i)}
                  className={`w-full flex items-center gap-3 px-4 py-2 text-left ${i === activeIndex ? 'bg-purple-600/30' : ''}`}
                >
                  <span className="w-5 text-center shrink-0">{s.icon || '/'}</span>
                  <span className="font-mono text-purple-300 shrink-0">{s.command}</span>
                  <span className="text-gray-400 text-sm truncate">{s.description}</span>
                </button>
              </li>
            ))}
          </ul>
        )}
        <div className="relative flex items-end bg-gray-900 rounded-xl border border-gray-600 focus-within:border-purple-500 overflow-hidden">
          <textarea
            ref={taRef}
            className="w-full bg-transparent text-white p-4 max-h-32 focus:outline-none resize-none overflow-y-auto"
            rows={1}
            placeholder="What do you do?"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={handleKeyDown}
            onFocus={() => onComposerFocus?.()}
            disabled={disabled}
            aria-label="Type your action"
          />
          {busy ? (
            <button
              onClick={() => onStop?.()}
              disabled={disabled}
              className="m-2 p-2 rounded-lg bg-gray-700 hover:bg-red-900/70 text-gray-200 hover:text-red-200 disabled:opacity-50 transition-colors"
              aria-label="Stop generating"
              title="Stop generating"
            >
              <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
                <rect x="5" y="5" width="10" height="10" rx="1.5" />
              </svg>
            </button>
          ) : (
            <button
              onClick={handleSend}
              disabled={blocked}
              className="m-2 p-2 rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-50 transition-colors"
              aria-label={isEmpty ? 'Continue the story' : 'Send message'}
              title={isEmpty ? 'Continue the story (no input)' : 'Send message'}
            >
              {isEmpty ? (
                // Fast-forward icon → "continue the story on its own"
                <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
                  <path d="M4.5 4.5a.75.75 0 011.2-.6l5.5 4.125V5.1a.75.75 0 011.2-.6l6 4.5a.75.75 0 010 1.2l-6 4.5a.75.75 0 01-1.2-.6v-2.925L5.7 15.9a.75.75 0 01-1.2-.6v-10.8z" />
                </svg>
              ) : (
                <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
                  <path d="M10.894 2.553a1 1 0 00-1.788 0l-7 14a1 1 0 001.169 1.409l5-1.429A1 1 0 009 15.571V11a1 1 0 112 0v4.571a1 1 0 00.725.962l5 1.428a1 1 0 001.17-1.408l-7-14z" />
                </svg>
              )}
            </button>
          )}
        </div>
      </div>
      <div className="text-center mt-2 text-xs text-gray-500">
        {busy
          ? 'Generating… press the stop button to interrupt.'
          : isEmpty
            ? 'Enter alone continues the story · ↑ edit last message · ←/→ switch variants · / for commands.'
            : 'Press Enter to send, Shift+Enter for new line.'}
      </div>
    </div>
  );
}

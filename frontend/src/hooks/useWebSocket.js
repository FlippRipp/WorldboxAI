import { useState, useRef, useEffect, useCallback } from 'react';
import { WS_URL } from '../lib/constants';

// Map server chat_messages (role 'ai') to UI messages (role 'assistant'),
// preserving reasoning and display metadata, and tagging the final message
// with the current turn.
function mapServerMessages(chatMsgs, lastTurn) {
  const arr = Array.isArray(chatMsgs) ? chatMsgs : [];
  return arr.map((m, i) => ({
    role: m.role === 'ai' ? 'assistant' : m.role,
    content: m.content,
    reasoning: m.reasoning || null,
    meta: m.meta || null,
    turn: i === arr.length - 1 ? lastTurn : null,
  }));
}

// Server rebuilds (`done`, swipe/edit REST responses) replace the whole
// transcript, but usually only the tail actually changed. Keep the previous
// object for every message whose fields are equal so memoized MessageBlocks
// bail out instead of re-rendering the entire feed.
function reconcileMessages(prev, next) {
  if (!Array.isArray(prev) || prev.length === 0) return next;
  let changed = prev.length !== next.length;
  const merged = next.map((m, i) => {
    const p = prev[i];
    if (
      p &&
      p.role === m.role &&
      p.content === m.content &&
      (p.reasoning || null) === (m.reasoning || null) &&
      (p.turn ?? null) === (m.turn ?? null) &&
      (p.error || false) === (m.error || false) &&
      JSON.stringify(p.meta || null) === JSON.stringify(m.meta || null)
    ) {
      return p;
    }
    changed = true;
    return m;
  });
  return changed ? merged : prev;
}

export function useWebSocket(onStateChange, onLLMCall) {
  const [isConnected, setIsConnected] = useState(false);
  const [isReconnecting, setIsReconnecting] = useState(false);
  const [currentStream, setCurrentStream] = useState(null);
  const [currentReasoning, setCurrentReasoning] = useState(null);
  // True while the reader/librarian and module on_librarian steps run
  // server-side, i.e. after the narration streamed (`message_complete`) but
  // before the turn fully resolves (`done`/`error`).
  const [postProcessing, setPostProcessing] = useState(false);
  // Latest pipeline stage reported by the server ({ stage, label } or null),
  // e.g. "Recalling memories…" / "Updating the world…". Purely informational.
  const [pipelineStatus, setPipelineStatus] = useState(null);
  const [messages, setMessages] = useState([]);
  const [swipes, setSwipes] = useState(null);
  // Latest slash-command output, shown in a popup instead of the story feed.
  const [commandResult, setCommandResult] = useState(null);
  // Set when the user stops a turn: the discarded input, so the composer can
  // restore it. `at` makes consecutive stops with the same text distinct.
  const [restoredInput, setRestoredInput] = useState(null);
  const wsRef = useRef(null);
  const streamRef = useRef('');
  const reasoningRef = useRef('');
  const flushRafRef = useRef(0);
  const reconnectTimeoutRef = useRef(null);
  // Reconnect backoff: starts at 3s, doubles up to 30s, resets on connect.
  const reconnectDelayRef = useRef(3000);
  const hadConnectedRef = useRef(false);
  const activeRef = useRef(true);
  const onStateChangeRef = useRef(onStateChange);
  onStateChangeRef.current = onStateChange;
  const onLLMCallRef = useRef(onLLMCall);
  onLLMCallRef.current = onLLMCall;

  // Tokens arrive faster than they should render: buffer them in refs and
  // flush to state once per animation frame, so markdown re-parsing and React
  // re-renders are capped at display refresh rate instead of per token.
  const cancelFlush = useCallback(() => {
    if (flushRafRef.current) {
      cancelAnimationFrame(flushRafRef.current);
      flushRafRef.current = 0;
    }
  }, []);

  const scheduleFlush = useCallback(() => {
    if (flushRafRef.current) return;
    flushRafRef.current = requestAnimationFrame(() => {
      flushRafRef.current = 0;
      setCurrentStream(streamRef.current);
      setCurrentReasoning(reasoningRef.current);
    });
  }, []);

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!activeRef.current) return;
      if (wsRef.current === ws) {
        setIsConnected(true);
        setIsReconnecting(false);
        reconnectDelayRef.current = 3000;
        if (hadConnectedRef.current) {
          // Reconnect: an in-flight turn keeps generating server-side. Drop
          // the stale local stream and ask the server to replay authoritative
          // state — if a turn is still running it answers with a
          // `generation_snapshot` that repaints the stream, and live tokens
          // resume on this socket.
          cancelFlush();
          setCurrentStream(null);
          setCurrentReasoning(null);
          setPostProcessing(false);
          setPipelineStatus(null);
          streamRef.current = '';
          reasoningRef.current = '';
          ws.send(JSON.stringify({ action: 'sync' }));
        }
        hadConnectedRef.current = true;
      }
    };

    ws.onmessage = (event) => {
      if (!activeRef.current) return;
      const data = JSON.parse(event.data);

      if (data.type === 'token') {
        streamRef.current += data.content;
        scheduleFlush();
      } else if (data.type === 'reasoning_token') {
        reasoningRef.current += data.content;
        scheduleFlush();
      } else if (data.type === 'message_complete') {
        cancelFlush();
        // Storyteller narration is finished; the reader/librarian are still
        // running server-side. Finalize the streamed text into a message now so
        // it renders immediately (correct colors, no "writing" indicator) rather
        // than waiting for the whole graph. The later `done` rebuilds from
        // authoritative state (turn number, swipes, veto rewrites).
        const streamed = data.content ?? streamRef.current;
        const reasoning = data.reasoning || reasoningRef.current || null;
        setCurrentStream(null);
        setCurrentReasoning(null);
        streamRef.current = '';
        reasoningRef.current = '';
        // Narration is done, but the reader/librarian still run server-side.
        setPostProcessing(true);
        // Clear stale swipe meta from the prior turn; `done` sets the real value.
        setSwipes(null);
        if (streamed) {
          setMessages(msgs => [...msgs, { role: 'assistant', content: streamed, reasoning }]);
        }
      } else if (data.type === 'player_action') {
        // Storyteller auto mode: the server generated the player's in-character
        // action. Show it as the user message — replacing the locally echoed
        // guidance text if there was one, appending otherwise (continue turns).
        setMessages(prev => {
          const next = [...prev];
          if (next.length && next[next.length - 1].role === 'user') {
            next[next.length - 1] = { ...next[next.length - 1], content: data.content };
          } else {
            next.push({ role: 'user', content: data.content });
          }
          return next;
        });
      } else if (data.type === 'status') {
        // Pipeline stage update (gather_context / storyteller / reader /
        // librarian). Shown as a live status line while the turn runs.
        setPipelineStatus({ stage: data.stage, label: data.label });
      } else if (data.type === 'done') {
        cancelFlush();
        const streamed = streamRef.current;
        setCurrentStream(null);
        setCurrentReasoning(null);
        setPostProcessing(false);
        setPipelineStatus(null);
        streamRef.current = '';
        reasoningRef.current = '';
        // Rebuild the transcript from authoritative server state so regenerate,
        // veto rewrites and reasoning are all reflected in place.
        const chatMsgs = data.state?.chat_messages;
        if (Array.isArray(chatMsgs) && chatMsgs.length) {
          setMessages(prev => reconcileMessages(prev, mapServerMessages(chatMsgs, data.state?.turn)));
        } else if (streamed) {
          setMessages(msgs => [...msgs, { role: 'assistant', content: streamed, turn: data.state?.turn }]);
        }
        if (data.state) setSwipes(data.state.swipes || null);
        if (data.state && onStateChangeRef.current) onStateChangeRef.current(data.state);
      } else if (data.type === 'turn_stopped') {
        // User stopped the turn: nothing was saved server-side, so rebuild the
        // transcript from authoritative state (drops the optimistic user bubble
        // and any partial stream) and hand the discarded input back.
        cancelFlush();
        setCurrentStream(null);
        setCurrentReasoning(null);
        setPostProcessing(false);
        setPipelineStatus(null);
        streamRef.current = '';
        reasoningRef.current = '';
        const chatMsgs = data.state?.chat_messages;
        if (Array.isArray(chatMsgs)) {
          setMessages(prev => reconcileMessages(prev, mapServerMessages(chatMsgs, data.state?.turn)));
        }
        if (data.state) setSwipes(data.state.swipes || null);
        if (data.state && onStateChangeRef.current) onStateChangeRef.current(data.state);
        if (data.input) setRestoredInput({ text: data.input, at: Date.now() });
      } else if (data.type === 'error') {
        cancelFlush();
        setCurrentStream(null);
        setCurrentReasoning(null);
        setPostProcessing(false);
        setPipelineStatus(null);
        streamRef.current = '';
        reasoningRef.current = '';
        setMessages(prev => [...prev, { role: 'system', content: data.message || 'Turn failed.', error: true }]);
        if (data.onError) data.onError(data);
      } else if (data.type === 'generation_snapshot') {
        // A turn survived a disconnect (or page reload) and is still running
        // server-side: repaint everything streamed so far and resume the
        // generating UI. The turn's own `done`/`error` will land on this
        // socket and finalize as usual.
        cancelFlush();
        if (data.input && data.action === 'turn') {
          // The pending player message isn't in the saved transcript yet
          // (it's only persisted when the turn completes), so restore its
          // bubble unless it's already painted.
          setMessages(prev => {
            const last = prev[prev.length - 1];
            if (last && last.role === 'user' && last.content === data.input) return prev;
            return [...prev, { role: 'user', content: data.input }];
          });
        }
        if (data.narration_complete) {
          // Narration finished while we were away; reader/librarian still run.
          streamRef.current = '';
          reasoningRef.current = '';
          setCurrentStream(null);
          setCurrentReasoning(null);
          setPostProcessing(true);
          if (data.story) {
            setMessages(prev => [...prev, { role: 'assistant', content: data.story, reasoning: data.reasoning || null }]);
          }
        } else {
          streamRef.current = data.story || '';
          reasoningRef.current = data.reasoning || '';
          setCurrentStream(streamRef.current);
          setCurrentReasoning(reasoningRef.current || null);
          setPostProcessing(false);
        }
        setPipelineStatus(data.status || null);
      } else if (data.type === 'state_load') {
        const chatMessages = data.chat_messages || [];
        if (chatMessages.length > 0) {
          setMessages(prev => reconcileMessages(prev, mapServerMessages(chatMessages, null)));
        }
      } else if (data.type === 'command_result') {
        // Slash-command output: surface it in a popup, never the transcript.
        setCommandResult({
          command: data.command,
          name: data.name,
          icon: data.icon,
          message: data.message,
          at: Date.now(),
        });
      } else if (data.type === 'state_update') {
        if (data.state && onStateChangeRef.current) onStateChangeRef.current(data.state);
      } else if (data.type === 'llm_call') {
        if (data.call && onLLMCallRef.current) onLLMCallRef.current(data.call);
      }
    };

    ws.onclose = () => {
      if (!activeRef.current) return;
      if (wsRef.current === ws) {
        setIsConnected(false);
        setIsReconnecting(true);
        reconnectTimeoutRef.current = setTimeout(connect, reconnectDelayRef.current);
        reconnectDelayRef.current = Math.min(reconnectDelayRef.current * 2, 30000);
      }
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [cancelFlush, scheduleFlush]);

  // Reset stream buffers (and any pending flush) before starting a new turn.
  const resetStream = useCallback(() => {
    cancelFlush();
    setCurrentStream('');
    setCurrentReasoning('');
    setPipelineStatus(null);
    streamRef.current = '';
    reasoningRef.current = '';
  }, [cancelFlush]);

  const sendMessage = useCallback((text) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      setMessages(prev => [...prev, { role: 'user', content: text }]);
      resetStream();
      wsRef.current.send(JSON.stringify({ action: 'turn', text }));
    }
  }, [resetStream]);

  // A slash command: no user bubble, no stream — the server answers with a
  // `command_result` popup and a `state_update`, leaving the transcript alone.
  const sendCommand = useCallback((text) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'turn', text }));
    }
  }, []);

  const clearCommandResult = useCallback(() => setCommandResult(null), []);

  const sendRegenerate = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      // Drop the current last AI message so the regenerated one streams in place.
      setMessages(prev => (prev.length && prev[prev.length - 1].role === 'assistant' ? prev.slice(0, -1) : prev));
      resetStream();
      wsRef.current.send(JSON.stringify({ action: 'regenerate' }));
    }
  }, [resetStream]);

  const sendContinue = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      // A "continue" turn injects no player message, so don't add a user bubble;
      // the assistant response streams straight in.
      resetStream();
      wsRef.current.send(JSON.stringify({ action: 'continue' }));
    }
  }, [resetStream]);

  // `quiet` re-opens an existing story: the transcript is already painted from
  // cached state, so we don't want the "AI is writing…" placeholder while the
  // server runs on_gather_context (which can take seconds). A new story omits
  // it and streams its opening as usual.
  const sendIntro = useCallback(({ quiet = false } = {}) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      if (quiet) {
        cancelFlush();
        streamRef.current = '';
        reasoningRef.current = '';
      } else {
        resetStream();
      }
      wsRef.current.send(JSON.stringify({ action: 'intro' }));
    }
  }, [resetStream, cancelFlush]);

  // Interrupt the running turn. The server answers with `turn_stopped`, which
  // clears the stream and restores the input — no local state change here.
  const sendStop = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ action: 'stop' }));
    }
  }, []);

  // Apply a REST response's state (swipe select / edit / delete) to the transcript.
  const applyServerState = useCallback((state) => {
    if (!state) return;
    if (Array.isArray(state.chat_messages)) {
      setMessages(prev => reconcileMessages(prev, mapServerMessages(state.chat_messages, state.turn)));
    }
    setSwipes(state.swipes || null);
    if (onStateChangeRef.current) onStateChangeRef.current(state);
  }, []);

  useEffect(() => {
    activeRef.current = true;
    connect();
    return () => {
      activeRef.current = false;
      cancelFlush();
      clearTimeout(reconnectTimeoutRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [connect, cancelFlush]);

  return {
    isConnected, isReconnecting, messages, currentStream, currentReasoning, swipes, postProcessing, pipelineStatus, restoredInput,
    commandResult, clearCommandResult,
    sendMessage, sendCommand, sendRegenerate, sendContinue, sendIntro, sendStop, setMessages, setSwipes, applyServerState,
  };
}

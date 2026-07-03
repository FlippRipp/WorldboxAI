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

export function useWebSocket(onStateChange, onLLMCall) {
  const [isConnected, setIsConnected] = useState(false);
  const [isReconnecting, setIsReconnecting] = useState(false);
  const [currentStream, setCurrentStream] = useState(null);
  const [currentReasoning, setCurrentReasoning] = useState(null);
  // True while the reader/librarian and module on_librarian steps run
  // server-side, i.e. after the narration streamed (`message_complete`) but
  // before the turn fully resolves (`done`/`error`).
  const [postProcessing, setPostProcessing] = useState(false);
  const [messages, setMessages] = useState([]);
  const [swipes, setSwipes] = useState(null);
  // Set when the user stops a turn: the discarded input, so the composer can
  // restore it. `at` makes consecutive stops with the same text distinct.
  const [restoredInput, setRestoredInput] = useState(null);
  const wsRef = useRef(null);
  const streamRef = useRef('');
  const reasoningRef = useRef('');
  const flushRafRef = useRef(0);
  const reconnectTimeoutRef = useRef(null);
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
        if (hadConnectedRef.current) {
          // Reconnect: any in-flight turn died with the old socket. Drop the
          // stale stream and ask the server to replay authoritative state.
          cancelFlush();
          setCurrentStream(null);
          setCurrentReasoning(null);
          setPostProcessing(false);
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
      } else if (data.type === 'done') {
        cancelFlush();
        const streamed = streamRef.current;
        setCurrentStream(null);
        setCurrentReasoning(null);
        setPostProcessing(false);
        streamRef.current = '';
        reasoningRef.current = '';
        // Rebuild the transcript from authoritative server state so regenerate,
        // veto rewrites and reasoning are all reflected in place.
        const chatMsgs = data.state?.chat_messages;
        if (Array.isArray(chatMsgs) && chatMsgs.length) {
          setMessages(mapServerMessages(chatMsgs, data.state?.turn));
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
        streamRef.current = '';
        reasoningRef.current = '';
        const chatMsgs = data.state?.chat_messages;
        if (Array.isArray(chatMsgs)) {
          setMessages(mapServerMessages(chatMsgs, data.state?.turn));
        }
        if (data.state) setSwipes(data.state.swipes || null);
        if (data.state && onStateChangeRef.current) onStateChangeRef.current(data.state);
        if (data.input) setRestoredInput({ text: data.input, at: Date.now() });
      } else if (data.type === 'error') {
        cancelFlush();
        setCurrentStream(null);
        setCurrentReasoning(null);
        setPostProcessing(false);
        streamRef.current = '';
        reasoningRef.current = '';
        setMessages(prev => [...prev, { role: 'system', content: data.message || 'Turn failed.', error: true }]);
        if (data.onError) data.onError(data);
      } else if (data.type === 'state_load') {
        const chatMessages = data.chat_messages || [];
        if (chatMessages.length > 0) {
          setMessages(mapServerMessages(chatMessages, null));
        }
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
        reconnectTimeoutRef.current = setTimeout(connect, 3000);
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

  const sendIntro = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      resetStream();
      wsRef.current.send(JSON.stringify({ action: 'intro' }));
    }
  }, [resetStream]);

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
      setMessages(mapServerMessages(state.chat_messages, state.turn));
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
    isConnected, isReconnecting, messages, currentStream, currentReasoning, swipes, postProcessing, restoredInput,
    sendMessage, sendRegenerate, sendContinue, sendIntro, sendStop, setMessages, setSwipes, applyServerState,
  };
}

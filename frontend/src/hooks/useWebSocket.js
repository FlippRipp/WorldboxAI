import { useState, useRef, useEffect, useCallback } from 'react';
import { WS_URL } from '../lib/constants';

export function useWebSocket(onStateChange, onLLMCall) {
  const [isConnected, setIsConnected] = useState(false);
  const [isReconnecting, setIsReconnecting] = useState(false);
  const [currentStream, setCurrentStream] = useState(null);
  const [messages, setMessages] = useState([]);
  const wsRef = useRef(null);
  const streamRef = useRef('');
  const reconnectTimeoutRef = useRef(null);
  const activeRef = useRef(true);
  const onStateChangeRef = useRef(onStateChange);
  onStateChangeRef.current = onStateChange;
  const onLLMCallRef = useRef(onLLMCall);
  onLLMCallRef.current = onLLMCall;

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      if (!activeRef.current) return;
      if (wsRef.current === ws) {
        setIsConnected(true);
        setIsReconnecting(false);
      }
    };

    ws.onmessage = (event) => {
      if (!activeRef.current) return;
      const data = JSON.parse(event.data);

      if (data.type === 'token') {
        setCurrentStream(prev => {
          const next = (prev || '') + data.content;
          streamRef.current = next;
          return next;
        });
      } else if (data.type === 'done') {
        const completed = streamRef.current;
        setCurrentStream(null);
        if (completed) {
          setMessages(msgs => [...msgs, { role: 'assistant', content: completed, turn: data.state?.turn }]);
        }
        streamRef.current = '';
        if (data.state && onStateChangeRef.current) onStateChangeRef.current(data.state);
      } else if (data.type === 'error') {
        setCurrentStream(null);
        setMessages(prev => [...prev, { role: 'system', content: data.message || 'Turn failed.', error: true }]);
        if (data.onError) data.onError(data);
      } else if (data.type === 'state_load') {
        const chatMessages = data.chat_messages || [];
        if (chatMessages.length > 0) {
          setMessages(chatMessages.map(m => ({
            role: m.role === 'ai' ? 'assistant' : m.role,
            content: m.content,
          })));
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
  }, []);

  const sendMessage = useCallback((text) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      setMessages(prev => [...prev, { role: 'user', content: text }]);
      setCurrentStream('');
      wsRef.current.send(JSON.stringify({ action: 'turn', text }));
    }
  }, []);

  const sendIntro = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      setCurrentStream('');
      wsRef.current.send(JSON.stringify({ action: 'intro' }));
    }
  }, []);

  useEffect(() => {
    activeRef.current = true;
    connect();
    return () => {
      activeRef.current = false;
      clearTimeout(reconnectTimeoutRef.current);
      if (wsRef.current) {
        wsRef.current.onclose = null;
        wsRef.current.close();
      }
    };
  }, [connect]);

  return {
    isConnected, isReconnecting, messages, currentStream,
    sendMessage, sendIntro, setMessages
  };
}

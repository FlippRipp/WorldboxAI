import { useRef, useEffect, useCallback } from 'react';
import MarkdownRenderer from '../shared/MarkdownRenderer';

const MESSAGE_STYLES = {
  assistant: { bg: 'bg-gray-850/80', text: 'text-gray-200' },
  ai:        { bg: 'bg-gray-850/80', text: 'text-gray-200' },
  user:      { bg: 'bg-gray-800/80', text: 'text-gray-200' },
  system:    { bg: 'bg-red-950/40',  text: 'text-red-200' },
};

function MessageBlock({ message }) {
  const isUser = message.role === 'user';
  const isSystem = message.role === 'system' || message.error;
  const styleKey = isSystem ? 'system' : isUser ? 'user' : 'assistant';
  const style = MESSAGE_STYLES[styleKey] || MESSAGE_STYLES.assistant;

  return (
    <div className={`${style.bg} ${style.text}`}>
      <div className="max-w-[720px] mx-auto px-8 py-6">
        {isUser ? (
          <p className="text-lg leading-relaxed whitespace-pre-wrap">{message.content}</p>
        ) : isSystem ? (
          <p className="whitespace-pre-wrap text-sm">{message.content}</p>
        ) : (
          <div className="text-lg leading-relaxed">
            <MarkdownRenderer content={message.content} />
          </div>
        )}
        {message.turn != null && (
          <div className="text-xs text-gray-600 mt-6 text-right">Turn {message.turn}</div>
        )}
      </div>
    </div>
  );
}

function StreamingBlock({ content }) {
  if (content == null) return null;

  return (
    <div className="bg-gray-850/80 text-gray-200">
      <div className="max-w-[720px] mx-auto px-8 py-6">
        <div className="flex items-center gap-2 mb-3">
          <span className="relative flex h-2 w-2">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-purple-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-2 w-2 bg-purple-500" />
          </span>
          <span className="text-xs text-gray-500 animate-pulse">AI is writing...</span>
        </div>
        <p className="text-lg leading-relaxed whitespace-pre-wrap text-gray-200">
          {content}
          <span className="inline-block w-0.5 h-5 ml-0.5 bg-gray-400 animate-pulse align-middle" />
        </p>
      </div>
    </div>
  );
}

export function ChatFeed({ messages, currentStream }) {
  const messagesEndRef = useRef(null);

  const scrollToBottom = useCallback(() => {
    const behavior = currentStream ? 'auto' : 'smooth';
    messagesEndRef.current?.scrollIntoView({ behavior });
  }, [currentStream]);

  useEffect(() => {
    scrollToBottom();
  }, [messages, currentStream, scrollToBottom]);

  const isEmpty = messages.length === 0 && currentStream == null;

  return (
    <div className="flex-1 overflow-y-auto book-scroll" role="log" aria-live="polite">
      {isEmpty && (
        <div className="flex items-center justify-center h-full">
          <p className="text-gray-600 text-lg">
            Welcome to WorldBox. Start your adventure!
          </p>
        </div>
      )}

      {messages.map((msg, idx) => (
        <MessageBlock key={idx} message={msg} />
      ))}

      <StreamingBlock content={currentStream} />

      <div ref={messagesEndRef} />
    </div>
  );
}

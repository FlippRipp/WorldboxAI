import { useState, useCallback } from 'react';

export default function ChatInput({ onSend, disabled }) {
  const [inputValue, setInputValue] = useState('');

  const handleSend = useCallback(() => {
    if (!inputValue.trim() || disabled) return;
    onSend(inputValue);
    setInputValue('');
  }, [inputValue, disabled, onSend]);

  const handleKeyDown = useCallback((e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }, [handleSend]);

  return (
    <div className="p-4 border-t border-gray-700 bg-gray-800">
      <div className="max-w-[720px] mx-auto relative flex items-end bg-gray-900 rounded-xl border border-gray-600 focus-within:border-purple-500 overflow-hidden">
        <textarea
          className="w-full bg-transparent text-white p-4 max-h-32 focus:outline-none resize-none"
          rows={1}
          placeholder="What do you do?"
          value={inputValue}
          onChange={(e) => setInputValue(e.target.value)}
          onKeyDown={handleKeyDown}
          disabled={disabled}
          aria-label="Type your action"
        />
        <button
          onClick={handleSend}
          disabled={disabled || !inputValue.trim()}
          className="m-2 p-2 rounded-lg bg-purple-600 hover:bg-purple-500 disabled:opacity-50 transition-colors"
          aria-label="Send message"
        >
          <svg xmlns="http://www.w3.org/2000/svg" className="h-5 w-5" viewBox="0 0 20 20" fill="currentColor">
            <path d="M10.894 2.553a1 1 0 00-1.788 0l-7 14a1 1 0 001.169 1.409l5-1.429A1 1 0 009 15.571V11a1 1 0 112 0v4.571a1 1 0 00.725.962l5 1.428a1 1 0 001.17-1.408l-7-14z" />
          </svg>
        </button>
      </div>
      <div className="text-center mt-2 text-xs text-gray-500">
        Press Enter to send, Shift+Enter for new line.
      </div>
    </div>
  );
}

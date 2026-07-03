import { useState, useCallback, useRef } from 'react';

// Minimal toast stack: useToasts() owns the queue, <ToastStack /> renders it.
// Replaces blocking alert() calls for non-fatal errors.
export function useToasts() {
  const [toasts, setToasts] = useState([]);
  const nextId = useRef(0);

  const showToast = useCallback((message, kind = 'error') => {
    const id = ++nextId.current;
    setToasts(prev => [...prev, { id, message, kind }]);
    setTimeout(() => {
      setToasts(prev => prev.filter(t => t.id !== id));
    }, 5000);
  }, []);

  const dismissToast = useCallback((id) => {
    setToasts(prev => prev.filter(t => t.id !== id));
  }, []);

  return { toasts, showToast, dismissToast };
}

const KIND_STYLES = {
  error: 'border-red-700/70 bg-red-950/90 text-red-100',
  info: 'border-gray-600 bg-gray-800/95 text-gray-200',
};

export function ToastStack({ toasts, onDismiss }) {
  if (!toasts.length) return null;
  return (
    <div className="fixed bottom-4 right-4 z-50 flex flex-col gap-2 max-w-sm">
      {toasts.map(t => (
        <div
          key={t.id}
          role="alert"
          className={`flex items-start gap-3 px-4 py-3 rounded-lg border shadow-xl text-sm animate-slide-in-right ${KIND_STYLES[t.kind] || KIND_STYLES.info}`}
        >
          <span className="flex-1 break-words">{t.message}</span>
          <button
            onClick={() => onDismiss(t.id)}
            className="shrink-0 opacity-60 hover:opacity-100 transition-opacity"
            aria-label="Dismiss notification"
          >
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
      ))}
    </div>
  );
}

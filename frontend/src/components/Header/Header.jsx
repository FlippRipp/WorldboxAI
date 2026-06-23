import ConnectionStatus from './ConnectionStatus';
import SlotRenderer from '../Slots/SlotRenderer';

export default function Header({
  modules, gameState, ws, session,
  onOpenSettings, onOpenHealth, onOpenMemories,
  onBack
}) {
  return (
    <>
      <ConnectionStatus isConnected={ws.isConnected} isReconnecting={ws.isReconnecting} />

      <header className="h-14 border-b border-gray-700 flex items-center px-4 justify-between bg-gray-800 shrink-0">
        <div className="flex items-center gap-3">
          {onBack && (
            <button
              onClick={onBack}
              className="flex items-center gap-1 text-gray-400 hover:text-white transition-colors px-2 py-1 rounded hover:bg-gray-700"
              aria-label="Back to menu"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
              </svg>
            </button>
          )}
          <span className="lg:hidden font-bold text-purple-400">WorldBox</span>
          {session?.sessionState && (
            <div className="hidden sm:flex items-center gap-2 text-sm text-gray-400">
              <span className="text-gray-500">{session.sessionState.active_save_id || 'autosave'}</span>
              <span className="text-gray-600">·</span>
              <span>Turn {session.sessionState.turn ?? 0}</span>
            </div>
          )}
        </div>

        <div className="flex items-center gap-2">
          <SlotRenderer
            slotName="slot_header"
            modules={modules}
            state={gameState}
            config={gameState?.module_configs}
          />

          <button
            className="text-gray-400 hover:text-white px-3 py-2 text-sm border border-gray-700 rounded hover:border-purple-500 transition-colors"
            onClick={onOpenMemories}
            aria-label="Open memory browser"
          >
            Memories
          </button>

          <button
            className="text-gray-400 hover:text-white px-3 py-2 text-sm border border-gray-700 rounded hover:border-purple-500 transition-colors"
            onClick={onOpenHealth}
            aria-label="View system health"
          >
            Health
          </button>

          <button
            className="text-gray-400 hover:text-white p-2"
            onClick={onOpenSettings}
            aria-label="Open settings"
          >
            &#9881;
          </button>
        </div>
      </header>
    </>
  );
}

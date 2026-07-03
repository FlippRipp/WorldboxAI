import ConnectionStatus from './ConnectionStatus';
import SlotRenderer from '../Slots/SlotRenderer';

export default function Header({
  modules, gameState, ws, session,
  onOpenSettings, onOpenHealth, onOpenMemories, onOpenCharacter,
  onBack, onOpenDrawer, hidden = false,
}) {
  return (
    <>
      <ConnectionStatus isConnected={ws.isConnected} isReconnecting={ws.isReconnecting} />

      {/* On phones the bar hides on scroll-down (Chrome-style); the negative
          margin lets the feed reflow into the freed space. lg:mt-0 hard-stops
          the behavior on desktop regardless of JS state. */}
      <header
        className={`h-14 border-b border-gray-700 flex items-center px-4 justify-between bg-gray-800 shrink-0 transition-[margin-top] duration-200 ease-out ${hidden ? '-mt-14 lg:mt-0' : 'mt-0'}`}
      >
        <div className="flex items-center gap-3 min-w-0">
          {onOpenDrawer && (
            <button
              className="lg:hidden p-2 -ml-2 text-gray-400 hover:text-white transition-colors rounded hover:bg-gray-700"
              onClick={onOpenDrawer}
              aria-label="Open sidebar"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
              </svg>
            </button>
          )}
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
          {ws.postProcessing && (
            <div
              className="flex items-center gap-1.5 text-xs text-purple-300 px-2 py-1 rounded bg-purple-500/10 border border-purple-500/30"
              title="The reader and librarian are processing this turn (memories, character updates, module hooks)."
              aria-live="polite"
            >
              <svg className="w-3.5 h-3.5 animate-spin" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
              <span className="hidden sm:inline">Processing…</span>
            </div>
          )}

          <SlotRenderer
            slotName="slot_header"
            modules={modules}
            state={gameState}
            config={gameState?.module_configs}
          />

          {/* On mobile these live in the sidebar drawer instead. */}
          <div className="hidden lg:flex items-center gap-2">
            <button
              className="text-gray-400 hover:text-white px-3 py-2 text-sm border border-gray-700 rounded hover:border-purple-500 transition-colors"
              onClick={onOpenCharacter}
              aria-label="Open character view"
            >
              Character
            </button>

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
          </div>

          <button
            className="hidden lg:block text-gray-400 hover:text-white p-2"
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

import ConnectionStatus from './ConnectionStatus';
import SlotRenderer from '../Slots/SlotRenderer';

export default function Header({
  modules, gameState, ws, session,
  onOpenSettings, onOpenHealth, onOpenMemories, onOpenCharacter,
  onToggleMap, displayMode = 'feed', onToggleDisplayMode, onBack, onOpenDrawer, hidden = false,
}) {
  const storyTitle =
    session?.sessionState?.active_display_name
    || session?.sessionState?.active_save_id?.replace(/_/g, ' ')
    || 'WorldBox';
  const currentTurn = gameState?.turn ?? session?.sessionState?.turn ?? 0;

  return (
    <>
      <ConnectionStatus isConnected={ws.isConnected} isReconnecting={ws.isReconnecting} />

      {/* On phones the bar hides on scroll-down (Chrome-style); the negative
          margin lets the feed reflow into the freed space. lg:mt-0 hard-stops
          the behavior on desktop regardless of JS state. */}
      <header
        className={`h-14 border-b border-gray-700 flex items-center px-2 sm:px-4 justify-between gap-2 bg-gray-800 shrink-0 transition-[margin-top] duration-200 ease-out ${hidden ? '-mt-14 lg:mt-0' : 'mt-0'}`}
      >
        <div className="flex items-center gap-1 sm:gap-2 min-w-0 flex-1">
          {onOpenDrawer && (
            <button
              className="lg:hidden min-h-11 min-w-11 flex items-center justify-center text-gray-400 hover:text-white transition-colors rounded-lg hover:bg-gray-700 shrink-0"
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
              className="min-h-11 min-w-11 sm:min-w-0 flex items-center justify-center gap-1 text-gray-400 hover:text-white transition-colors px-2 py-1 rounded-lg hover:bg-gray-700 shrink-0"
              aria-label="Back to stories"
              title="Back to stories"
            >
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
              </svg>
            </button>
          )}

          {/* Mobile story title + turn indicator */}
          <div className="lg:hidden flex items-center gap-1.5 min-w-0 ml-0.5">
            <span className="font-semibold text-sm text-gray-100 truncate">
              {storyTitle}
            </span>
            <span className="shrink-0 text-[11px] font-mono px-1.5 py-0.5 rounded bg-gray-900/70 border border-gray-700 text-gray-400">
              T{currentTurn}
            </span>
          </div>

          {session?.sessionState && (
            <div className="hidden lg:flex items-center gap-2 text-sm text-gray-400 min-w-0">
              <span className="text-gray-300 font-medium truncate max-w-[18rem]">
                {storyTitle}
              </span>
              <span className="text-gray-600">·</span>
              <span>Turn {currentTurn}</span>
            </div>
          )}
        </div>

        <div className="flex items-center gap-1 sm:gap-2 shrink-0">
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

          <div className="hidden sm:block">
            <SlotRenderer
              slotName="slot_header"
              modules={modules}
              state={gameState}
              config={gameState?.module_configs}
              skeleton={false}
            />
          </div>

          {/* Mobile 1-tap quick actions (full nav + module widgets live in the drawer) */}
          <div className="flex lg:hidden items-center gap-0.5">
            {onToggleDisplayMode && (
              <button
                onClick={onToggleDisplayMode}
                className={`min-h-11 min-w-11 flex items-center justify-center rounded-lg transition-colors ${
                  displayMode === 'stage'
                    ? 'text-purple-300 bg-purple-600/20'
                    : 'text-gray-400 hover:text-white hover:bg-gray-700'
                }`}
                aria-label={displayMode === 'stage' ? 'Switch to story log' : 'Switch to paced stage mode'}
                title={displayMode === 'stage' ? 'Paced Stage active — tap for Story Log' : 'Switch to Paced Stage'}
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M4 6h16M4 12h10m-10 6h16" />
                </svg>
              </button>
            )}
            {onToggleMap && (
              <button
                onClick={onToggleMap}
                className="min-h-11 min-w-11 flex items-center justify-center text-gray-400 hover:text-white rounded-lg hover:bg-gray-700 transition-colors"
                aria-label="Toggle world map"
                title="World Map"
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M9 20l-5.447-2.724A1 1 0 013 16.382V5.618a1 1 0 011.447-.894L9 7m0 13l6-3m-6 3V7m6 10l4.553 2.276A1 1 0 0021 18.382V7.618a1 1 0 00-.553-.894L15 4m0 13V4m0 0L9 7" />
                </svg>
              </button>
            )}
            <button
              onClick={onOpenCharacter}
              className="min-h-11 min-w-11 flex items-center justify-center text-gray-400 hover:text-white rounded-lg hover:bg-gray-700 transition-colors"
              aria-label="Open character view"
              title="Character"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M16 7a4 4 0 11-8 0 4 4 0 018 0zM12 14a7 7 0 00-7 7h14a7 7 0 00-7-7z" />
              </svg>
            </button>
            <button
              onClick={onOpenSettings}
              className="min-h-11 min-w-11 flex items-center justify-center text-gray-400 hover:text-white rounded-lg hover:bg-gray-700 transition-colors"
              aria-label="Open settings"
              title="Settings"
            >
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.8} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
              </svg>
            </button>
          </div>

          {/* Desktop text buttons */}
          <div className="hidden lg:flex items-center gap-2">
            {onToggleDisplayMode && (
              <button
                className={`px-3 py-2 text-sm border rounded transition-colors ${
                  displayMode === 'stage'
                    ? 'text-purple-200 bg-purple-600/20 border-purple-500/70 hover:border-purple-400'
                    : 'text-gray-400 hover:text-white border-gray-700 hover:border-purple-500'
                }`}
                onClick={onToggleDisplayMode}
                aria-label={displayMode === 'stage' ? 'Switch to Story Log' : 'Switch to Paced Stage'}
                title={displayMode === 'stage' ? 'Paced Stage mode active — click for classic Story Log' : 'Switch to centered Paced Stage reader'}
              >
                {displayMode === 'stage' ? 'Stage: On' : 'Stage'}
              </button>
            )}
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

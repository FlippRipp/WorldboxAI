import { useRef } from 'react';
import SlotRenderer from '../Slots/SlotRenderer';
import { useLLMInspector } from '../../hooks/useLLMInspector';

// Mounted only while the mobile drawer is open so incoming LLM calls during
// story streaming never re-render the closed sidebar.
function DrawerLLMInspectorRow({ onCloseDrawer }) {
  const { calls, setIsOpen } = useLLMInspector();
  const seen = new Set();
  for (const c of calls) seen.add(c.id);
  const count = seen.size;

  return (
    <button
      onClick={() => {
        onCloseDrawer?.();
        setIsOpen(true);
      }}
      className="min-h-11 flex items-center justify-between px-3 py-2.5 rounded-lg text-gray-300 hover:text-white hover:bg-gray-700 transition-colors"
    >
      <span className="flex items-center gap-2">
        <svg className="w-4 h-4 text-purple-400 shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 9l3 3-3 3m5 0h3M5 20h14a2 2 0 002-2V6a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
        </svg>
        <span>LLM Inspector</span>
      </span>
      <span className="text-xs font-mono px-2 py-0.5 rounded-full bg-gray-900 border border-gray-700 text-gray-400">
        {count}
      </span>
    </button>
  );
}

// Desktop: permanent aside (gated on !isMobile so hidden widgets don't mount
// or fetch bundles on phones). Mobile: a swipe-dismissible drawer controlled
// from App, also hosting nav actions and the relocated LLM Inspector trigger.
export default function Sidebar({
  session, modules, gameState, onCommand, generating,
  isMobile = false, drawerOpen, onCloseDrawer,
  onOpenCharacter, onOpenMemories, onOpenHealth, onOpenSettings,
}) {
  const touchStartRef = useRef(null);

  const sidebarBody = (
    <div className="flex flex-col p-4 gap-4 min-w-0">
      <h2 className="text-xl font-bold text-purple-400 hidden lg:block">WorldBox</h2>

      <div className="text-sm text-gray-400">
        <p>Status: {session?.sessionState ? <span className="text-green-500">Connected</span> : <span className="text-red-500">Disconnected</span>}</p>
      </div>

      <div className="p-3 bg-gray-900/70 rounded-md border border-gray-700 text-xs space-y-2">
        <div className="flex justify-between gap-2">
          <span className="text-gray-500">Story</span>
          <span className="text-gray-200 truncate">
            {session.sessionState?.active_display_name
              || session.sessionState?.active_save_id?.replace(/_/g, ' ')
              || 'unknown'}
          </span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">Turn</span>
          <span className="text-gray-200 font-mono">{gameState?.turn ?? session.sessionState?.turn ?? 0}</span>
        </div>
      </div>

      {/* Header slots hidden on narrow mobile bars surface cleanly here */}
      <div className="sm:hidden">
        <SlotRenderer
          slotName="slot_header"
          modules={modules}
          state={gameState}
          config={gameState?.module_configs}
        />
      </div>

      <SlotRenderer
        slotName="slot_sidebar"
        modules={modules}
        state={gameState}
        config={gameState?.module_configs}
        slotProps={{ onCommand, generating }}
      />
    </div>
  );

  // Header nav actions relocated into the drawer on mobile (they stay in the
  // header bar on lg+). Close the drawer first so overlays don't stack.
  const drawerNav = (
    <div className="border-t border-gray-700 pt-2.5 mt-1 mx-3 flex flex-col gap-0.5 shrink-0 pb-[max(0.75rem,env(safe-area-inset-bottom))]">
      {[
        ['Character', onOpenCharacter],
        ['Memories', onOpenMemories],
        ['Health', onOpenHealth],
        ['Settings', onOpenSettings],
      ].map(([label, fn]) => (
        <button
          key={label}
          onClick={() => { onCloseDrawer?.(); fn?.(); }}
          className="min-h-11 text-left px-3 py-2.5 rounded-lg text-gray-300 hover:text-white hover:bg-gray-700 transition-colors"
        >
          {label}
        </button>
      ))}
      <DrawerLLMInspectorRow onCloseDrawer={onCloseDrawer} />
    </div>
  );

  const handleTouchStart = (e) => {
    const t = e.touches?.[0];
    if (t) touchStartRef.current = { x: t.clientX, y: t.clientY };
  };

  const handleTouchEnd = (e) => {
    const start = touchStartRef.current;
    touchStartRef.current = null;
    const t = e.changedTouches?.[0];
    if (!start || !t) return;
    const dx = t.clientX - start.x;
    const dy = t.clientY - start.y;
    if (dx < -60 && Math.abs(dx) > Math.abs(dy) * 1.8) {
      onCloseDrawer?.();
    }
  };

  return (
    <>
      {/* Desktop sidebar — unmounted on mobile so hidden widgets don't compile/run */}
      {!isMobile && (
        <aside className="hidden lg:flex lg:flex-col lg:w-72 border-r border-gray-700 bg-gray-800 overflow-y-auto overflow-x-hidden book-scroll">
          {sidebarBody}
        </aside>
      )}

      {/* Mobile drawer */}
      {drawerOpen && (
        <div className="lg:hidden fixed inset-0 z-40">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={onCloseDrawer}
          />
          <div
            onTouchStart={handleTouchStart}
            onTouchEnd={handleTouchEnd}
            className="absolute left-0 top-0 bottom-0 w-72 max-w-[85vw] bg-gray-800 border-r border-gray-700 shadow-2xl animate-slide-in flex flex-col"
          >
            <div className="flex items-center justify-between px-4 py-2 border-b border-gray-700/70 shrink-0">
              <span className="text-base font-bold text-purple-400">WorldBox</span>
              <button
                onClick={onCloseDrawer}
                className="min-h-11 min-w-11 -mr-2 flex items-center justify-center text-gray-400 hover:text-white hover:bg-gray-700 rounded-lg transition-colors"
                aria-label="Close sidebar"
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden book-scroll">
              {sidebarBody}
            </div>
            {drawerNav}
          </div>
        </div>
      )}
    </>
  );
}

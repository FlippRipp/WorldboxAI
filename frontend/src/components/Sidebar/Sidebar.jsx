import SlotRenderer from '../Slots/SlotRenderer';

// Desktop: permanent aside. Mobile: a drawer controlled from App (the trigger
// lives in the Header so it never overlaps other header controls). On mobile
// the drawer also hosts the nav actions that don't fit in the slim header bar.
export default function Sidebar({
  session, modules, gameState,
  drawerOpen, onCloseDrawer,
  onOpenCharacter, onOpenMemories, onOpenHealth, onOpenSettings,
}) {
  const sidebarContent = (
    <div className="flex flex-col h-full p-4 gap-4 overflow-y-auto">
      <h2 className="text-xl font-bold text-purple-400">WorldBox</h2>

      <div className="text-sm text-gray-400">
        <p>Status: {session?.sessionState ? <span className="text-green-500">Connected</span> : <span className="text-red-500">Disconnected</span>}</p>
      </div>

      <div className="p-3 bg-gray-900/70 rounded-md border border-gray-700 text-xs space-y-2">
        <div className="flex justify-between">
          <span className="text-gray-500">Save</span>
          <span className="text-gray-200 font-mono">{session.sessionState?.active_save_id || 'unknown'}</span>
        </div>
        <div className="flex justify-between">
          <span className="text-gray-500">Turn</span>
          <span className="text-gray-200 font-mono">{session.sessionState?.turn ?? 0}</span>
        </div>
      </div>

      <SlotRenderer
        slotName="slot_sidebar"
        modules={modules}
        state={gameState}
        config={gameState?.module_configs}
      />
    </div>
  );

  // Header nav actions relocated into the drawer on mobile (they stay in the
  // header bar on lg+). Close the drawer first so overlays don't stack.
  const drawerNav = (
    <div className="border-t border-gray-700 pt-3 mt-1 mx-4 flex flex-col gap-1 shrink-0 pb-4">
      {[
        ['Character', onOpenCharacter],
        ['Memories', onOpenMemories],
        ['Health', onOpenHealth],
        ['Settings', onOpenSettings],
      ].map(([label, fn]) => (
        <button
          key={label}
          onClick={() => { onCloseDrawer?.(); fn?.(); }}
          className="text-left px-3 py-2.5 rounded text-gray-300 hover:text-white hover:bg-gray-700 transition-colors"
        >
          {label}
        </button>
      ))}
    </div>
  );

  return (
    <>
      {/* Desktop sidebar */}
      <aside className="hidden lg:flex lg:flex-col lg:w-72 border-r border-gray-700 bg-gray-800">
        {sidebarContent}
      </aside>

      {/* Mobile drawer */}
      {drawerOpen && (
        <div className="lg:hidden fixed inset-0 z-40">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={onCloseDrawer}
          />
          <div className="absolute left-0 top-0 bottom-0 w-72 bg-gray-800 border-r border-gray-700 shadow-2xl animate-slide-in flex flex-col">
            <div className="flex justify-end p-2 shrink-0">
              <button
                onClick={onCloseDrawer}
                className="p-2 hover:bg-gray-700 rounded-lg"
                aria-label="Close sidebar"
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            <div className="flex-1 min-h-0 overflow-y-auto">
              {sidebarContent}
            </div>
            {drawerNav}
          </div>
        </div>
      )}
    </>
  );
}

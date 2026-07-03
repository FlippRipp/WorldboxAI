import { useState } from 'react';
import SlotRenderer from '../Slots/SlotRenderer';

export default function Sidebar({ session, modules, gameState }) {
  const [drawerOpen, setDrawerOpen] = useState(false);

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

  return (
    <>
      {/* Desktop sidebar */}
      <aside className="hidden lg:flex lg:flex-col lg:w-72 border-r border-gray-700 bg-gray-800">
        {sidebarContent}
      </aside>

      {/* Mobile hamburger */}
      <button
        className="lg:hidden fixed top-3 left-3 z-50 p-2 bg-gray-800 rounded-lg border border-gray-700 shadow-lg"
        onClick={() => setDrawerOpen(true)}
        aria-label="Open sidebar"
      >
        <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
        </svg>
      </button>

      {/* Mobile drawer */}
      {drawerOpen && (
        <div className="lg:hidden fixed inset-0 z-40">
          <div
            className="absolute inset-0 bg-black/60 backdrop-blur-sm"
            onClick={() => setDrawerOpen(false)}
          />
          <div className="absolute left-0 top-0 bottom-0 w-72 bg-gray-800 border-r border-gray-700 shadow-2xl animate-slide-in">
            <div className="flex justify-end p-2">
              <button
                onClick={() => setDrawerOpen(false)}
                className="p-2 hover:bg-gray-700 rounded-lg"
                aria-label="Close sidebar"
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>
            {sidebarContent}
          </div>
        </div>
      )}
    </>
  );
}

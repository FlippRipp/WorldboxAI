import { useState } from 'react';
import SlotRenderer from '../Slots/SlotRenderer';

export default function Sidebar({ session, modules, gameState }) {
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [showConfirmUndo, setShowConfirmUndo] = useState(false);

  const handleUndoClick = () => {
    if (!session.sessionState || session.sessionState.turn <= 0) return;
    setShowConfirmUndo(true);
  };

  const handleUndoConfirm = async () => {
    const targetTurn = session.sessionState.turn - 1;
    try {
      await session.undoTurn(targetTurn);
    } catch (e) {
      alert(`Failed to undo: ${e.message}`);
    }
    setShowConfirmUndo(false);
  };

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

        <button onClick={handleUndoClick} disabled={!session.sessionState || session.sessionState.turn <= 0} className="w-full bg-gray-700 hover:bg-gray-600 disabled:opacity-50 rounded px-2 py-1 transition-colors">
          Undo
        </button>
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

      {/* Undo confirmation dialog */}
      {showConfirmUndo && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" role="dialog" aria-modal="true">
          <div className="bg-gray-800 rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl">
            <h3 className="text-lg font-semibold mb-2">Undo Turn</h3>
            <p className="text-gray-300 text-sm mb-6">
              Revert to turn {session.sessionState.turn - 1}? This will discard all turns after this point.
            </p>
            <div className="flex justify-end gap-3">
              <button onClick={() => setShowConfirmUndo(false)} className="px-4 py-2 text-sm rounded-lg bg-gray-700 hover:bg-gray-600 transition-colors">
                Cancel
              </button>
              <button onClick={handleUndoConfirm} className="px-4 py-2 text-sm rounded-lg bg-red-600 hover:bg-red-500 text-white transition-colors">
                Undo
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

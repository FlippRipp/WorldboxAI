export default function ExitWarning({ onConfirm, onCancel }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-gray-800 rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl border border-gray-700">
        <h3 className="text-lg font-semibold text-gray-100 mb-2">Exit Mode</h3>
        <p className="text-gray-300 text-sm mb-6">
          Leaving this mode may result in unsaved progress. Are you sure you want to return to the main menu?
        </p>
        <div className="flex justify-end gap-3">
          <button
            onClick={onCancel}
            className="px-4 py-2 text-sm rounded-lg bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors"
          >
            Stay
          </button>
          <button
            onClick={onConfirm}
            className="px-4 py-2 text-sm rounded-lg bg-red-600 hover:bg-red-500 text-white transition-colors"
          >
            Exit
          </button>
        </div>
      </div>
    </div>
  );
}

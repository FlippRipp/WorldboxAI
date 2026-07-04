// Shown only while a turn is generating: completed turns are autosaved the
// moment the pipeline finishes, so an idle exit has nothing to warn about.
// `savedTurn` is the last completed turn (0 while the opening streams).
export default function ExitWarning({ savedTurn, onConfirm, onCancel }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="bg-gray-800 rounded-xl p-6 max-w-sm w-full mx-4 shadow-2xl border border-gray-700">
        <h3 className="text-lg font-semibold text-gray-100 mb-2">Generation in progress</h3>
        <p className="text-gray-300 text-sm mb-6">
          {savedTurn > 0 ? (
            <>
              A turn is still generating — if you exit now it will be stopped and
              won&apos;t be saved. Your story is saved through turn {savedTurn}.
            </>
          ) : (
            <>
              The opening is still generating — if you exit now it will be stopped
              and won&apos;t be saved.
            </>
          )}
        </p>
        <div className="flex justify-end gap-3">
          <button
            onClick={onCancel}
            className="px-4 py-2 text-sm rounded-lg bg-gray-700 hover:bg-gray-600 text-gray-200 transition-colors"
          >
            Keep writing
          </button>
          <button
            onClick={onConfirm}
            className="px-4 py-2 text-sm rounded-lg bg-red-600 hover:bg-red-500 text-white transition-colors"
          >
            Exit anyway
          </button>
        </div>
      </div>
    </div>
  );
}

import { useEffect, useRef, useState } from 'react';
import { api } from '../../lib/api';

// App-wide module manager: enable/disable any discovered module, install new
// ones from a zip archive or a GitHub repository, and remove manager-installed
// ones. Distinct from the per-story module toggles on the story start screen —
// a module disabled here disappears from the whole app (menus, story toggles,
// engine dispatch) until re-enabled.
export default function ModuleManagerScreen({ onBack }) {
  const [modules, setModules] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState(null); // module id with an in-flight toggle/remove
  const [installing, setInstalling] = useState(false);
  const [githubUrl, setGithubUrl] = useState('');
  const [notice, setNotice] = useState(null); // {kind: 'ok'|'error', text}
  const fileInputRef = useRef(null);

  useEffect(() => {
    let cancelled = false;
    api.getModuleManager()
      .then((d) => { if (!cancelled) setModules(d.modules || []); })
      .catch((e) => { if (!cancelled) setNotice({ kind: 'error', text: `Failed to load modules: ${e.message}` }); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const applyEntry = (entry) => {
    setModules((prev) => prev.map((m) => (m.id === entry.id ? entry : m)));
  };

  const toggle = async (mod, enabled) => {
    setBusyId(mod.id);
    setNotice(null);
    try {
      const { module } = await api.setModuleEnabled(mod.id, enabled);
      applyEntry(module);
    } catch (e) {
      setNotice({ kind: 'error', text: e.message });
    }
    setBusyId(null);
  };

  const remove = async (mod) => {
    if (!window.confirm(`Remove "${mod.name}"? Its folder is deleted from disk. Stories keep their data but the module stops running.`)) return;
    setBusyId(mod.id);
    setNotice(null);
    try {
      await api.removeModule(mod.id);
      setModules((prev) => prev.filter((m) => m.id !== mod.id));
      setNotice({ kind: 'ok', text: `Removed "${mod.name}".` });
    } catch (e) {
      setNotice({ kind: 'error', text: e.message });
    }
    setBusyId(null);
  };

  const finishInstall = (module) => {
    setModules((prev) => [...prev.filter((m) => m.id !== module.id), module].sort((a, b) => a.id.localeCompare(b.id)));
    setNotice({ kind: 'ok', text: `Installed "${module.name}" ${module.version}.` });
  };

  const handleZipFile = (e) => {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      setInstalling(true);
      setNotice(null);
      try {
        // readAsDataURL yields "data:...;base64,<payload>" — the payload is all we send.
        const dataBase64 = String(reader.result).split(',')[1] || '';
        const { module } = await api.installModuleZip(dataBase64, file.name);
        finishInstall(module);
      } catch (err) {
        setNotice({ kind: 'error', text: `Install failed: ${err.message}` });
      }
      setInstalling(false);
    };
    reader.readAsDataURL(file);
  };

  const handleGithubInstall = async () => {
    const url = githubUrl.trim();
    if (!url) return;
    setInstalling(true);
    setNotice(null);
    try {
      const { module } = await api.installModuleGithub(url);
      setGithubUrl('');
      finishInstall(module);
    } catch (err) {
      setNotice({ kind: 'error', text: `Install failed: ${err.message}` });
    }
    setInstalling(false);
  };

  const enabledCount = modules.filter((m) => m.enabled).length;

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center p-6">
      <div className="w-full max-w-3xl">
        <button
          onClick={onBack}
          className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-8"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Menu
        </button>

        <div className="mb-6">
          <h2 className="text-3xl font-bold text-gray-100 mb-2">Modules</h2>
          <p className="text-gray-500 text-sm">
            Enable and disable modules app-wide, or install new ones. A disabled module disappears
            everywhere — menus, story toggles, and the engine — until re-enabled. Per-story choices
            on the story start screen still apply within the enabled set.
          </p>
        </div>

        <div className="mb-6 p-4 rounded-lg border border-gray-700 bg-gray-800/50">
          <div className="flex flex-col sm:flex-row gap-3">
            <button
              onClick={() => fileInputRef.current?.click()}
              disabled={installing}
              className="px-4 py-2 rounded-lg bg-purple-700 hover:bg-purple-600 disabled:opacity-50 text-sm font-medium transition-colors whitespace-nowrap"
            >
              {installing ? 'Installing…' : '+ Install from zip'}
            </button>
            <input ref={fileInputRef} type="file" accept=".zip,application/zip" onChange={handleZipFile} className="hidden" />
            <div className="flex flex-1 gap-2">
              <input
                type="text"
                value={githubUrl}
                onChange={(e) => setGithubUrl(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter') handleGithubInstall(); }}
                placeholder="https://github.com/user/repo (or /tree/branch/path link)"
                disabled={installing}
                className="flex-1 bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-gray-200 disabled:opacity-50"
                aria-label="GitHub repository URL"
              />
              <button
                onClick={handleGithubInstall}
                disabled={installing || !githubUrl.trim()}
                className="px-4 py-2 rounded-lg border border-gray-600 hover:bg-gray-700 disabled:opacity-50 text-sm text-gray-200 transition-colors whitespace-nowrap"
              >
                Install
              </button>
            </div>
          </div>
          <p className="text-xs text-gray-600 mt-2">
            ⚠️ A module's backend runs with the same permissions as the app itself — only install modules you trust.
          </p>
        </div>

        {notice && (
          <div className={`mb-4 px-4 py-3 rounded-lg border text-sm ${
            notice.kind === 'ok'
              ? 'border-green-800 bg-green-900/30 text-green-300'
              : 'border-red-800 bg-red-900/30 text-red-300'
          }`}>
            {notice.text}
          </div>
        )}

        {loading ? (
          <div className="text-gray-500 text-center py-12">Loading...</div>
        ) : modules.length === 0 ? (
          <p className="text-gray-500 text-center py-12 border border-dashed border-gray-700 rounded-lg">
            No modules found.
          </p>
        ) : (
          <>
            <p className="text-xs text-gray-600 mb-2">{enabledCount}/{modules.length} enabled</p>
            <div className="space-y-2">
              {modules.map((m) => (
                <div key={m.id} className="p-4 rounded-lg border border-gray-700 bg-gray-800/50">
                  <div className="flex items-center justify-between gap-4">
                    <div className="flex items-center gap-3 min-w-0">
                      <span className="text-xl shrink-0">{m.icon || '🧩'}</span>
                      <div className="min-w-0">
                        <div className="flex items-center gap-2 flex-wrap">
                          <h4 className="font-medium text-gray-200">{m.name}</h4>
                          <span className="text-xs text-gray-500">{m.version}</span>
                          <span className={`text-[10px] px-1.5 py-0.5 rounded font-medium ${
                            m.builtin ? 'bg-gray-700 text-gray-400' : 'bg-purple-900/60 text-purple-300'
                          }`}>
                            {m.builtin ? 'BUILT-IN' : 'INSTALLED'}
                          </span>
                        </div>
                        {m.description && (
                          <p className="text-xs text-gray-500 mt-0.5">{m.description}</p>
                        )}
                        {(m.dependencies || []).length > 0 && (
                          <p className="text-xs text-gray-600 mt-0.5">requires: {m.dependencies.join(', ')}</p>
                        )}
                        {m.load_error && (
                          <p className="text-xs text-amber-400 mt-0.5">{m.load_error}</p>
                        )}
                      </div>
                    </div>

                    <div className="flex items-center gap-3 shrink-0">
                      {!m.builtin && (
                        <button
                          onClick={() => remove(m)}
                          disabled={busyId === m.id || installing}
                          className="text-xs text-gray-500 hover:text-red-400 disabled:opacity-50 transition-colors"
                          title="Delete this module from disk"
                        >
                          Remove
                        </button>
                      )}
                      <button
                        onClick={() => toggle(m, !m.enabled)}
                        disabled={busyId === m.id || installing}
                        className={`shrink-0 w-9 h-5 rounded-full relative transition-colors disabled:opacity-50 ${
                          m.enabled ? 'bg-purple-600' : 'bg-gray-600'
                        }`}
                        title={m.enabled ? 'Disable app-wide' : 'Enable app-wide'}
                        aria-label={`${m.enabled ? 'Disable' : 'Enable'} ${m.name}`}
                      >
                        <span
                          className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-all ${
                            m.enabled ? 'left-[1.125rem]' : 'left-0.5'
                          }`}
                        />
                      </button>
                    </div>
                  </div>
                </div>
              ))}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

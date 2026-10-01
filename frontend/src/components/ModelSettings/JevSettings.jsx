import { useEffect, useState } from 'react';

async function request(path, method = 'GET', body) {
  const response = await fetch(`/api/jev/${path}`, {
    method, headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || 'Could not update Jev settings.');
  return data;
}

export default function JevSettings() {
  const [config, setConfig] = useState(null);
  const [key, setKey] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  useEffect(() => {
    request('config').then(setConfig).catch(e => setMessage(e.message));
  }, []);

  async function save(remove = false) {
    setBusy(true); setMessage('');
    try {
      const settings = { enabled: config.enabled, rpg_enabled: config.rpg_enabled,
        npc_enabled: config.npc_enabled, model: config.model };
      const next = await request('config', 'PUT', {
        ...settings, ...(key ? { api_key: key } : {}),
        ...(remove ? { remove_api_key: true } : {}),
      });
      setConfig(next); setKey(''); setMessage('Jev settings saved.');
    } catch (e) { setMessage(e.message); }
    finally { setBusy(false); }
  }

  async function test() {
    setBusy(true); setMessage('Checking connection…');
    try {
      const result = await request('test', 'POST');
      setMessage(result.success ? `Connected: ${result.model}` : `Connection failed: ${result.message}`);
    } catch (e) { setMessage(e.message); }
    finally { setBusy(false); }
  }

  return <section className="mb-6 p-4 rounded-xl bg-gray-800/50 border border-gray-700 space-y-3" aria-label="Jev decisions">
    <h3 className="text-lg font-semibold text-gray-100">Jev decisions</h3>
    <p className="text-sm text-gray-400">Speed up RPG rulings and NPC decisions. Uncertain answers use your existing models. Storytelling keeps its current model.</p>
    <p className="text-xs text-gray-500">Optional TypeSafe connection, independent of the provider below. NSFW mode keeps using its selected model.</p>
    {config && <>
      <div className="flex flex-wrap gap-5 text-sm text-gray-200">
        {[['enabled', 'Enable Jev'], ['rpg_enabled', 'RPG decisions'], ['npc_enabled', 'NPC decisions']].map(([field, label]) =>
          <label key={field} className="flex items-center gap-2"><input type="checkbox" checked={config[field]} disabled={busy}
            onChange={e => setConfig({ ...config, [field]: e.target.checked })} />{label}</label>)}
      </div>
      <label className="block text-sm text-gray-300">TypeSafe API key
        <input type="password" autoComplete="new-password" value={key} disabled={busy} onChange={e => setKey(e.target.value)}
          placeholder={config.api_key_set ? 'Key saved — enter a replacement' : 'Enter your TypeSafe API key'}
          className="block mt-1 w-full bg-gray-900 border border-gray-700 rounded px-3 py-2 text-gray-200" />
      </label>
      <div className="flex flex-wrap gap-2">
        <button disabled={busy} onClick={() => save()} className="px-3 py-2 bg-purple-600 rounded text-sm disabled:opacity-40">Save Jev settings</button>
        <button disabled={busy || !config.api_key_set || !!key} onClick={test} className="px-3 py-2 bg-gray-700 rounded text-sm disabled:opacity-40">Test saved key</button>
        {config.api_key_set && <button disabled={busy} onClick={() => save(true)} className="px-3 py-2 bg-gray-700 rounded text-sm disabled:opacity-40">Remove key</button>}
      </div>
      <p className="text-xs text-gray-500">Default XP rules use Jev; custom XP rules use your existing judge. A connection test makes one small paid request.</p>
    </>}
    {message && <p role="status" className="text-sm text-gray-300">{message}</p>}
  </section>;
}

// localStorage, namespaced by the backend's active data root ("profile").
// Demo mode and future user profiles run against their own data root and
// must not read or clobber the default profile's browser-side state (UI
// restore state, chat/character drafts, onboarding flag). The namespace is
// resolved from /api/health before React mounts (see main.jsx), so every
// component-level read already sees the right keys. The "default" profile
// maps to bare, un-prefixed keys — existing browsers keep their state.
let prefix = '';

export function initStorageNamespace(profileId) {
  prefix = profileId && profileId !== 'default' ? `${profileId}:` : '';
}

export const storage = {
  getItem: (key) => localStorage.getItem(prefix + key),
  setItem: (key, value) => localStorage.setItem(prefix + key, value),
  removeItem: (key) => localStorage.removeItem(prefix + key),
};

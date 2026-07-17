import React, { useState, useEffect } from 'react';
import { api } from 'api';
import WorldListScreen from './WorldBuilder/WorldListScreen';
import WorldBuilderWizard from './WorldBuilder/WorldBuilderWizard';
import WorldReviewScreen from './WorldBuilder/WorldReviewScreen';

// Entry screen for the "World Generation" module mode. Owns the list/create/
// review navigation that used to live in App.jsx as separate top-level modes.
export default function WorldGenScreen({ onBack }) {
  const [view, setView] = useState('list'); // 'list' | 'create' | 'review'
  const [reviewWorldId, setReviewWorldId] = useState(null);
  const [wizardKey, setWizardKey] = useState(0);

  // Any live session — a generation still running server-side, or unsaved
  // work sitting in memory (it may have finished while the app was minimized;
  // Android kills the backgrounded PWA) — drops straight back into the
  // wizard, which restores the session, shows every step generated so far,
  // and follows a still-running run via polling. The list stays one "Exit"
  // tap away; only sessions with no work at all land on it directly.
  useEffect(() => {
    api.getWorldState().then((d) => {
      const st = d.state;
      if (st?._generating || Object.keys(st?.steps || {}).length > 0) {
        setView((v) => (v === 'list' ? 'create' : v));
      }
    }).catch(() => {});
  }, []);

  if (view === 'create') {
    return (
      <WorldBuilderWizard
        key={wizardKey}
        onBack={() => setView('list')}
        onWorldCreated={() => setView('list')}
      />
    );
  }

  if (view === 'review') {
    return (
      <WorldReviewScreen
        worldId={reviewWorldId}
        onBack={() => setView('list')}
      />
    );
  }

  return (
    <WorldListScreen
      onBack={onBack}
      onOpenWorld={(id, resume = false) => {
        if (id) {
          if (resume) {
            api.resumeWorld(id).then(() => {
              setWizardKey((k) => k + 1);
              setView('create');
            }).catch((e) => alert('Failed to resume: ' + e.message));
          } else {
            setReviewWorldId(id);
            setView('review');
          }
        } else {
          api.discardWorld();
          setWizardKey((k) => k + 1);
          setView('create');
        }
      }}
    />
  );
}

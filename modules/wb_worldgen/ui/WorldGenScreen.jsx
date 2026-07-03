import React, { useState } from 'react';
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

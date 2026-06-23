import { useState, useEffect, useCallback, useRef } from 'react';
import { ModuleEventProvider } from './hooks/useModuleEventBus';
import { useWebSocket } from './hooks/useWebSocket';
import { useSession } from './hooks/useSession';
import { useModules } from './hooks/useModules';
import Header from './components/Header/Header';
import Sidebar from './components/Sidebar/Sidebar';
import { ChatFeed } from './components/Chat/ChatFeed';
import ChatInput from './components/Chat/ChatInput';
import SlotRenderer from './components/Slots/SlotRenderer';
import SettingsModal from './SettingsModal';
import PromptStudio from './PromptStudio';
import HealthPanel from './components/Header/HealthPanel';
import MemoryBrowser from './components/MemoryBrowser';
import GameMapOverlay from './components/GameMapOverlay';
import MainMenu from './components/Menu/MainMenu';
import SaveSelectScreen from './components/Menu/SaveSelectScreen';
import ExitWarning from './components/Menu/ExitWarning';
import WorldBuilderWizard from './components/WorldBuilder/WorldBuilderWizard';
import WorldListScreen from './components/WorldBuilder/WorldListScreen';
import WorldReviewScreen from './components/WorldBuilder/WorldReviewScreen';
import CharacterListScreen from './components/CharacterBuilder/CharacterListScreen';
import CharacterCreator from './components/CharacterBuilder/CharacterCreator';
import ModelSettings from './components/ModelSettings/ModelSettings';
import { LLMInspectorProvider, useLLMInspector } from './hooks/useLLMInspector';
import LLMInspectorButton from './components/LLMInspector/LLMInspectorButton';
import LLMInspectorPanel from './components/LLMInspector/LLMInspectorPanel';
import { api } from './lib/api';
import './index.css';

function PlaceholderMode({ title, onBack }) {
  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col items-center justify-center p-6">
      <div className="w-full max-w-md">
        <button
          onClick={onBack}
          className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors mb-8"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Menu
        </button>
        <h2 className="text-3xl font-bold text-gray-100 mb-3">{title}</h2>
        <p className="text-gray-500">This feature is coming soon.</p>
      </div>
    </div>
  );
}

function AppContent() {
  const [currentMode, setCurrentMode] = useState(null);
  const [showExitWarning, setShowExitWarning] = useState(false);
  const [reviewWorldId, setReviewWorldId] = useState(null);
  const [editCharacterId, setEditCharacterId] = useState(null);
  const [editCharacterData, setEditCharacterData] = useState(null);
  const [gameState, setGameState] = useState({});
  const [wizardKey, setWizardKey] = useState(0);

  const handleStateFromServer = useCallback((state) => {
    setGameState(prev => ({
      ...prev,
      module_data: state.module_data || prev.module_data || {},
      module_configs: state.module_configs || prev.module_configs || {},
      characters: state.characters || prev.characters || {},
      turn: state.turn,
      world_data: state.world_data,
      player_location_node_id: state.player_location_node_id ?? prev.player_location_node_id,
      player_location_layer_id: state.player_location_layer_id ?? prev.player_location_layer_id,
      revealed_node_ids: state.revealed_node_ids ?? prev.revealed_node_ids ?? [],
    }));
  }, []);

  const { addCall } = useLLMInspector();
  const ws = useWebSocket(handleStateFromServer, addCall);
  const session = useSession();
  const { modules, setModules } = useModules();

  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [isHealthOpen, setIsHealthOpen] = useState(false);
  const [isMemoryOpen, setIsMemoryOpen] = useState(false);
  const sentIntroRef = useRef(false);

  const handleSend = useCallback((text) => {
    ws.sendMessage(text);
  }, [ws]);

  const handleSaveModuleConfigs = useCallback(async (nextConfigs) => {
    await session.updateModuleConfigs(nextConfigs);
    setGameState(prev => ({ ...prev, module_configs: nextConfigs }));
  }, [session]);

  const handleEnterGame = useCallback(async (saveId) => {
    await session.refreshSession();
    setCurrentMode('storyteller-game');
  }, [session]);

  const handleExitMode = useCallback(() => {
    if (currentMode === 'storyteller-game' && (session.sessionState?.turn ?? 0) > 0) {
      setShowExitWarning(true);
    } else {
      sentIntroRef.current = false;
      setCurrentMode(null);
    }
  }, [currentMode, session.sessionState]);

  const handleConfirmExit = useCallback(() => {
    setShowExitWarning(false);
    sentIntroRef.current = false;
    setCurrentMode(null);
  }, []);

  useEffect(() => {
    if (session.sessionState) {
      setGameState(prev => ({
        ...prev,
        module_configs: session.moduleConfigs,
      }));
    }
  }, [session.sessionState, session.moduleConfigs]);

  useEffect(() => {
    if (
      currentMode === 'storyteller-game' &&
      ws.isConnected &&
      !sentIntroRef.current
    ) {
      sentIntroRef.current = true;
      ws.sendIntro();
    }
  }, [currentMode, ws.isConnected]);

  if (currentMode === null) {
    return (
      <MainMenu
        onSelectMode={setCurrentMode}
        modules={modules}
        onModulesLoaded={setModules}
      />
    );
  }

  if (currentMode === 'storyteller-select') {
    return (
      <SaveSelectScreen
        onLoad={handleEnterGame}
        onCreate={handleEnterGame}
        onBack={() => setCurrentMode(null)}
      />
    );
  }

  if (currentMode === 'model-settings') {
    return (
      <ModelSettings
        onBack={() => setCurrentMode(null)}
      />
    );
  }

  if (currentMode === 'prompt-studio') {
    return <PromptStudioStandalone onBack={() => setCurrentMode(null)} />;
  }

  if (currentMode === 'world-building') {
    return (
      <WorldListScreen
        onBack={() => setCurrentMode(null)}
        onOpenWorld={(id, resume = false) => {
          if (id) {
            if (resume) {
              api.resumeWorld(id).then(() => {
                setWizardKey((k) => k + 1);
                setCurrentMode('world-create');
              }).catch((e) => alert('Failed to resume: ' + e.message));
            } else {
              setReviewWorldId(id);
              setCurrentMode('world-review');
            }
          } else {
            api.discardWorld();
            setWizardKey((k) => k + 1);
            setCurrentMode('world-create');
          }
        }}
      />
    );
  }

  if (currentMode === 'world-create') {
    return (
      <WorldBuilderWizard
        key={wizardKey}
        onBack={() => setCurrentMode('world-building')}
        onWorldCreated={() => setCurrentMode('world-building')}
      />
    );
  }

  if (currentMode === 'world-review') {
    return (
      <WorldReviewScreen
        worldId={reviewWorldId}
        onBack={() => setCurrentMode('world-building')}
      />
    );
  }

  if (currentMode === 'character-creator') {
    return (
      <CharacterListScreen
        onBack={() => setCurrentMode(null)}
        onOpenCharacter={(id) => {
          if (id) {
            setEditCharacterId(id);
            api.loadCharacter(id).then(data => {
              setEditCharacterData(data);
              setCurrentMode('character-create');
            }).catch(e => {
              alert('Failed to load character: ' + e.message);
            });
          } else {
            setEditCharacterId(null);
            setEditCharacterData(null);
            setCurrentMode('character-create');
          }
        }}
      />
    );
  }

  if (currentMode === 'character-create') {
    return (
      <CharacterCreator
        onBack={() => setCurrentMode('character-creator')}
        onSaved={() => setCurrentMode('character-creator')}
        editCharacterId={editCharacterId}
        initialData={editCharacterData}
      />
    );
  }

  if (currentMode === 'storyteller-game') {
    return (
    <ModuleEventProvider>
      <div className="flex h-screen bg-gray-900 text-gray-100 font-sans overflow-hidden">
        <Sidebar
          session={session}
          modules={modules}
          gameState={gameState}
        />

        <div className="flex-1 flex flex-col min-w-0">
          <Header
            modules={modules}
            gameState={gameState}
            ws={ws}
            session={session}
            onOpenSettings={() => setIsSettingsOpen(true)}
            onOpenHealth={() => setIsHealthOpen(true)}
            onOpenMemories={() => setIsMemoryOpen(true)}
            onBack={handleExitMode}
          />

          <ChatFeed
            messages={ws.messages}
            currentStream={ws.currentStream}
          />

          <ChatInput
            onSend={handleSend}
            disabled={!ws.isConnected || ws.isReconnecting}
          />
        </div>

        <SettingsModal
          isOpen={isSettingsOpen}
          onClose={() => setIsSettingsOpen(false)}
          modules={modules}
          moduleConfigs={session.moduleConfigs}
          onSaveModuleConfigs={handleSaveModuleConfigs}
          gameState={gameState}
        />

        <HealthPanel
          isOpen={isHealthOpen}
          onClose={() => setIsHealthOpen(false)}
        />

        <MemoryBrowser
          isOpen={isMemoryOpen}
          onClose={() => setIsMemoryOpen(false)}
        />

        {gameState.world_data && (
          <GameMapOverlay
            worldData={gameState.world_data}
            playerNodeId={gameState.player_location_node_id}
            playerLayerId={gameState.player_location_layer_id}
            revealedNodeIds={gameState.revealed_node_ids || []}
          />
        )}
      </div>

      {showExitWarning && (
        <ExitWarning
          onConfirm={handleConfirmExit}
          onCancel={() => setShowExitWarning(false)}
        />
      )}
    </ModuleEventProvider>
    );
  }
}

function PromptStudioStandalone({ onBack }) {
  const [pipeline, setPipeline] = useState([]);
  const [modules, setModules] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      api.getGlobalPromptPipeline(),
      api.getModules(),
    ]).then(([pipeData, modData]) => {
      setPipeline(pipeData.prompt_pipeline || []);
      setModules(modData.modules || []);
    }).catch(() => {}).finally(() => setLoading(false));
  }, []);

  const handleSave = useCallback(async (nextPipeline) => {
    const data = await api.updateGlobalPromptPipeline(nextPipeline);
    setPipeline(data.prompt_pipeline || []);
  }, []);

  const handleRefresh = useCallback(async () => {
    const data = await api.getGlobalPromptPipeline();
    setPipeline(data.prompt_pipeline || []);
  }, []);

  if (loading) {
    return (
      <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex items-center justify-center">
        <p className="text-gray-400">Loading Prompt Studio...</p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950 flex flex-col">
      <div className="p-4 border-b border-gray-700 bg-gray-900">
        <button
          onClick={onBack}
          className="flex items-center gap-2 text-gray-400 hover:text-gray-200 transition-colors"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
          Back to Menu
        </button>
      </div>
      <div className="flex-1 overflow-hidden">
        <PromptStudio
          isOpen={true}
          onClose={onBack}
          modules={modules}
          promptPipeline={pipeline}
          promptTrace={[]}
          onSave={handleSave}
          onPreview={async () => ({ messages: [], trace: [] })}
          onRefresh={handleRefresh}
          standalone={true}
        />
      </div>
    </div>
  );
}

export default function App() {
  return (
    <LLMInspectorProvider>
      <AppContent />
      <LLMInspectorButton />
      <LLMInspectorPanel />
    </LLMInspectorProvider>
  );
}

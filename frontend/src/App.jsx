import { useState, useEffect, useCallback, useRef } from 'react';
import { ModuleEventProvider } from './hooks/useModuleEventBus';
import { useWebSocket } from './hooks/useWebSocket';
import { useSession } from './hooks/useSession';
import { useModules } from './hooks/useModules';
import Header from './components/Header/Header';
import Sidebar from './components/Sidebar/Sidebar';
import { ChatFeed } from './components/Chat/ChatFeed';
import ChatInput from './components/Chat/ChatInput';
import CommandResultModal from './components/Chat/CommandResultModal';
import SlotRenderer from './components/Slots/SlotRenderer';
import SettingsModal from './SettingsModal';
import PromptStudio from './PromptStudio';
import HealthPanel from './components/Header/HealthPanel';
import MemoryBrowser from './components/MemoryBrowser';
import CharacterView from './components/CharacterView/CharacterView';
import ModuleGameOverlay from './components/shared/ModuleGameOverlay';
import MainMenu from './components/Menu/MainMenu';
import OnboardingWizard from './components/Onboarding/OnboardingWizard';
import SaveSelectScreen from './components/Menu/SaveSelectScreen';
import ExitWarning from './components/Menu/ExitWarning';
import CharacterListScreen from './components/CharacterBuilder/CharacterListScreen';
import CharacterCreator from './components/CharacterBuilder/CharacterCreator';
import SettingsScreen from './components/Settings/SettingsScreen';
import ModuleScreen from './components/shared/ModuleScreen';
import ScenarioManager from './components/Scenario/ScenarioManager';
import LorebookManager from './components/Lorebook/LorebookManager';
import { useToasts, ToastStack } from './components/shared/Toasts';
import { LLMInspectorProvider, useLLMInspector } from './hooks/useLLMInspector';
import { ThemeProvider, useTheme } from './hooks/useTheme';
import { useMediaQuery } from './hooks/useMediaQuery';
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

const ONBOARDING_DONE_KEY = 'wb_onboarding_done';

function AppContent() {
  const [currentMode, setCurrentMode] = useState(null);
  // First-launch onboarding: null = checking, true = show wizard, false = no.
  // Shown once, only when no AI provider is configured anywhere (fresh
  // install); a returning user whose key broke gets the menu card instead.
  const [showOnboarding, setShowOnboarding] = useState(() =>
    localStorage.getItem(ONBOARDING_DONE_KEY) ? false : null
  );
  const [showExitWarning, setShowExitWarning] = useState(false);
  const [editCharacterId, setEditCharacterId] = useState(null);
  const [editCharacterData, setEditCharacterData] = useState(null);
  const [gameState, setGameState] = useState({});

  const handleStateFromServer = useCallback((state) => {
    setGameState(prev => ({
      ...prev,
      module_data: state.module_data || prev.module_data || {},
      module_configs: state.module_configs || prev.module_configs || {},
      characters: state.characters || prev.characters || {},
      active_save_id: state.active_save_id ?? prev.active_save_id,
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
  const { toasts, showToast, dismissToast } = useToasts();
  const { density } = useTheme();
  const [editRequest, setEditRequest] = useState(null);

  const [isSettingsOpen, setIsSettingsOpen] = useState(false);
  const [isHealthOpen, setIsHealthOpen] = useState(false);
  const [isMemoryOpen, setIsMemoryOpen] = useState(false);
  const [isCharacterOpen, setIsCharacterOpen] = useState(false);
  const sentIntroRef = useRef(false);

  // Mobile-only chrome behavior: the sidebar drawer trigger lives in the
  // header, and the header hides on scroll-down / reveals on scroll-up.
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [headerHidden, setHeaderHidden] = useState(false);
  const isMobile = useMediaQuery('(max-width: 1023px)'); // matches Tailwind lg
  const isMobileRef = useRef(isMobile);
  isMobileRef.current = isMobile;
  const feedScrollRef = useRef(null);

  // Chrome-style header: user scrolls (programmatic streaming scrolls are
  // filtered out upstream in useStickToBottom) hide it going down and reveal
  // it going up; near the top it's always shown. Small dead zone kills jitter.
  const handleFeedUserScroll = useCallback((delta, el) => {
    if (!isMobileRef.current) return;
    if (el.scrollTop < 64) { setHeaderHidden(false); return; }
    if (delta > 6) setHeaderHidden(true);
    else if (delta < -6) setHeaderHidden(false);
  }, []);

  // Focusing the composer on a phone brings the story back into view. The
  // second scroll compensates for the on-screen keyboard resizing the
  // viewport shortly after focus.
  const handleComposerFocus = useCallback(() => {
    if (!isMobileRef.current) return;
    feedScrollRef.current?.scrollToBottom();
    setTimeout(() => feedScrollRef.current?.scrollToBottom(), 300);
  }, []);

  const handleOpenDrawer = useCallback(() => {
    setHeaderHidden(false);
    setDrawerOpen(true);
  }, []);

  const handleSend = useCallback((text) => {
    // A recognized slash command (active module + declared command) is dispatched
    // as a command — its result pops up instead of entering the story feed.
    // Unrecognized "/…" text falls through as a normal turn.
    const first = text.trim().split(/\s+/)[0]?.toLowerCase();
    const active = session.moduleConfigs?.__active_modules__;
    const activeSet = Array.isArray(active) ? new Set(active) : null;
    const isCommand = !!first && first.startsWith('/') && (modules || []).some(
      (m) => (!activeSet || activeSet.has(m.id))
        && Object.prototype.hasOwnProperty.call(m.commands || {}, first)
    );
    if (isCommand) ws.sendCommand(text);
    else ws.sendMessage(text);
  }, [ws, modules, session.moduleConfigs]);

  const handleContinue = useCallback(() => {
    ws.sendContinue();
  }, [ws]);

  const handleRegenerate = useCallback(() => {
    ws.sendRegenerate();
  }, [ws]);

  const handleStop = useCallback(() => {
    ws.sendStop();
  }, [ws]);

  // ArrowUp in the empty composer edits the player's most recent message.
  const handleEditLast = useCallback(() => {
    const msgs = ws.messages;
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === 'user') {
        setEditRequest({ index: i, at: Date.now() });
        return;
      }
    }
  }, [ws.messages]);

  // Fork the story at a given turn and jump into the branch: the server copies
  // the save (rolled back to that turn), we load it and replay its transcript.
  const handleBranchMessage = useCallback(async (turn) => {
    const saveId = session.sessionState?.active_save_id;
    if (!saveId) return;
    try {
      const r = await api.branchSave(saveId, { targetTurn: turn });
      await session.loadSave(r.branch.id);
      ws.sendIntro();
      showToast(`Branched into "${r.branch.display_name || r.branch.id}"`, 'info');
    } catch (e) {
      showToast(`Failed to branch: ${e.message}`);
    }
  }, [session, ws, showToast]);

  const handleSwipe = useCallback(async (index) => {
    try {
      const r = await api.selectSwipe(index);
      ws.applyServerState(r.state);
      session.refreshSession();
    } catch (e) { showToast(`Failed to switch variant: ${e.message}`); }
  }, [ws, session, showToast]);

  // Keyboard swipes from the empty composer: ←/→ walk the last turn's
  // variants; → past the newest one regenerates (same as the swipe buttons).
  const handleSwipePrev = useCallback(() => {
    const s = ws.swipes;
    if (s && s.active > 0) handleSwipe(s.active - 1);
  }, [ws.swipes, handleSwipe]);

  const handleSwipeNext = useCallback(() => {
    const s = ws.swipes;
    if (!s) return;
    if (s.active < s.count - 1) handleSwipe(s.active + 1);
    else ws.sendRegenerate();
  }, [ws, handleSwipe]);

  const handleEditMessage = useCallback(async (index, content) => {
    try {
      const r = await api.editMessage(index, content);
      ws.applyServerState(r.state);
      session.refreshSession();
    } catch (e) { showToast(`Failed to edit: ${e.message}`); }
  }, [ws, session, showToast]);

  const handleDeleteMessage = useCallback(async (index) => {
    try {
      const r = await api.deleteMessage(index);
      ws.applyServerState(r.state);
      session.refreshSession();
    } catch (e) { showToast(`Failed to delete: ${e.message}`); }
  }, [ws, session, showToast]);

  const handleSaveModuleConfigs = useCallback(async (nextConfigs) => {
    await session.updateModuleConfigs(nextConfigs);
    setGameState(prev => ({ ...prev, module_configs: nextConfigs }));
  }, [session]);

  const handleEnterGame = useCallback(async (saveId) => {
    await session.refreshSession();
    setHeaderHidden(false);
    setCurrentMode('storyteller-game');
  }, [session]);

  // Completed turns are autosaved server-side, so exiting while idle loses
  // nothing; only an in-flight generation is at risk (it never gets saved if
  // interrupted), so that's the only case that warrants a warning.
  const generating = ws.currentStream != null || ws.postProcessing;

  const handleExitMode = useCallback(() => {
    if (currentMode === 'storyteller-game' && generating) {
      setShowExitWarning(true);
    } else {
      sentIntroRef.current = false;
      setCurrentMode(null);
    }
  }, [currentMode, generating]);

  const handleConfirmExit = useCallback(() => {
    // Abandon the in-flight turn; otherwise the server keeps generating into
    // the story after we've left and re-entering hits a "busy" error.
    ws.sendStop();
    setShowExitWarning(false);
    sentIntroRef.current = false;
    setCurrentMode(null);
  }, [ws]);

  // If the turn finishes while the warning is up, the risk is gone: close it.
  // The next exit tap leaves silently since everything is saved.
  useEffect(() => {
    if (showExitWarning && !generating) setShowExitWarning(false);
  }, [showExitWarning, generating]);

  useEffect(() => {
    if (showOnboarding !== null) return;
    api.getHealth()
      .then((health) => setShowOnboarding(health.status === 'missing_api_key'))
      .catch(() => setShowOnboarding(false));
  }, [showOnboarding]);

  const handleFinishOnboarding = useCallback((nextMode) => {
    localStorage.setItem(ONBOARDING_DONE_KEY, '1');
    setShowOnboarding(false);
    if (nextMode) setCurrentMode(nextMode);
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
      // Clear any messages left over from a previously opened story before the
      // intro arrives. Existing stories get replaced via `state_load`, but a new
      // story streams its opening and would otherwise append to the stale list.
      ws.setMessages([]);
      ws.sendIntro();
    }
  }, [currentMode, ws.isConnected]);

  if (currentMode === null) {
    // Hold the menu back until the first-launch check resolves (fast, local)
    // so a fresh install doesn't flash the menu before the wizard.
    if (showOnboarding === null) {
      return <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950" />;
    }
    if (showOnboarding) {
      return <OnboardingWizard onFinish={handleFinishOnboarding} />;
    }
    return (
      <MainMenu
        onSelectMode={setCurrentMode}
        modules={modules}
        onModulesLoaded={setModules}
      />
    );
  }

  // Module-contributed full-screen modes: "module:{modId}:{modeId}".
  if (typeof currentMode === 'string' && currentMode.startsWith('module:')) {
    const [, modId, modeId] = currentMode.split(':');
    const mod = (modules || []).find((m) => m.id === modId);
    const mode = (mod?.modes || []).find((md) => md.id === modeId);
    if (mod && mode?.screen) {
      return (
        <ModuleScreen
          modId={modId}
          screen={mode.screen}
          onBack={() => setCurrentMode(null)}
        />
      );
    }
    return <PlaceholderMode title={mode?.label || 'Module'} onBack={() => setCurrentMode(null)} />;
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

  // 'settings' is the unified Model + Appearance screen. 'model-settings' is kept
  // as an alias so any older navigation still lands on the same place.
  if (currentMode === 'settings' || currentMode === 'model-settings') {
    return (
      <SettingsScreen
        onBack={() => setCurrentMode(null)}
      />
    );
  }

  if (currentMode === 'prompt-studio') {
    return <PromptStudioStandalone onBack={() => setCurrentMode(null)} />;
  }

  if (currentMode === 'scenario-manager') {
    return <ScenarioManager onBack={() => setCurrentMode(null)} />;
  }

  if (currentMode === 'lorebook-manager') {
    return <LorebookManager onBack={() => setCurrentMode(null)} />;
  }

  if (currentMode === 'character-creator') {
    return (
      <>
        <CharacterListScreen
          onBack={() => setCurrentMode(null)}
          onOpenCharacter={(id) => {
            if (id) {
              setEditCharacterId(id);
              api.loadCharacter(id).then(data => {
                setEditCharacterData(data);
                setCurrentMode('character-create');
              }).catch(e => {
                showToast('Failed to load character: ' + e.message);
              });
            } else {
              setEditCharacterId(null);
              setEditCharacterData(null);
              setCurrentMode('character-create');
            }
          }}
        />
        <ToastStack toasts={toasts} onDismiss={dismissToast} />
      </>
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
    // A save restricts which modules are active (chosen at story start / edited
    // later via the cog). Filter the UI to that set so disabled modules
    // contribute no sidebar/header/settings/overlay UI. Legacy saves without the
    // reserved key get all modules.
    const activeModuleIds = session.moduleConfigs?.__active_modules__;
    const gameModules = Array.isArray(activeModuleIds)
      ? (modules || []).filter((m) => activeModuleIds.includes(m.id))
      : modules;
    // Flatten the active modules' declared slash commands for composer autocomplete.
    const slashCommands = (gameModules || [])
      .flatMap((m) =>
        Object.keys(m.commands || {}).map((cmd) => ({
          command: cmd,
          icon: m.icon,
          module: m.name,
          description: (m.command_help || {})[cmd] || m.name,
        }))
      )
      .sort((a, b) => a.command.localeCompare(b.command));
    return (
    <ModuleEventProvider>
      <div className="flex h-dvh bg-gray-900 text-gray-100 font-sans overflow-hidden">
        <Sidebar
          session={session}
          modules={gameModules}
          gameState={gameState}
          drawerOpen={drawerOpen}
          onCloseDrawer={() => setDrawerOpen(false)}
          onOpenSettings={() => setIsSettingsOpen(true)}
          onOpenHealth={() => setIsHealthOpen(true)}
          onOpenMemories={() => setIsMemoryOpen(true)}
          onOpenCharacter={() => setIsCharacterOpen(true)}
        />

        <div className="flex-1 flex flex-col min-w-0">
          <Header
            modules={gameModules}
            gameState={gameState}
            ws={ws}
            session={session}
            onOpenSettings={() => setIsSettingsOpen(true)}
            onOpenHealth={() => setIsHealthOpen(true)}
            onOpenMemories={() => setIsMemoryOpen(true)}
            onOpenCharacter={() => setIsCharacterOpen(true)}
            onBack={handleExitMode}
            onOpenDrawer={handleOpenDrawer}
            hidden={headerHidden}
          />

          <ChatFeed
            messages={ws.messages}
            currentStream={ws.currentStream}
            currentReasoning={ws.currentReasoning}
            swipes={ws.swipes}
            busy={ws.currentStream != null || ws.postProcessing}
            postProcessing={ws.postProcessing}
            pipelineStatus={ws.pipelineStatus}
            editRequest={editRequest}
            currentTurn={gameState.turn ?? null}
            density={density}
            scrollControlRef={feedScrollRef}
            onUserScroll={handleFeedUserScroll}
            onBranchMessage={handleBranchMessage}
            onRegenerate={handleRegenerate}
            onSwipe={handleSwipe}
            onEditMessage={handleEditMessage}
            onDeleteMessage={handleDeleteMessage}
            modules={gameModules}
            slotState={gameState}
            moduleConfigs={gameState?.module_configs}
          />

          {(!ws.isConnected || ws.isReconnecting) && (
            <div className="flex items-center justify-center gap-2 px-4 py-1.5 bg-red-950/60 border-t border-red-900/60 text-xs text-red-200">
              <span className="w-2 h-2 rounded-full bg-red-400 animate-pulse" />
              Connection lost — reconnecting…
            </div>
          )}

          <ChatInput
            commands={slashCommands}
            onSend={handleSend}
            onContinue={handleContinue}
            onStop={handleStop}
            onEditLast={handleEditLast}
            onSwipePrev={handleSwipePrev}
            onSwipeNext={handleSwipeNext}
            onComposerFocus={handleComposerFocus}
            restoredInput={ws.restoredInput}
            busy={ws.currentStream != null || ws.postProcessing}
            disabled={!ws.isConnected || ws.isReconnecting}
          />
        </div>

        <CommandResultModal result={ws.commandResult} onClose={ws.clearCommandResult} />

        <SettingsModal
          isOpen={isSettingsOpen}
          onClose={() => setIsSettingsOpen(false)}
          modules={gameModules}
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
          saveId={session.sessionState?.active_save_id}
        />

        <CharacterView
          isOpen={isCharacterOpen}
          onClose={() => setIsCharacterOpen(false)}
          modules={gameModules}
          gameState={gameState}
          onCommand={handleSend}
          busy={generating}
        />

        {(gameModules || [])
          .filter((m) => m.game_overlay)
          .map((m) => (
            <ModuleGameOverlay
              key={m.id}
              modId={m.id}
              file={m.game_overlay}
              state={gameState}
            />
          ))}
      </div>

      {showExitWarning && (
        <ExitWarning
          savedTurn={gameState.turn ?? session.sessionState?.turn ?? 0}
          onConfirm={handleConfirmExit}
          onCancel={() => setShowExitWarning(false)}
        />
      )}

      <ToastStack toasts={toasts} onDismiss={dismissToast} />
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
    <ThemeProvider>
      <LLMInspectorProvider>
        <AppContent />
        <LLMInspectorButton />
        <LLMInspectorPanel />
      </LLMInspectorProvider>
    </ThemeProvider>
  );
}

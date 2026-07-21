import { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { storage } from './lib/storage';
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
import BranchNameDialog from './components/shared/BranchNameDialog';
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
import ServerLogScreen from './components/Logs/ServerLogScreen';
import { useToasts, ToastStack } from './components/shared/Toasts';
import { LLMInspectorProvider, useLLMInspectorActions } from './hooks/useLLMInspector';
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
// The screen that was open when the page last unloaded, kept in localStorage
// so the next launch can drop the player straight back in. Android kills the
// backgrounded PWA under memory pressure, and relaunching it from the home
// screen is a brand-new browsing session (sessionStorage comes up empty) that
// would otherwise reset to the main menu. Shape: { mode, saveId?,
// editCharacterId? }; removed while on the menu, so a deliberate exit still
// lands there.
const UI_STATE_KEY = 'wb_ui_state';

// Screens that restore by just setting the mode again: they carry no client
// context and (re)fetch everything they show from the server on mount.
// 'storyteller-game' and 'character-create' are handled separately — they
// need an async load before the screen can paint. Module full-screen modes
// ('module:{modId}:{modeId}') are also plain restores.
const PLAIN_MODES = new Set([
  'storyteller-select',
  'settings',
  'model-settings',
  'prompt-studio',
  'scenario-manager',
  'lorebook-manager',
  'character-creator',
  'server-logs',
]);

// Read and validate the persisted UI state; anything unrecognized (corrupt
// JSON, a mode this build no longer has, a game marker without a save id)
// restores to the menu instead of a blank screen.
function readSavedUiState() {
  try {
    const saved = JSON.parse(storage.getItem(UI_STATE_KEY) || 'null');
    const mode = saved?.mode;
    if (typeof mode !== 'string') return null;
    if (mode === 'storyteller-game') return saved.saveId ? saved : null;
    if (mode === 'character-create') return saved.editCharacterId ? saved : null;
    if (PLAIN_MODES.has(mode) || mode.startsWith('module:')) return { mode };
    return null;
  } catch {
    return null;
  }
}

function AppContent() {
  // Captured before any effect can overwrite it: the screen that was open
  // when the page last unloaded (see UI_STATE_KEY). Plain screens restore
  // synchronously below; the game and the character editor need data loaded
  // first, so they go through the async-restore effect while `resuming`
  // holds the menu back.
  const [savedUiState] = useState(readSavedUiState);
  const asyncRestore =
    savedUiState?.mode === 'storyteller-game' || savedUiState?.mode === 'character-create'
      ? savedUiState
      : null;
  const [currentMode, setCurrentMode] = useState(asyncRestore ? null : savedUiState?.mode ?? null);
  // First-launch onboarding: null = checking, true = show wizard, false = no.
  // Shown once, only when no AI provider is configured anywhere (fresh
  // install); a returning user whose key broke gets the menu card instead.
  const [showOnboarding, setShowOnboarding] = useState(() =>
    storage.getItem(ONBOARDING_DONE_KEY) ? false : null
  );
  const [showExitWarning, setShowExitWarning] = useState(false);
  const [editCharacterId, setEditCharacterId] = useState(null);
  const [editCharacterData, setEditCharacterData] = useState(null);
  const [gameState, setGameState] = useState({});
  const [resuming, setResuming] = useState(asyncRestore != null);

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
      player_location_map_id: state.player_location_map_id ?? prev.player_location_map_id,
      player_location_layer_id: state.player_location_layer_id ?? prev.player_location_layer_id,
      revealed_node_ids: state.revealed_node_ids ?? prev.revealed_node_ids ?? [],
    }));
  }, []);

  // Actions-only subscription: new LLM calls must not re-render the whole app
  // (that made the storyteller feed "refresh" and drop text selections).
  const { addCall } = useLLMInspectorActions();
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
  // The full engine state returned by the load/branch REST call, stashed so the
  // intro effect can paint the transcript instantly instead of waiting for the
  // server's (slow) intro round-trip. Consumed once, then cleared.
  const pendingSeedRef = useRef(null);

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

  const handleSend = useCallback((text, opts) => {
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
    if (isCommand) ws.sendCommand(text, opts);
    else ws.sendMessage(text);
  }, [ws, modules, session.moduleConfigs]);

  // Commands dispatched by module UI buttons (sidebar widgets, character
  // tabs). The widget already reflects the outcome via state_update, so the
  // server skips the result popup for these unless the command failed.
  const handleButtonCommand = useCallback(
    (text) => handleSend(text, { source: 'button' }),
    [handleSend]
  );

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

  // Branching always asks for a name first. The dialog is prefilled with the
  // auto-generated "<save> (branch @ turn N)", so confirming without typing
  // keeps the old default. Shape: { turn, defaultName } | null.
  const [branchPrompt, setBranchPrompt] = useState(null);
  const [branching, setBranching] = useState(false);
  const [branchError, setBranchError] = useState(null);

  const handleBranchMessage = useCallback((turn) => {
    const saveId = session.sessionState?.active_save_id;
    if (!saveId) return;
    const sourceName = session.saves?.find((s) => s.id === saveId)?.display_name || saveId;
    setBranchError(null);
    setBranchPrompt({ turn, defaultName: `${sourceName} (branch @ turn ${turn})` });
  }, [session]);

  // Fork the story at the chosen turn under the confirmed name and jump into
  // the branch: the server copies the save (rolled back to that turn), we
  // load it and replay its transcript.
  const handleConfirmBranch = useCallback(async (name) => {
    const saveId = session.sessionState?.active_save_id;
    if (!saveId || branchPrompt == null) {
      setBranchPrompt(null);
      return;
    }
    setBranching(true);
    setBranchError(null);
    try {
      const r = await api.branchSave(saveId, { targetTurn: branchPrompt.turn, displayName: name });
      const data = await session.loadSave(r.branch.id);
      // Paint the branched transcript immediately; the quiet intro reconciles.
      if (Array.isArray(data?.state?.chat_messages) && data.state.chat_messages.length > 0) {
        ws.applyServerState(data.state);
        ws.sendIntro({ quiet: true });
      } else {
        ws.sendIntro();
      }
      setBranchPrompt(null);
      showToast(`Branched into "${r.branch.display_name || r.branch.id}"`, 'info');
    } catch (e) {
      // Keep the dialog open so the player can retry or cancel.
      setBranchError(e.message || 'Failed to branch.');
    }
    setBranching(false);
  }, [session, ws, showToast, branchPrompt]);

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

  const handleEnterGame = useCallback(async (saveId, seedState = null) => {
    pendingSeedRef.current = seedState;
    await session.refreshSession();
    setHeaderHidden(false);
    setCurrentMode('storyteller-game');
  }, [session]);

  // Keep the persisted UI state in sync: which screen is open, plus the save
  // (following the active save across branches) or character it points at.
  // Removed when the player exits to the menu.
  useEffect(() => {
    if (currentMode === null) {
      storage.removeItem(UI_STATE_KEY);
    } else if (currentMode === 'storyteller-game') {
      const id = session.sessionState?.active_save_id;
      if (id) storage.setItem(UI_STATE_KEY, JSON.stringify({ mode: currentMode, saveId: id }));
    } else if (currentMode === 'character-create') {
      // A brand-new character has no id to reload from the server, so a
      // relaunch lands on the character list instead; the editor's own draft
      // persistence brings the form content back when it's reopened.
      storage.setItem(UI_STATE_KEY, JSON.stringify(
        editCharacterId
          ? { mode: currentMode, editCharacterId }
          : { mode: 'character-creator' }
      ));
    } else {
      storage.setItem(UI_STATE_KEY, JSON.stringify({ mode: currentMode }));
    }
  }, [currentMode, session.sessionState?.active_save_id, editCharacterId]);

  // Auto-restore after a relaunch while the game or the character editor was
  // open (e.g. Android killed the PWA while the player checked another app).
  // The game path mirrors SaveSelectScreen's load: fetch the save, then enter
  // with its state as the seed so the transcript paints instantly instead of
  // waiting on the intro round-trip.
  useEffect(() => {
    if (!asyncRestore) return undefined;
    let alive = true;
    (async () => {
      try {
        if (asyncRestore.mode === 'storyteller-game') {
          const data = await api.loadSave(asyncRestore.saveId);
          if (!alive) return;
          await handleEnterGame(asyncRestore.saveId, data?.state);
        } else {
          const data = await api.loadCharacter(asyncRestore.editCharacterId);
          if (!alive) return;
          setEditCharacterId(asyncRestore.editCharacterId);
          setEditCharacterData(data);
          setCurrentMode('character-create');
        }
      } catch (e) {
        if (!alive) return;
        // Save/character gone or backend restarted into a clean state — fall
        // back rather than looping on a broken restore.
        if (asyncRestore.mode === 'character-create') setCurrentMode('character-creator');
        else storage.removeItem(UI_STATE_KEY);
      } finally {
        if (alive) setResuming(false);
      }
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Completed turns are autosaved server-side, so exiting while idle loses
  // nothing; only an in-flight generation is at risk (it never gets saved if
  // interrupted), so that's the only case that warrants a warning.
  const generating = ws.currentStream != null || ws.postProcessing;

  // A save restricts which modules are active (chosen at story start / edited
  // later via the cog). Filter the UI to that set so disabled modules
  // contribute no sidebar/header/settings/overlay UI. Legacy saves without the
  // reserved key get all modules. Memoized so streaming re-renders (which
  // happen every animation frame) pass an identity-stable list down to the
  // memoized feed blocks.
  const activeModuleIds = session.moduleConfigs?.__active_modules__;
  const gameModules = useMemo(
    () => (Array.isArray(activeModuleIds)
      ? (modules || []).filter((m) => activeModuleIds.includes(m.id))
      : modules),
    [modules, activeModuleIds]
  );

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
    storage.setItem(ONBOARDING_DONE_KEY, '1');
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
      // Existing stories: paint the transcript immediately from the state the
      // load call already returned, instead of sitting on an empty feed (with a
      // misleading "AI is writing…") while the server's intro round-trip runs
      // on_gather_context — that can take several seconds on a big story. The
      // intro still runs (quietly) to initialize module_data/swipes, and the
      // later `done` reconciles turn numbers and swipes in place.
      const seed = pendingSeedRef.current;
      pendingSeedRef.current = null;
      const existing = Array.isArray(seed?.chat_messages) && seed.chat_messages.length > 0;
      if (existing) {
        ws.applyServerState(seed);
        ws.sendIntro({ quiet: true });
      } else {
        // New story: clear any stale transcript so the streamed opening doesn't
        // append to a previous story's messages.
        ws.setMessages([]);
        ws.sendIntro();
      }
    }
  }, [currentMode, ws.isConnected]);

  if (currentMode === null) {
    // Hold the menu back until the first-launch check resolves (fast, local)
    // so a fresh install doesn't flash the menu before the wizard — and while
    // auto-resuming a story after a reload, so the menu never flashes by.
    if (showOnboarding === null || resuming) {
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
    // Restoring straight into a module screen races the module-list fetch;
    // hold the blank frame instead of flashing the placeholder.
    if ((modules || []).length === 0) {
      return <div className="min-h-screen bg-gradient-to-br from-gray-950 via-gray-900 to-gray-950" />;
    }
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

  if (currentMode === 'server-logs') {
    return <ServerLogScreen onBack={() => setCurrentMode(null)} />;
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
      .sort((a, b) => a.module.localeCompare(b.module) || a.command.localeCompare(b.command));
    return (
    <ModuleEventProvider>
      <div className="flex h-dvh bg-gray-900 text-gray-100 font-sans overflow-hidden">
        <Sidebar
          session={session}
          modules={gameModules}
          gameState={gameState}
          onCommand={handleButtonCommand}
          generating={generating}
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
          onCommand={handleButtonCommand}
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

      {branchPrompt && (
        <BranchNameDialog
          defaultName={branchPrompt.defaultName}
          busy={branching}
          error={branchError}
          onConfirm={handleConfirmBranch}
          onCancel={() => !branching && setBranchPrompt(null)}
        />
      )}

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

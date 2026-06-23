import { createContext, useContext, useState, useCallback } from 'react';

const ModuleEventContext = createContext(null);

export function ModuleEventProvider({ children }) {
  const [modalStates, setModalStates] = useState({});
  const [events, setEvents] = useState({});

  const openModal = useCallback((modId) => {
    setModalStates(prev => ({ ...prev, [modId]: true }));
  }, []);

  const closeModal = useCallback((modId) => {
    setModalStates(prev => ({ ...prev, [modId]: false }));
  }, []);

  const isModalOpen = useCallback((modId) => {
    return modalStates[modId] || false;
  }, [modalStates]);

  const emitEvent = useCallback((eventName, payload) => {
    setEvents(prev => ({ ...prev, [eventName]: { payload, ts: Date.now() } }));
  }, []);

  const value = {
    modalStates, openModal, closeModal, isModalOpen,
    events, emitEvent
  };

  return (
    <ModuleEventContext.Provider value={value}>
      {children}
    </ModuleEventContext.Provider>
  );
}

export function useModuleEvents() {
  const ctx = useContext(ModuleEventContext);
  if (!ctx) throw new Error('useModuleEvents must be used within ModuleEventProvider');
  return ctx;
}

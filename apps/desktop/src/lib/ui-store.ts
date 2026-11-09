import { create } from 'zustand';

interface InterfaceState {
  activeMeeting: string | null;
  selectedUtterance: string | null;
  panel: string;
  shortcutsOpen: boolean;
  /** Meeting created by "Start meeting now": its page starts capture as soon as it can. */
  autoStart: string | null;
  setAutoStart: (id: string | null) => void;
  setActiveMeeting: (id: string | null) => void;
  selectUtterance: (id: string | null) => void;
  setPanel: (panel: string) => void;
  setShortcutsOpen: (open: boolean) => void;
}

// Persisted domain and native preference state belong to Query, never this store.
export const useInterface = create<InterfaceState>((set) => ({
  activeMeeting: null,
  selectedUtterance: null,
  panel: 'Transcript',
  shortcutsOpen: false,
  autoStart: null,
  setAutoStart: (autoStart) => set({ autoStart }),
  setActiveMeeting: (activeMeeting) => set({ activeMeeting }),
  selectUtterance: (selectedUtterance) => set(selectedUtterance ? { selectedUtterance, panel: 'Transcript' } : { selectedUtterance }),
  setPanel: (panel) => set({ panel }),
  setShortcutsOpen: (shortcutsOpen) => set({ shortcutsOpen }),
}));

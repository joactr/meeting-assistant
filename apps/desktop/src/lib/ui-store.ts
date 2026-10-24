import { create } from 'zustand';

interface InterfaceState {
  activeMeeting: string | null;
  selectedUtterance: string | null;
  panel: string;
  shortcutsOpen: boolean;
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
  setActiveMeeting: (activeMeeting) => set({ activeMeeting }),
  selectUtterance: (selectedUtterance) => set(selectedUtterance ? { selectedUtterance, panel: 'Transcript' } : { selectedUtterance }),
  setPanel: (panel) => set({ panel }),
  setShortcutsOpen: (shortcutsOpen) => set({ shortcutsOpen }),
}));

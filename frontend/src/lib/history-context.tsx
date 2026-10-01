"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { getHistory } from "./api";
import type { HistoryEntry } from "./types";

type HistoryState = {
  entries: HistoryEntry[];
  loading: boolean;
  refresh: () => Promise<void>;
};

const HistoryContext = createContext<HistoryState | null>(null);

export function HistoryProvider({ children }: { children: ReactNode }) {
  const [entries, setEntries] = useState<HistoryEntry[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const data = await getHistory();
      setEntries(data);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <HistoryContext.Provider value={{ entries, loading, refresh }}>
      {children}
    </HistoryContext.Provider>
  );
}

export function useHistory() {
  const ctx = useContext(HistoryContext);
  if (!ctx) throw new Error("useHistory должен использоваться внутри HistoryProvider");
  return ctx;
}

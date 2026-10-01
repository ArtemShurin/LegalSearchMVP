"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { getCollections } from "./api";
import type { CollectionResponse } from "./types";

type CollectionsState = {
  collections: CollectionResponse[];
  loading: boolean;
  refresh: () => Promise<void>;
};

const CollectionsContext = createContext<CollectionsState | null>(null);

export function CollectionsProvider({ children }: { children: ReactNode }) {
  const [collections, setCollections] = useState<CollectionResponse[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const data = await getCollections();
      setCollections(data);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  return (
    <CollectionsContext.Provider value={{ collections, loading, refresh }}>
      {children}
    </CollectionsContext.Provider>
  );
}

export function useCollections() {
  const ctx = useContext(CollectionsContext);
  if (!ctx) throw new Error("useCollections должен использоваться внутри CollectionsProvider");
  return ctx;
}

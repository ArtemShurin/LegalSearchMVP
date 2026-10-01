"use client";

import { createContext, useCallback, useContext, useEffect, useState, type ReactNode } from "react";
import { getDocuments } from "./api";
import type { DocumentResponse } from "./types";

type DocumentsState = {
  documents: DocumentResponse[];
  loading: boolean;
  refresh: () => Promise<void>;
};

const DocumentsContext = createContext<DocumentsState | null>(null);

export function DocumentsProvider({ children }: { children: ReactNode }) {
  const [documents, setDocuments] = useState<DocumentResponse[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const data = await getDocuments();
      setDocuments(data);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    refresh();
  }, [refresh]);

  // Пока хоть один документ "processing" (фоновая индексация) — поллим раз в
  // 3 секунды, чтобы статус в UI обновился сам, без ручного рефреша страницы.
  useEffect(() => {
    if (!documents.some((d) => d.status === "processing")) return;
    const t = setInterval(refresh, 3000);
    return () => clearInterval(t);
  }, [documents, refresh]);

  return (
    <DocumentsContext.Provider value={{ documents, loading, refresh }}>{children}</DocumentsContext.Provider>
  );
}

export function useDocuments() {
  const ctx = useContext(DocumentsContext);
  if (!ctx) throw new Error("useDocuments должен использоваться внутри DocumentsProvider");
  return ctx;
}

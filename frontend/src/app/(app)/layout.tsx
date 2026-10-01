"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "@/lib/auth-context";
import { HistoryProvider } from "@/lib/history-context";
import { CollectionsProvider } from "@/lib/collections-context";
import { DocumentsProvider } from "@/lib/documents-context";
import { Sidebar } from "@/components/Sidebar";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { user, loading } = useAuth();
  const router = useRouter();

  useEffect(() => {
    if (!loading && !user) router.replace("/login");
  }, [loading, user, router]);

  if (loading || !user) {
    return (
      <div className="flex-1 flex items-center justify-center text-text-tertiary text-sm">
        Загрузка…
      </div>
    );
  }

  return (
    <HistoryProvider>
      <CollectionsProvider>
        <DocumentsProvider>
          <div className="flex-1 flex min-h-0">
            <Sidebar />
            {children}
          </div>
        </DocumentsProvider>
      </CollectionsProvider>
    </HistoryProvider>
  );
}

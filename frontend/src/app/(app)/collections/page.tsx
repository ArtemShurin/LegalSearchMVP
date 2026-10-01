"use client";

import { useState } from "react";
import Link from "next/link";
import { useCollections } from "@/lib/collections-context";
import { createCollection } from "@/lib/api";
import { FolderIcon, PlusIcon } from "@/components/icons";
import { formatDate } from "@/lib/format";

export default function CollectionsPage() {
  const { collections, loading, refresh } = useCollections();
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  async function submitCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      await createCollection(name.trim());
      await refresh();
      setName("");
      setCreating(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex-1 min-w-0 h-full overflow-y-auto">
      <div className="max-w-[920px] mx-auto px-8 py-9 pb-16">
        <h1 className="font-display text-[26px] font-bold mb-6">Подборки</h1>

        {loading && <div className="text-text-tertiary text-sm">Загрузка…</div>}

        {!loading && (
          <div className="grid grid-cols-4 gap-3">
            {collections.map((c) => (
              <Link
                key={c.id}
                href={`/collections/${c.id}`}
                className="border border-border bg-surface rounded-2xl p-4 hover:border-accent-soft-2"
              >
                <FolderIcon size={20} />
                <div className="font-display text-[15px] font-bold mt-3">{c.name}</div>
                <div className="text-[12px] text-text-secondary mt-1">
                  {c.items.length} {c.items.length === 1 ? "документ" : "документов"} ·{" "}
                  {formatDate(c.created_at)}
                </div>
              </Link>
            ))}

            {!creating && (
              <button
                onClick={() => setCreating(true)}
                className="border border-dashed border-border rounded-2xl p-4 flex flex-col items-center justify-center gap-1.5 text-text-tertiary min-h-[104px]"
              >
                <PlusIcon size={18} />
                <div className="text-[12.5px] font-semibold">Создать подборку</div>
              </button>
            )}

            {creating && (
              <form
                onSubmit={submitCreate}
                className="border border-accent-soft-2 bg-accent-soft rounded-2xl p-4 flex flex-col gap-2 min-h-[104px]"
              >
                <label htmlFor="new-col" className="sr-only">
                  Название подборки
                </label>
                <input
                  id="new-col"
                  autoFocus
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Название подборки"
                  className="bg-bg border border-border rounded-md px-2.5 h-9 text-[13px] outline-none"
                />
                <div className="flex gap-2 mt-auto">
                  <button
                    type="submit"
                    disabled={busy || !name.trim()}
                    className="flex-1 h-8 rounded-md bg-accent text-[#1a1408] text-[12.5px] font-semibold disabled:opacity-50"
                  >
                    Создать
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setCreating(false);
                      setName("");
                    }}
                    className="h-8 px-3 rounded-md border border-border text-[12.5px] text-text-secondary"
                  >
                    Отмена
                  </button>
                </div>
              </form>
            )}
          </div>
        )}

        {!loading && collections.length === 0 && (
          <div className="text-text-tertiary text-sm mt-2">
            Пока нет подборок — сохраните документ из результатов поиска, или создайте пустую подборку выше.
          </div>
        )}
      </div>
    </main>
  );
}

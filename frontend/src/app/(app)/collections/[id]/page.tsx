"use client";

import { useState } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { useCollections } from "@/lib/collections-context";
import { deleteCollection, deleteCollectionItem, renameCollection } from "@/lib/api";
import { XIcon } from "@/components/icons";
import { formatDate } from "@/lib/format";
import type { CollectionItemResponse } from "@/lib/types";

const COLLAPSE_THRESHOLD = 130; // примерно столько символов влезает в 2 строки превью

export default function CollectionDetailPage() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const { collections, refresh } = useCollections();
  const [renaming, setRenaming] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const collection = collections.find((c) => c.id === id);

  if (!collection) {
    return (
      <main className="flex-1 min-w-0 h-full flex items-center justify-center text-text-tertiary text-sm">
        Подборка не найдена
      </main>
    );
  }

  async function submitRename(e: React.FormEvent) {
    e.preventDefault();
    if (!name.trim()) return;
    setBusy(true);
    try {
      await renameCollection(collection!.id, name.trim());
      await refresh();
      setRenaming(false);
    } finally {
      setBusy(false);
    }
  }

  async function onDelete() {
    if (!confirm(`Удалить подборку «${collection!.name}» вместе со всеми документами?`)) return;
    setBusy(true);
    try {
      await deleteCollection(collection!.id);
      await refresh();
      router.push("/collections");
    } finally {
      setBusy(false);
    }
  }

  async function onRemoveItem(itemId: string) {
    setBusy(true);
    try {
      await deleteCollectionItem(collection!.id, itemId);
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex-1 min-w-0 h-full overflow-y-auto">
      <div className="max-w-[920px] mx-auto px-8 py-9 pb-16">
        <div className="flex items-center gap-2 text-[13px] text-text-tertiary mb-4">
          <Link href="/collections" className="hover:text-text-secondary">
            Подборки
          </Link>
          <span>／</span>
          <span className="text-text font-semibold">{collection.name}</span>
          <div className="ml-auto flex gap-2">
            {!renaming ? (
              <button
                onClick={() => {
                  setName(collection.name);
                  setRenaming(true);
                }}
                className="h-[30px] rounded-md border border-border bg-surface text-text-secondary text-[12px] px-2.5"
              >
                Переименовать
              </button>
            ) : (
              <form onSubmit={submitRename} className="flex gap-1.5">
                <input
                  autoFocus
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  className="h-[30px] rounded-md border border-border bg-surface text-[12.5px] px-2 outline-none"
                />
                <button
                  type="submit"
                  disabled={busy}
                  className="h-[30px] rounded-md bg-accent-soft text-accent-text text-[12px] px-2.5"
                >
                  Сохранить
                </button>
              </form>
            )}
            <button
              onClick={onDelete}
              disabled={busy}
              className="h-[30px] rounded-md border border-border bg-surface text-danger text-[12px] px-2.5"
            >
              Удалить
            </button>
          </div>
        </div>

        {collection.items.length === 0 && (
          <div className="text-text-tertiary text-sm">
            Пока пусто — сохраняйте документы сюда прямо из результатов поиска.
          </div>
        )}

        <div className="flex flex-col gap-2">
          {collection.items.map((item) => (
            <CollectionItemRow key={item.id} item={item} busy={busy} onRemove={() => onRemoveItem(item.id)} />
          ))}
        </div>
      </div>
    </main>
  );
}

function CollectionItemRow({
  item,
  busy,
  onRemove,
}: {
  item: CollectionItemResponse;
  busy: boolean;
  onRemove: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const canExpand = (item.fragment?.length ?? 0) > COLLAPSE_THRESHOLD;

  return (
    <div className="border border-border bg-surface rounded-[10px] p-3.5 flex gap-3.5 items-start">
      <div className="flex-1 min-w-0">
        <span className="font-display text-[14.5px] font-bold">{item.title}</span>
        {item.fragment && (
          <>
            <div
              className="text-[12.5px] text-text-secondary leading-[1.5] italic mt-1.5 pl-2.5 border-l-2 border-border overflow-hidden"
              style={
                expanded
                  ? undefined
                  : { display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }
              }
              title="Дословная цитата из текста документа"
            >
              «{item.fragment}»
            </div>
            {canExpand && (
              <button
                onClick={() => setExpanded((v) => !v)}
                className="text-[11.5px] font-semibold text-accent-text mt-1"
              >
                {expanded ? "Свернуть" : "Показать полностью"}
              </button>
            )}
          </>
        )}
        <div className="text-[11.5px] text-text-tertiary mt-2">Добавлено {formatDate(item.added_at)}</div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        {item.relevance != null && (
          <span className="text-[11.5px] font-bold text-accent-text bg-accent-soft px-2.5 py-0.5 rounded-full tabular-nums">
            {item.relevance.toFixed(1)}
          </span>
        )}
        <button
          aria-label="Убрать из подборки"
          onClick={onRemove}
          disabled={busy}
          className="w-7 h-7 rounded-md text-text-tertiary flex items-center justify-center"
        >
          <XIcon />
        </button>
      </div>
    </div>
  );
}

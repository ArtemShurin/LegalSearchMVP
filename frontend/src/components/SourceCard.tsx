"use client";

import { useRef, useState } from "react";
import { useCollections } from "@/lib/collections-context";
import { addCollectionItem, createCollection, ApiError } from "@/lib/api";
import { BookmarkIcon, PlusIcon } from "./icons";
import type { SourceItem } from "@/lib/types";

const COLLAPSE_THRESHOLD = 130; // примерно столько символов влезает в 2 строки превью

export function SourceCard({ source, index }: { source: SourceItem; index: number }) {
  const { collections, refresh } = useCollections();
  const [open, setOpen] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [newName, setNewName] = useState("");
  const [busy, setBusy] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);
  const canExpand = source.fragment.length > COLLAPSE_THRESHOLD;

  // Вычисляем "сохранено" из реальных данных подборок, а не из локального
  // стейта — иначе флажок гаснет при возврате на эту страницу (стейт
  // компонента не переживает переход на другой роут и обратно).
  const savedInIds = new Set(
    collections.filter((c) => c.items.some((it) => it.document_id === source.document_id)).map((c) => c.id),
  );
  const alreadySaved = savedInIds.size > 0;

  async function saveTo(collectionId: string) {
    setBusy(true);
    setNotice(null);
    try {
      await addCollectionItem(collectionId, {
        document_id: source.document_id,
        title: source.title,
        fragment: source.fragment,
        relevance: source.relevance,
      });
      setOpen(false);
      await refresh();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setNotice("Уже в этой подборке");
      } else {
        setNotice("Не удалось сохранить");
      }
    } finally {
      setBusy(false);
    }
  }

  async function createAndSave() {
    if (!newName.trim()) return;
    setBusy(true);
    try {
      const col = await createCollection(newName.trim());
      await refresh();
      setNewName("");
      await saveTo(col.id);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="relative border border-border bg-surface rounded-[10px] p-3.5 flex gap-3 items-start">
      <div className="w-5 h-5 shrink-0 rounded-md bg-surface-2 text-accent-text text-[11px] font-bold flex items-center justify-center mt-0.5">
        {index + 1}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2 flex-wrap">
          <span className="font-display text-[14px] font-bold">{source.title}</span>
        </div>
        <div
          className="text-[12.5px] text-text-secondary leading-[1.5] italic mt-1.5 pl-2.5 border-l-2 border-border overflow-hidden"
          style={
            expanded
              ? undefined
              : { display: "-webkit-box", WebkitLineClamp: 2, WebkitBoxOrient: "vertical" }
          }
          title="Дословная цитата из текста документа"
        >
          «{source.fragment}»
        </div>
        {canExpand && (
          <button
            onClick={() => setExpanded((v) => !v)}
            className="text-[11.5px] font-semibold text-accent-text mt-1"
          >
            {expanded ? "Свернуть" : "Показать полностью"}
          </button>
        )}
      </div>
      <div className="flex items-center gap-2 shrink-0" ref={boxRef}>
        <span className="text-[11.5px] font-bold text-accent-text bg-accent-soft px-2.5 py-0.5 rounded-full tabular-nums">
          {source.relevance.toFixed(1)}
        </span>
        <button
          aria-label="Сохранить в подборку"
          onClick={() => setOpen((v) => !v)}
          className="w-[26px] h-[26px] rounded-md text-text-tertiary flex items-center justify-center"
        >
          <BookmarkIcon filled={alreadySaved} />
        </button>

        {open && (
          <div className="absolute right-0 top-9 z-10 w-60 bg-surface-2 border border-border rounded-xl p-2 shadow-lg">
            <div className="text-[11px] font-bold uppercase tracking-[.06em] text-text-tertiary px-2 pt-1 pb-1.5">
              Сохранить в подборку
            </div>
            <div className="max-h-40 overflow-y-auto flex flex-col gap-0.5">
              {collections.map((c) => {
                const here = savedInIds.has(c.id);
                return (
                  <button
                    key={c.id}
                    disabled={busy || here}
                    onClick={() => saveTo(c.id)}
                    className="text-left text-[13px] px-2 py-1.5 rounded-md hover:bg-surface disabled:opacity-50 flex items-center justify-between gap-2"
                  >
                    <span>{c.name}</span>
                    {here && <span className="text-[11px] text-text-tertiary">уже здесь</span>}
                  </button>
                );
              })}
            </div>
            <div className="flex gap-1.5 mt-1.5 px-1">
              <input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="Новая подборка…"
                className="flex-1 h-8 rounded-md border border-border bg-bg text-[12.5px] px-2 outline-none min-w-0"
              />
              <button
                aria-label="Создать и сохранить"
                disabled={busy || !newName.trim()}
                onClick={createAndSave}
                className="w-8 h-8 shrink-0 rounded-md bg-accent-soft text-accent-text flex items-center justify-center disabled:opacity-50"
              >
                <PlusIcon size={13} />
              </button>
            </div>
            {notice && <div className="text-[12px] text-text-tertiary px-2 pt-1.5">{notice}</div>}
          </div>
        )}
      </div>
    </div>
  );
}

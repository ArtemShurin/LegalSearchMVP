"use client";

import Link from "next/link";
import { usePathname, useSearchParams } from "next/navigation";
import { useState } from "react";
import { useAuth } from "@/lib/auth-context";
import { useHistory } from "@/lib/history-context";
import { useCollections } from "@/lib/collections-context";
import { useDocuments } from "@/lib/documents-context";
import { useTheme } from "@/lib/theme-context";
import { groupHistoryByDate } from "@/lib/format";
import { DocumentIcon, FolderIcon, LogoMark, LogOutIcon, PlusIcon, SearchIcon, SunMoonIcon } from "./icons";

export function Sidebar() {
  const { user, logout } = useAuth();
  const { entries, loading } = useHistory();
  const { collections } = useCollections();
  const { documents } = useDocuments();
  const { toggle } = useTheme();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const activeId = searchParams.get("id");
  const [filter, setFilter] = useState("");

  const filtered = filter.trim()
    ? entries.filter((e) => e.text.toLowerCase().includes(filter.trim().toLowerCase()))
    : entries;
  const groups = groupHistoryByDate(filtered);

  const initials = (user?.email ?? "?")
    .split("@")[0]
    .slice(0, 2)
    .toUpperCase();

  return (
    <aside className="w-[272px] shrink-0 h-full bg-bg-sidebar border-r border-border-soft flex flex-col">
      {/* Wordmark */}
      <div className="h-[60px] shrink-0 flex items-center px-[18px] border-b border-border-soft">
        <div className="flex items-center gap-[9px]">
          <LogoMark />
          <span className="font-display text-[18px] font-bold tracking-[.2px]">LegalSearch</span>
        </div>
      </div>

      {/* New query */}
      <div className="px-3.5 pt-3.5 pb-2.5">
        <Link
          href="/search"
          className="w-full h-10 rounded-[10px] border border-accent-soft-2 bg-accent-soft text-accent-text font-semibold text-[13.5px] flex items-center justify-center gap-1.5"
        >
          <PlusIcon />
          Новый запрос
        </Link>
      </div>

      {/* Collections nav */}
      <div className="px-3.5 pb-3">
        <Link
          href="/collections"
          className={`h-[38px] rounded-[10px] flex items-center gap-2.5 px-2.5 text-[13.5px] font-medium ${
            pathname.startsWith("/collections") ? "bg-surface-2 text-text font-semibold" : "text-text-secondary"
          }`}
        >
          <FolderIcon />
          Подборки
          <span className="ml-auto text-[11.5px] text-text-tertiary bg-bg-sidebar rounded-full px-2 py-0.5">
            {collections.length}
          </span>
        </Link>
      </div>

      {/* Documents nav */}
      <div className="px-3.5 pb-3">
        <Link
          href="/documents"
          className={`h-[38px] rounded-[10px] flex items-center gap-2.5 px-2.5 text-[13.5px] font-medium ${
            pathname.startsWith("/documents") ? "bg-surface-2 text-text font-semibold" : "text-text-secondary"
          }`}
        >
          <DocumentIcon />
          Документы
          <span className="ml-auto text-[11.5px] text-text-tertiary bg-bg-sidebar rounded-full px-2 py-0.5">
            {documents.length}
          </span>
        </Link>
      </div>

      <div className="h-px bg-border-soft mx-3.5 mb-3" />

      {/* History search */}
      <div className="px-3.5 pb-2.5">
        <div className="relative">
          <span className="absolute left-[11px] top-1/2 -translate-y-1/2 text-text-tertiary">
            <SearchIcon />
          </span>
          <label htmlFor="hist-search" className="sr-only">
            Искать в истории
          </label>
          <input
            id="hist-search"
            type="text"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Искать в истории"
            className="w-full h-[34px] rounded-[10px] border border-border bg-surface text-text text-[13px] pl-8 pr-2.5 outline-none placeholder:text-text-tertiary"
          />
        </div>
      </div>

      {/* History list */}
      <div className="flex-1 overflow-y-auto px-2.5 pb-2.5">
        {loading && <div className="px-2 py-2 text-[12.5px] text-text-tertiary">Загрузка…</div>}
        {!loading && groups.length === 0 && (
          <div className="px-2 py-2 text-[12.5px] text-text-tertiary">
            {filter ? "Ничего не найдено" : "История запросов появится здесь"}
          </div>
        )}
        {groups.map((group) => (
          <div key={group.label}>
            <div className="px-2 pt-2.5 pb-1.5 text-[11px] font-bold tracking-[.06em] uppercase text-text-tertiary">
              {group.label}
            </div>
            <div className="flex flex-col gap-0.5">
              {group.items.map((entry) => (
                <Link
                  key={entry.id}
                  href={`/search?id=${entry.id}`}
                  className={`rounded-[7px] px-2.5 py-2.5 text-[13px] whitespace-nowrap overflow-hidden text-ellipsis block ${
                    activeId === entry.id ? "bg-surface-2 text-text" : "text-text-secondary"
                  }`}
                  title={entry.text}
                >
                  {entry.text}
                </Link>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* User row */}
      <div className="h-[58px] shrink-0 border-t border-border-soft flex items-center gap-2.5 px-3.5">
        <div className="w-[30px] h-[30px] rounded-full bg-accent-soft text-accent-text flex items-center justify-center text-[12.5px] font-bold font-display shrink-0">
          {initials}
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-[13px] font-semibold whitespace-nowrap overflow-hidden text-ellipsis">
            {user?.email ?? "…"}
          </div>
          <div className="text-[11.5px] text-text-tertiary">LegalSearch MVP</div>
        </div>
        <button
          aria-label="Переключить тему"
          onClick={toggle}
          className="w-7 h-7 shrink-0 rounded-md text-text-tertiary flex items-center justify-center"
        >
          <SunMoonIcon />
        </button>
        <button
          aria-label="Выйти"
          onClick={logout}
          className="w-7 h-7 shrink-0 rounded-md text-text-tertiary flex items-center justify-center"
        >
          <LogOutIcon />
        </button>
      </div>
    </aside>
  );
}

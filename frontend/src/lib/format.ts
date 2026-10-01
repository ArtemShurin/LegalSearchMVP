import type { HistoryEntry } from "./types";

function isSameDay(a: Date, b: Date) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

export function groupHistoryByDate(entries: HistoryEntry[]) {
  const now = new Date();
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  const weekAgo = new Date(now);
  weekAgo.setDate(now.getDate() - 7);

  const groups: { label: string; items: HistoryEntry[] }[] = [
    { label: "Сегодня", items: [] },
    { label: "Вчера", items: [] },
    { label: "Последние 7 дней", items: [] },
    { label: "Ранее", items: [] },
  ];

  for (const entry of entries) {
    const d = new Date(entry.created_at);
    if (isSameDay(d, now)) groups[0].items.push(entry);
    else if (isSameDay(d, yesterday)) groups[1].items.push(entry);
    else if (d >= weekAgo) groups[2].items.push(entry);
    else groups[3].items.push(entry);
  }

  return groups.filter((g) => g.items.length > 0);
}

export function formatDate(iso: string) {
  return new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

export function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} Б`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(0)} КБ`;
  return `${(bytes / 1024 ** 2).toFixed(1)} МБ`;
}

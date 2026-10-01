import type { ReactNode } from "react";

// Ответ LLM содержит буквальные подстроки вида "[1]", "[2]" (см. ANSWER_PROMPT
// в backend/src/services/llm.py) — превращаем их в оформленные чипы-цитаты.
// Модель не всегда строго следует формату:
//  - иногда добавляет пояснение внутрь скобок ("[3, Статья 1069]") — показываем только номер;
//  - иногда пишет диапазон как два отдельных "[1] - [5]" вместо одного "[1-5]" —
//    схлопываем такую пару в один чип ДО основного разбора, иначе дефис между
//    ними остаётся голым текстом и выглядит как мусор.
export function renderAnswerWithCitations(text: string): ReactNode[] {
  const normalized = text.replace(/\[(\d+)\]\s*[-–]\s*\[(\d+)\]/g, "[$1–$2]");
  const parts = normalized.split(/(\[\d+(?:[-–]\d+)?(?:,[^\]]*)?\])/g);
  return parts.map((part, i) => {
    const match = part.match(/^\[(\d+(?:[-–]\d+)?)(?:,[^\]]*)?\]$/);
    if (!match) return <span key={i}>{part}</span>;
    return (
      <span
        key={i}
        className="inline-flex items-center justify-center min-w-[17px] h-[17px] px-1 rounded-[5px] bg-surface-2 text-accent-text text-[10.5px] font-bold mx-0.5 align-[2px]"
      >
        {match[1]}
      </span>
    );
  });
}

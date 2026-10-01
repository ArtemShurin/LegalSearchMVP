"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { streamSearch, ApiError } from "@/lib/api";
import { useHistory } from "@/lib/history-context";
import { renderAnswerWithCitations } from "@/lib/citations";
import { SourceCard } from "@/components/SourceCard";
import { SendIcon, StopIcon, LogoMark, DocumentIcon } from "@/components/icons";
import type { SourceItem } from "@/lib/types";

type Displayed = {
  query: string;
  answer: string;
  sources: SourceItem[];
  elapsedSec?: number;
};

type Phase = "idle" | "thinking" | "streaming" | "done";

export default function SearchPage() {
  return (
    <Suspense fallback={null}>
      <SearchPageRouter />
    </Suspense>
  );
}

function SearchPageRouter() {
  const searchParams = useSearchParams();
  const id = searchParams.get("id");
  return <SearchView key={id ?? "new"} replayId={id} />;
}

function SearchView({ replayId }: { replayId: string | null }) {
  const { entries, refresh } = useHistory();
  const [input, setInput] = useState("");
  const [result, setResult] = useState<Displayed | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [error, setError] = useState<string | null>(null);
  const [internalOnly, setInternalOnly] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);

  // Режим воспроизведения: показываем сохранённый вопрос-ответ из истории как есть.
  useEffect(() => {
    if (!replayId) return;
    const found = entries.find((e) => e.id === replayId);
    if (found) {
      // Синхронизируем локальный стейт с историей (внешний источник данных,
      // который может подгрузиться позже первого рендера) — этот эффект
      // выполняется один раз на смонтированный экземпляр (см. key={id} выше).
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setResult({
        query: found.text,
        answer: found.answer,
        sources: found.sources.map((s) => ({
          document_id: s.document_id,
          title: s.title ?? "Без названия",
          relevance: s.relevance ?? 0,
          fragment: s.fragment ?? "",
        })),
      });
      setPhase("done");
    }
  }, [replayId, entries]);

  // Отменяем незавершённый запрос при размонтировании (переход на другую страницу).
  useEffect(() => {
    return () => abortRef.current?.abort();
  }, []);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    const q = input.trim();
    if (!q || phase === "thinking" || phase === "streaming") return;

    setError(null);
    setInput("");
    setPhase("thinking");
    setResult({ query: q, answer: "", sources: [] });
    const t0 = performance.now();

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await streamSearch(
        q,
        {
          onSources: (sources) => {
            setPhase("streaming");
            setResult((prev) => (prev ? { ...prev, sources } : prev));
          },
          onToken: (text) => {
            setResult((prev) => (prev ? { ...prev, answer: prev.answer + text } : prev));
          },
          onDone: () => {
            setPhase("done");
            setResult((prev) => (prev ? { ...prev, elapsedSec: (performance.now() - t0) / 1000 } : prev));
            refresh();
          },
        },
        controller.signal,
        internalOnly,
      );
    } catch (err) {
      if (err instanceof DOMException && err.name === "AbortError") {
        // Пользователь сам остановил генерацию — оставляем то, что успело прийти, без ошибки.
        setPhase("done");
        setResult((prev) => (prev ? { ...prev, elapsedSec: (performance.now() - t0) / 1000 } : prev));
      } else {
        setPhase("idle");
        setResult(null);
        setError(err instanceof ApiError ? err.message : "Не удалось выполнить поиск. Попробуйте ещё раз.");
      }
    } finally {
      abortRef.current = null;
    }
  }

  function onStop() {
    abortRef.current?.abort();
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      e.currentTarget.form?.requestSubmit();
    }
  }

  const busy = phase === "thinking" || phase === "streaming";

  return (
    <main className="flex-1 min-w-0 h-full flex flex-col">
      {/* Top bar */}
      <div className="h-14 shrink-0 flex items-center justify-between px-7 border-b border-border-soft">
        <span className="font-display text-[15px] font-bold text-text-secondary">Поиск</span>
        {result && phase !== "thinking" && result.sources.length > 0 ? (
          <div className="text-[12px] text-text-tertiary">
            Найдено {result.sources.length} источников
            {result.elapsedSec ? ` · ${result.elapsedSec.toFixed(1)} с` : ""}
          </div>
        ) : null}
      </div>

      {/* Thread */}
      <div className="flex-1 overflow-y-auto flex justify-center px-6 pt-8 pb-3">
        <div className="w-full max-w-[760px] flex flex-col gap-5">
          {!result && phase === "idle" && <EmptyState onPick={(q) => setInput(q)} />}

          {result && (
            <>
              <div className="flex justify-end">
                <div className="max-w-[78%] bg-accent-soft border border-accent-soft-2 rounded-[16px_16px_4px_16px] px-4 py-3 text-[14.5px] leading-[1.5]">
                  {result.query}
                </div>
              </div>

              <div className="flex gap-3">
                <div className="w-7 h-7 shrink-0 rounded-lg bg-surface-2 flex items-center justify-center mt-0.5">
                  <LogoMark size={15} />
                </div>
                <div className="flex-1 min-w-0">
                  <div className="text-[12.5px] font-bold text-text-secondary mb-1.5">LegalSearch</div>
                  {phase === "thinking" ? (
                    <div className="text-[14px] text-text-tertiary pt-0.5">Ищу по нормам и практике…</div>
                  ) : (
                    <div className="text-[14.5px] leading-[1.7]">
                      {renderAnswerWithCitations(result.answer)}
                      {phase === "streaming" && (
                        <span className="inline-block w-[2px] h-[1em] bg-accent-text align-middle ml-0.5 animate-pulse" />
                      )}
                    </div>
                  )}
                </div>
              </div>

              {result.sources.length > 0 && (
                <div className="ml-10 flex flex-col gap-2">
                  <div className="text-[11px] font-bold uppercase tracking-[.06em] text-text-tertiary mb-0.5">
                    Источники
                  </div>
                  {result.sources.map((s, i) => (
                    <SourceCard key={s.document_id + i} source={s} index={i} />
                  ))}
                </div>
              )}
            </>
          )}

          {error && <div className="text-[13.5px] text-danger">{error}</div>}
        </div>
      </div>

      {/* Input bar */}
      <div className="shrink-0 flex justify-center px-6 pt-3 pb-5 border-t border-border-soft">
        <div className="w-full max-w-[760px]">
          <div className="flex justify-start mb-2">
            <button
              type="button"
              onClick={() => setInternalOnly((v) => !v)}
              aria-pressed={internalOnly}
              className={`h-7 rounded-full border text-[12px] font-semibold px-3 flex items-center gap-1.5 ${
                internalOnly
                  ? "bg-accent-soft border-accent-soft-2 text-accent-text"
                  : "bg-transparent border-border text-text-tertiary"
              }`}
            >
              <DocumentIcon size={12} />
              Только внутренние документы
            </button>
          </div>
          <form
            onSubmit={onSubmit}
            className="flex items-end gap-2.5 border border-border bg-surface rounded-[20px] pl-4 pr-2 py-2"
          >
            <label htmlFor="q" className="sr-only">
              Вопрос по праву
            </label>
            <textarea
              id="q"
              ref={textareaRef}
              rows={1}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="Задайте вопрос по праву…"
              className="flex-1 resize-none bg-transparent outline-none text-[14.5px] py-1.5 leading-[1.4] max-h-32"
            />
            {busy ? (
              <button
                type="button"
                aria-label="Остановить генерацию"
                onClick={onStop}
                className="w-9 h-9 shrink-0 rounded-full bg-accent text-[#1a1408] flex items-center justify-center"
              >
                <StopIcon />
              </button>
            ) : (
              <button
                type="submit"
                aria-label="Отправить запрос"
                disabled={!input.trim()}
                className="w-9 h-9 shrink-0 rounded-full bg-accent text-[#1a1408] flex items-center justify-center disabled:opacity-40"
              >
                <SendIcon />
              </button>
            )}
          </form>
          <div className="text-center text-[11px] text-text-tertiary mt-2">
            LegalSearch может ошибаться. Проверяйте нормы по первоисточнику.
          </div>
        </div>
      </div>
    </main>
  );
}

const EXAMPLES = [
  "Сосед затопил квартиру — кто должен возмещать ущерб?",
  "Какой срок исковой давности по трудовым спорам?",
  "Как расторгнуть договор аренды досрочно?",
];

function EmptyState({ onPick }: { onPick: (q: string) => void }) {
  return (
    <div className="flex flex-col items-center gap-5 pt-16 text-center">
      <LogoMark size={34} />
      <div className="font-display text-[22px] font-bold">Чем помочь по праву?</div>
      <div className="flex flex-col gap-2 w-full max-w-[420px]">
        {EXAMPLES.map((q) => (
          <button
            key={q}
            onClick={() => onPick(q)}
            className="text-left text-[13.5px] text-text-secondary border border-border bg-surface rounded-[10px] px-3.5 py-2.5"
          >
            {q}
          </button>
        ))}
      </div>
    </div>
  );
}

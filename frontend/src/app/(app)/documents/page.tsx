"use client";

import { useRef, useState } from "react";
import { useDocuments } from "@/lib/documents-context";
import { deleteDocument, uploadDocument, ApiError } from "@/lib/api";
import { formatBytes, formatDate } from "@/lib/format";
import { AlertIcon, CheckIcon, SpinnerIcon, UploadIcon, XIcon } from "@/components/icons";
import type { DocumentResponse } from "@/lib/types";

export default function DocumentsPage() {
  const { documents, loading, refresh } = useDocuments();
  const [uploading, setUploading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  async function onFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    e.target.value = ""; // чтобы можно было выбрать тот же файл повторно
    if (!file) return;

    setError(null);
    setUploading(true);
    try {
      await uploadDocument(file);
      await refresh();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "Не удалось загрузить файл");
    } finally {
      setUploading(false);
    }
  }

  async function onDelete(doc: DocumentResponse) {
    if (!confirm(`Удалить «${doc.filename}»? Документ перестанет участвовать в поиске.`)) return;
    await deleteDocument(doc.id);
    await refresh();
  }

  return (
    <main className="flex-1 min-w-0 h-full overflow-y-auto">
      <div className="max-w-[760px] mx-auto px-8 py-9 pb-16">
        <div className="flex items-center justify-between mb-2">
          <h1 className="font-display text-[26px] font-bold">Документы</h1>
          <button
            onClick={() => inputRef.current?.click()}
            disabled={uploading}
            className="h-9 rounded-[10px] border border-accent-soft-2 bg-accent-soft text-accent-text font-semibold text-[13px] px-3.5 flex items-center gap-2 disabled:opacity-50"
          >
            <UploadIcon size={14} />
            {uploading ? "Загружаем…" : "Загрузить PDF"}
          </button>
          <input ref={inputRef} type="file" accept="application/pdf" className="hidden" onChange={onFileChosen} />
        </div>
        <p className="text-[13px] text-text-secondary mb-6">
          Загруженные документы участвуют в поиске наравне с публичной базой, но видны только вашей компании.
        </p>

        {error && <div className="text-[13.5px] text-danger mb-4">{error}</div>}

        {loading && <div className="text-text-tertiary text-sm">Загрузка…</div>}

        {!loading && documents.length === 0 && (
          <div className="text-text-tertiary text-sm">
            Пока нет загруженных документов — добавьте первый PDF кнопкой выше.
          </div>
        )}

        <div className="flex flex-col gap-2">
          {documents.map((doc) => (
            <DocumentRow key={doc.id} doc={doc} onDelete={() => onDelete(doc)} />
          ))}
        </div>
      </div>
    </main>
  );
}

function DocumentRow({ doc, onDelete }: { doc: DocumentResponse; onDelete: () => void }) {
  return (
    <div className="border border-border bg-surface rounded-[10px] p-3.5 flex items-center gap-3">
      <StatusBadge status={doc.status} />
      <div className="flex-1 min-w-0">
        <div className="text-[14px] font-semibold truncate">{doc.filename}</div>
        <div className="text-[12px] text-text-tertiary mt-0.5">
          {formatBytes(doc.file_size)} · {formatDate(doc.created_at)}
        </div>
        {doc.status === "failed" && doc.error_message && (
          <div className="text-[12px] text-danger mt-1">{doc.error_message}</div>
        )}
      </div>
      <button
        aria-label="Удалить документ"
        onClick={onDelete}
        className="w-7 h-7 rounded-md text-text-tertiary flex items-center justify-center shrink-0"
      >
        <XIcon />
      </button>
    </div>
  );
}

function StatusBadge({ status }: { status: DocumentResponse["status"] }) {
  const map = {
    processing: { icon: <SpinnerIcon />, cls: "text-text-tertiary bg-surface-2", label: "Обрабатывается" },
    indexed: { icon: <CheckIcon />, cls: "text-accent-text bg-accent-soft", label: "Готово" },
    failed: { icon: <AlertIcon />, cls: "text-danger bg-surface-2", label: "Ошибка" },
  }[status];

  return (
    <div
      title={map.label}
      className={`w-8 h-8 shrink-0 rounded-full flex items-center justify-center ${map.cls}`}
    >
      {map.icon}
    </div>
  );
}

// Полное логирование действий пользователя на сайте (клики/ввод/навигация) —
// отправляется на backend (/telemetry/events), который пишет в activity_events.
// См. backend/src/routers/telemetry.py, backend/src/services/activity_log.py.
// Решение о том, хранить ли СЫРОЙ текст введённых значений (не только факт
// события), принимает backend (settings.log_input_content) — фронт всегда
// отправляет данные как есть, поэтому отключение в будущем не требует
// передеплоя фронтенда.
//
// Пароли не собираются вообще, на уровне слушателя (см. TelemetryProvider) —
// не как подстраховка "на сервере вырежут", а чтобы они физически не попадали
// в тело запроса.

import { getToken } from "./api";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const SESSION_KEY = "legalsearch-session-id";

export function getSessionId(): string {
  if (typeof window === "undefined") return "";
  let id = localStorage.getItem(SESSION_KEY);
  if (!id) {
    id = crypto.randomUUID();
    localStorage.setItem(SESSION_KEY, id);
  }
  return id;
}

type QueuedEvent = { event_type: string; session_id: string; payload: Record<string, unknown> };

let queue: QueuedEvent[] = [];
let flushTimer: ReturnType<typeof setTimeout> | null = null;

const FLUSH_INTERVAL_MS = 2000;
const MAX_BATCH_SIZE = 20;

function flush() {
  if (flushTimer) {
    clearTimeout(flushTimer);
    flushTimer = null;
  }
  if (queue.length === 0) return;
  const events = queue;
  queue = [];

  const token = getToken();
  const body = JSON.stringify({ events });

  // sendBeacon переживает закрытие/переход страницы (обычный fetch может
  // оборваться) — но не умеет заголовки, поэтому Authorization кладём только
  // когда он не критичен (fetch keepalive), а sendBeacon используем как
  // резервный путь на unload, где current_user всё равно не обязателен
  // (session_id уже привязывает события в цепочку).
  if (typeof navigator.sendBeacon === "function" && document.visibilityState === "hidden") {
    const blob = new Blob([body], { type: "application/json" });
    if (navigator.sendBeacon(`${API_URL}/telemetry/events`, blob)) return;
  }

  fetch(`${API_URL}/telemetry/events`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    body,
    keepalive: true,
  }).catch(() => {
    // Телеметрия не должна мешать пользователю — молча теряем пачку при сбое сети.
  });
}

export function track(eventType: string, payload: Record<string, unknown> = {}) {
  if (typeof window === "undefined") return;
  // client_ts — момент САМОГО события (клик/вывод результата/отправка
  // запроса), а не момент, когда пачка доехала до backend (та летит раз в
  // FLUSH_INTERVAL_MS — до 2с задержки, плюс сеть). Берётся здесь, в
  // момент вызова track(), а не в flush() — иначе разницы с server-side
  // created_at не было бы смысла фиксировать отдельно. Явный запрос
  // пользователя 2026-11-04: время каждой операции нужно "в этот самый
  // момент", не когда она доехала до сервера.
  const client_ts = new Date().toISOString();
  queue.push({ event_type: eventType, session_id: getSessionId(), payload: { ...payload, client_ts } });
  if (queue.length >= MAX_BATCH_SIZE) {
    flush();
    return;
  }
  if (!flushTimer) {
    flushTimer = setTimeout(flush, FLUSH_INTERVAL_MS);
  }
}

export function setupTelemetryFlushOnExit() {
  if (typeof window === "undefined") return;
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") flush();
  });
  window.addEventListener("pagehide", flush);
}

"use client";

import { useEffect, type ReactNode } from "react";
import { usePathname } from "next/navigation";
import { track, setupTelemetryFlushOnExit } from "./telemetry";

// Максимальная длина текста кнопки/ссылки в событии клика — защита от
// случайного попадания большого куска текста страницы в лог, не от утечки
// (сам клик по определению на видимом пользователю элементе).
const CLICK_TEXT_MAX_LEN = 120;

function describeClickTarget(el: Element) {
  const button = el.closest("button, a, [role='button'], input[type='submit'], input[type='button']");
  const target = button ?? el;
  return {
    tag: target.tagName.toLowerCase(),
    text: (target.textContent || "").trim().slice(0, CLICK_TEXT_MAX_LEN),
    href: target instanceof HTMLAnchorElement ? target.href : null,
    id: target.id || null,
  };
}

export function TelemetryProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  // Переходы между страницами — App Router не даёт единого "route change"
  // события, но смена pathname в клиентском компоненте срабатывает надёжно
  // при обычной навигации (Link/router.push), чего достаточно для журнала.
  useEffect(() => {
    track("page_view", { path: pathname });
  }, [pathname]);

  useEffect(() => {
    setupTelemetryFlushOnExit();

    function onClick(e: MouseEvent) {
      if (!(e.target instanceof Element)) return;
      track("click", { ...describeClickTarget(e.target), path: window.location.pathname });
    }

    function onInput(e: Event) {
      const el = e.target;
      if (!(el instanceof HTMLInputElement) && !(el instanceof HTMLTextAreaElement)) return;
      // Пароль не собираем НИКОГДА — физически не формируем событие, а не
      // полагаемся только на редактирование на backend.
      if (el instanceof HTMLInputElement && el.type === "password") return;
      track("input_change", {
        name: el.name || el.id || null,
        type: el instanceof HTMLInputElement ? el.type : "textarea",
        value: el.value,
        path: window.location.pathname,
      });
    }

    // click — на capture-фазе, чтобы ловить даже если конкретный обработчик
    // ниже остановит всплытие (stopPropagation) кастомной кнопкой.
    document.addEventListener("click", onClick, true);
    // input/blur, не keyup — не нужен поток событий на каждое нажатие
    // клавиши, важно финальное значение поля.
    document.addEventListener("blur", onInput, true);

    return () => {
      document.removeEventListener("click", onClick, true);
      document.removeEventListener("blur", onInput, true);
    };
  }, []);

  return <>{children}</>;
}

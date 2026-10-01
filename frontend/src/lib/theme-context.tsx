"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";

type Theme = "dark" | "light";

const THEME_KEY = "legalsearch-theme";

const ThemeContext = createContext<{ theme: Theme; toggle: () => void } | null>(null);

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    // localStorage недоступен при SSR, поэтому читаем сохранённую тему только
    // на клиенте (root layout уже проставил data-theme синхронным скриптом,
    // здесь просто синхронизируем React-состояние с тем, что реально на DOM).
    const saved = localStorage.getItem(THEME_KEY);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (saved === "light" || saved === "dark") setTheme(saved);
  }, []);

  useEffect(() => {
    if (theme === "light") {
      document.documentElement.setAttribute("data-theme", "light");
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
    localStorage.setItem(THEME_KEY, theme);
  }, [theme]);

  function toggle() {
    setTheme((t) => (t === "dark" ? "light" : "dark"));
  }

  return <ThemeContext.Provider value={{ theme, toggle }}>{children}</ThemeContext.Provider>;
}

export function useTheme() {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme должен использоваться внутри ThemeProvider");
  return ctx;
}

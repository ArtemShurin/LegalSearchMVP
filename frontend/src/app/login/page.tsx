"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { login, me } from "@/lib/api";
import { useAuth } from "@/lib/auth-context";
import { LogoMark } from "@/components/icons";

export default function LoginPage() {
  const router = useRouter();
  const { setUser } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(email, password);
      const user = await me();
      setUser(user);
      router.push("/search");
    } catch {
      setError("Неверный email или пароль");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex-1 flex items-center justify-center px-4">
      <div className="w-full max-w-[380px]">
        <div className="flex items-center justify-center gap-2 mb-8">
          <LogoMark size={26} />
          <span className="font-display text-[22px] font-bold">LegalSearch</span>
        </div>

        <form onSubmit={onSubmit} className="flex flex-col gap-4 bg-surface border border-border rounded-2xl p-7">
          <h1 className="font-display text-[19px] font-bold text-center mb-1">Вход</h1>

          <div>
            <label htmlFor="email" className="block text-[12.5px] text-text-secondary mb-1.5">
              Email
            </label>
            <input
              id="email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="w-full h-10 rounded-[10px] border border-border bg-bg text-text text-[14px] px-3 outline-none"
              placeholder="you@company.ru"
            />
          </div>

          <div>
            <label htmlFor="password" className="block text-[12.5px] text-text-secondary mb-1.5">
              Пароль
            </label>
            <input
              id="password"
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full h-10 rounded-[10px] border border-border bg-bg text-text text-[14px] px-3 outline-none"
              placeholder="••••••••"
            />
          </div>

          {error && <div className="text-[13px] text-danger">{error}</div>}

          <button
            type="submit"
            disabled={busy}
            className="h-10 rounded-[10px] bg-accent text-[#1a1408] font-semibold text-[14px] mt-1 disabled:opacity-60"
          >
            {busy ? "Входим…" : "Войти"}
          </button>

          <div className="text-center text-[13px] text-text-secondary mt-1">
            Нет аккаунта?{" "}
            <Link href="/register" className="text-accent-text font-medium">
              Зарегистрироваться
            </Link>
          </div>
        </form>
      </div>
    </div>
  );
}

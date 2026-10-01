"use client";

import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { clearToken, getToken, me } from "./api";
import type { User } from "./types";

type AuthState = {
  user: User | null;
  loading: boolean;
  setUser: (u: User | null) => void;
  logout: () => void;
};

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    // Читаем токен из localStorage — доступно только на клиенте, поэтому не
    // можем вычислить это в initial state (SSR).
    const token = getToken();
    if (!token) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setLoading(false);
      return;
    }
    me()
      .then(setUser)
      .catch(() => clearToken())
      .finally(() => setLoading(false));
  }, []);

  function logout() {
    clearToken();
    setUser(null);
  }

  return (
    <AuthContext.Provider value={{ user, loading, setUser, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth должен использоваться внутри AuthProvider");
  return ctx;
}

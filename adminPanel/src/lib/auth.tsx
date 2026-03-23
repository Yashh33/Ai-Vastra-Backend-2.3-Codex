import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { verifyAdminSession } from "./api";
import {
  normalizeApiBaseUrl,
  readSessionFromStorage,
  writeSessionToStorage,
} from "./storage";
import type { AdminSession } from "./types";

type AdminAuthContextValue = {
  session: AdminSession | null;
  isAuthenticated: boolean;
  signIn: (payload: { apiBaseUrl: string; adminSecret: string }) => Promise<void>;
  signOut: () => void;
};

const AdminAuthContext = createContext<AdminAuthContextValue | undefined>(undefined);

export function AdminAuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AdminSession | null>(() => readSessionFromStorage());

  const value = useMemo<AdminAuthContextValue>(
    () => ({
      session,
      isAuthenticated: !!session,
      signIn: async ({ apiBaseUrl, adminSecret }) => {
        const normalized: AdminSession = {
          apiBaseUrl: normalizeApiBaseUrl(apiBaseUrl),
          adminSecret: adminSecret.trim(),
        };

        if (!normalized.apiBaseUrl || !normalized.adminSecret) {
          throw new Error("Backend URL and admin secret are required");
        }

        await verifyAdminSession(normalized);
        setSession(normalized);
        writeSessionToStorage(normalized);
      },
      signOut: () => {
        setSession(null);
        writeSessionToStorage(null);
      },
    }),
    [session]
  );

  return <AdminAuthContext.Provider value={value}>{children}</AdminAuthContext.Provider>;
}

export function useAdminAuth() {
  const ctx = useContext(AdminAuthContext);
  if (!ctx) {
    throw new Error("useAdminAuth must be used within AdminAuthProvider");
  }
  return ctx;
}

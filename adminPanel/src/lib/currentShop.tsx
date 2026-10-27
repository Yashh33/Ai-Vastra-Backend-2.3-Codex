import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from "react";

import type { AdminShopRow } from "./types";

/** The shop opened last: its menu stays in the left menu while Prompt library / Operations are open. */
const STORAGE_KEY = "aivastra_admin_current_shop";

type CurrentShop = { id: string; name: string; row: AdminShopRow | null };

type CurrentShopContextValue = {
  current: CurrentShop | null;
  setShopRow: (row: AdminShopRow) => void;
  patchShopRow: (patch: Partial<AdminShopRow>) => void;
  clear: () => void;
};

const CurrentShopContext = createContext<CurrentShopContextValue | undefined>(undefined);

function readStored(): CurrentShop | null {
  try {
    const parsed = JSON.parse(window.sessionStorage.getItem(STORAGE_KEY) || "null") as { id?: string; name?: string } | null;
    return parsed?.id ? { id: parsed.id, name: parsed.name || "", row: null } : null;
  } catch {
    return null;
  }
}

function store(value: CurrentShop | null) {
  try {
    if (value) window.sessionStorage.setItem(STORAGE_KEY, JSON.stringify({ id: value.id, name: value.name }));
    else window.sessionStorage.removeItem(STORAGE_KEY);
  } catch {
    // storage blocked: the menu just forgets the shop on reload
  }
}

export function CurrentShopProvider({ children }: { children: ReactNode }) {
  const [current, setCurrent] = useState<CurrentShop | null>(() => readStored());

  const setShopRow = useCallback((row: AdminShopRow) => {
    const next = { id: row.id, name: row.name || "", row };
    store(next);
    setCurrent(next);
  }, []);

  const patchShopRow = useCallback((patch: Partial<AdminShopRow>) => {
    setCurrent((prev) => {
      if (!prev?.row) return prev;
      const row = { ...prev.row, ...patch };
      const next = { id: prev.id, name: row.name || prev.name, row };
      store(next);
      return next;
    });
  }, []);

  const clear = useCallback(() => {
    store(null);
    setCurrent(null);
  }, []);

  const value = useMemo(() => ({ current, setShopRow, patchShopRow, clear }), [current, setShopRow, patchShopRow, clear]);
  return <CurrentShopContext.Provider value={value}>{children}</CurrentShopContext.Provider>;
}

export function useCurrentShop() {
  const ctx = useContext(CurrentShopContext);
  if (!ctx) throw new Error("useCurrentShop must be used within CurrentShopProvider");
  return ctx;
}

export function shopStatusLabel(row: AdminShopRow | null | undefined): string {
  if (!row) return "";
  if (row.is_suspended) return "Suspended";
  if (row.status === "trial") return "Trial";
  return "Active";
}

import { useCallback, useEffect, useState } from "react";
import { Outlet, useOutletContext, useParams } from "react-router-dom";

import { adminFetch } from "../../lib/api";
import { useAdminAuth } from "../../lib/auth";
import { useCurrentShop } from "../../lib/currentShop";
import { errorMessage } from "../../lib/promptLibrary";
import type { AdminSession, AdminShopRow } from "../../lib/types";

export type ShopOutletContext = {
  session: AdminSession;
  shopId: string;
  shop: AdminShopRow;
  reloadShop: () => Promise<void>;
  patchShop: (patch: Partial<AdminShopRow>) => void;
};

export function useShopScreen() {
  return useOutletContext<ShopOutletContext>();
}

/** Loads the open shop once for all its screens (Garments, Bulk looks, Shop settings, Credits). */
export function ShopLayout() {
  const { shopId = "" } = useParams();
  const { session } = useAdminAuth();
  const { current, setShopRow, patchShopRow } = useCurrentShop();
  const [error, setError] = useState("");

  const reloadShop = useCallback(async () => {
    if (!session || !shopId) return;
    try {
      const row = await adminFetch<AdminShopRow>(session, `/admin/shops/${encodeURIComponent(shopId)}`);
      setShopRow(row);
      setError("");
    } catch (err) {
      setError(errorMessage(err));
    }
  }, [session, shopId, setShopRow]);

  useEffect(() => {
    void reloadShop();
  }, [reloadShop]);

  const shop = current?.id === shopId ? current.row : null;

  if (error && !shop) {
    return (
      <div className="page">
        <p className="error-text">Could not load this shop: {error}</p>
      </div>
    );
  }
  if (!shop || !session) {
    return (
      <div className="page">
        <p className="muted">Loading shop…</p>
      </div>
    );
  }

  const context: ShopOutletContext = { session, shopId, shop, reloadShop, patchShop: patchShopRow };
  return <Outlet context={context} />;
}

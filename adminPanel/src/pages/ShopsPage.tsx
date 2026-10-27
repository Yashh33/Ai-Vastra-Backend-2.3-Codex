import { useEffect, useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { adminFetch } from "../lib/api";
import { useAdminAuth } from "../lib/auth";
import { copyText } from "../lib/clipboard";
import { shopStatusLabel, useCurrentShop } from "../lib/currentShop";
import { errorMessage } from "../lib/promptLibrary";
import type { AdminCreateShopResponse, AdminShopRow } from "../lib/types";

function formatPhone(raw: string | null | undefined): string {
  if (!raw) return "";
  if (raw.startsWith("91") && raw.length === 12) {
    return "+91 " + raw.slice(2);
  }
  return raw;
}

function channelLine(shop: AdminShopRow): string {
  if (shop.channel === "master") return "Master template";
  if (shop.channel === "whatsapp") return `WhatsApp · ${formatPhone(shop.whatsapp_phone) || "no number"}`;
  if (shop.channel === "react") return `Web app · ${shop.owner_email || "no email"}`;
  return "Unlinked";
}

export function ShopsPage() {
  const { session } = useAdminAuth();
  const { clear } = useCurrentShop();
  const navigate = useNavigate();

  const [shops, setShops] = useState<AdminShopRow[]>([]);
  const [searchText, setSearchText] = useState("");
  const [loading, setLoading] = useState(false);
  const [statusText, setStatusText] = useState("");
  const [errorText, setErrorText] = useState("");
  const [showTrial, setShowTrial] = useState(false);
  const [copiedSlugShopId, setCopiedSlugShopId] = useState<string | null>(null);

  const [panelOpen, setPanelOpen] = useState(false);
  const [shopName, setShopName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [openingCredits, setOpeningCredits] = useState("0");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState("");

  // The Shops list is "no shop open": the shop menu leaves the left menu.
  useEffect(() => {
    clear();
  }, [clear]);

  const canCreate = !!shopName.trim() && !!email.trim() && password.trim().length >= 6;

  const visibleShops = useMemo(() => {
    const needle = searchText.trim().toLowerCase();
    return shops.filter((shop) => {
      if (!showTrial && shop.status === "trial") return false;
      if (!needle) return true;
      return [shop.name, shop.screen_slug, shop.owner_email, shop.whatsapp_phone]
        .some((value) => (value || "").toLowerCase().includes(needle));
    });
  }, [shops, showTrial, searchText]);

  async function loadShops(search = "") {
    if (!session) return;
    setLoading(true);
    setErrorText("");
    try {
      const params = new URLSearchParams({ limit: "200" });
      if (search.trim()) params.set("search", search.trim());
      setShops(await adminFetch<AdminShopRow[]>(session, `/admin/shops?${params.toString()}`));
    } catch (err) {
      setErrorText(`Could not load shops: ${errorMessage(err)}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadShops();
  }, [session]);

  async function handleCopySlug(shopId: string, slug: string) {
    if (await copyText(slug)) {
      setCopiedSlugShopId(shopId);
      window.setTimeout(() => setCopiedSlugShopId((current) => (current === shopId ? null : current)), 1500);
    }
  }

  async function handleCreateShop(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !canCreate) return;

    setCreating(true);
    setCreateError("");
    try {
      const response = await adminFetch<AdminCreateShopResponse>(session, "/admin/shops", {
        method: "POST",
        body: JSON.stringify({
          shop_name: shopName.trim(),
          email: email.trim(),
          password,
          opening_credits: Math.max(0, Number(openingCredits) || 0),
          carousel_mode_default: false,
        }),
      });
      setShopName("");
      setEmail("");
      setPassword("");
      setOpeningCredits("0");
      setPanelOpen(false);
      setStatusText(`Shop created: ${response.shop_name} · login ${response.email} · ${response.balance_after} credits`);
      await loadShops();
    } catch (err) {
      setCreateError(`Create failed: ${errorMessage(err)}`);
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Shops</h1>
          <p className="muted">{loading ? "Loading…" : `${visibleShops.length} of ${shops.length} shops`}</p>
        </div>
        <div className="row">
          <button className="btn" type="button" onClick={() => void loadShops(searchText)} disabled={loading}>
            Refresh
          </button>
          <button className="btn btn-primary" type="button" onClick={() => setPanelOpen(true)}>
            + New shop
          </button>
        </div>
      </header>

      {statusText ? <p className="notice">{statusText}</p> : null}
      {errorText ? <p className="error-text">{errorText}</p> : null}

      <div className="toolbar">
        <form
          className="row"
          onSubmit={(event) => {
            event.preventDefault();
            void loadShops(searchText);
          }}
        >
          <input
            className="input search-input"
            placeholder="Search shops by name, TV slug, email or phone"
            value={searchText}
            onChange={(event) => setSearchText(event.target.value)}
          />
        </form>
        <label className="check">
          <input type="checkbox" checked={showTrial} onChange={(event) => setShowTrial(event.target.checked)} />
          Show trial shops
        </label>
      </div>

      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>Shop</th>
              <th className="num">Credits</th>
              <th className="num">Garments</th>
              <th>TV slug</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {visibleShops.length === 0 ? (
              <tr>
                <td colSpan={5} className="empty-cell">
                  {loading ? "Loading…" : "No shops match."}
                </td>
              </tr>
            ) : (
              visibleShops.map((shop) => (
                <tr
                  key={shop.id}
                  className="row-link"
                  tabIndex={0}
                  onClick={() => navigate(`/shops/${encodeURIComponent(shop.id)}/garments`)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter") navigate(`/shops/${encodeURIComponent(shop.id)}/garments`);
                  }}
                >
                  <td>
                    <div className="cell-title">{shop.name}</div>
                    <div className="tiny muted">{channelLine(shop)}</div>
                  </td>
                  <td className="num">{shop.credits_balance ?? 0}</td>
                  <td className="num">{shop.garments_count ?? "—"}</td>
                  <td>
                    {shop.screen_slug ? (
                      <span className="row tight">
                        <code>{shop.screen_slug}</code>
                        <button
                          type="button"
                          className="btn btn-small"
                          onClick={(event) => {
                            event.stopPropagation();
                            void handleCopySlug(shop.id, shop.screen_slug as string);
                          }}
                        >
                          {copiedSlugShopId === shop.id ? "Copied" : "Copy"}
                        </button>
                      </span>
                    ) : (
                      <span className="muted">Not set</span>
                    )}
                  </td>
                  <td>
                    <span className={shop.is_suspended ? "pill pill-bad" : shop.status === "trial" ? "pill pill-warn" : "pill pill-ok"}>
                      {shopStatusLabel(shop)}
                    </span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {panelOpen ? (
        <div className="drawer-overlay" onClick={() => !creating && setPanelOpen(false)}>
          <aside className="drawer drawer-narrow" onClick={(event) => event.stopPropagation()} aria-label="New shop">
            <header className="drawer-head">
              <h2>New shop</h2>
              <button type="button" className="btn btn-ghost" onClick={() => setPanelOpen(false)} disabled={creating}>
                Close
              </button>
            </header>
            <form className="stack" onSubmit={handleCreateShop}>
              <label className="field">
                <span>Shop name</span>
                <input className="input" value={shopName} onChange={(event) => setShopName(event.target.value)} placeholder="e.g. ABC Mens Wear" disabled={creating} autoFocus />
              </label>
              <label className="field">
                <span>Login email</span>
                <input className="input" type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="shop@example.com" disabled={creating} />
              </label>
              <label className="field">
                <span>Password</span>
                <input className="input" type="text" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="At least 6 characters" disabled={creating} />
              </label>
              <label className="field">
                <span>Opening credits (optional)</span>
                <input className="input" type="number" min={0} value={openingCredits} onChange={(event) => setOpeningCredits(event.target.value)} disabled={creating} />
              </label>
              {createError ? <p className="error-text">{createError}</p> : null}
              <div className="row">
                <button className="btn btn-primary" type="submit" disabled={creating || !canCreate}>
                  {creating ? "Creating…" : "Create shop"}
                </button>
                <button className="btn" type="button" onClick={() => setPanelOpen(false)} disabled={creating}>
                  Cancel
                </button>
              </div>
            </form>
          </aside>
        </div>
      ) : null}
    </div>
  );
}

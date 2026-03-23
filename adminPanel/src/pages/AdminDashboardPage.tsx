import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { adminFetch } from "../lib/api";
import { useAdminAuth } from "../lib/auth";
import type { AdminCreateShopResponse, AdminShopRow } from "../lib/types";

export function AdminDashboardPage() {
  const { session, signOut } = useAdminAuth();

  const [shops, setShops] = useState<AdminShopRow[]>([]);
  const [searchText, setSearchText] = useState("");
  const [loading, setLoading] = useState(false);
  const [statusText, setStatusText] = useState("Loading shops...");

  const [shopName, setShopName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [openingCredits, setOpeningCredits] = useState("0");
  const [creating, setCreating] = useState(false);

  const canCreate = useMemo(
    () => !!shopName.trim() && !!email.trim() && password.trim().length >= 6,
    [shopName, email, password]
  );

  async function loadShops() {
    if (!session) return;
    setLoading(true);
    try {
      const params = new URLSearchParams({ limit: "100" });
      if (searchText.trim()) {
        params.set("search", searchText.trim());
      }

      const rows = await adminFetch<AdminShopRow[]>(session, `/admin/shops?${params.toString()}`, {
        method: "GET",
      });

      setShops(rows);
      setStatusText(rows.length ? `Loaded ${rows.length} shop(s)` : "No shops found");
    } catch (err) {
      setStatusText(`Failed to load shops: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadShops();
  }, [session]);

  async function handleCreateShop(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session) return;

    setCreating(true);
    try {
      const payload = {
        shop_name: shopName.trim(),
        email: email.trim(),
        password,
        opening_credits: Math.max(0, Number(openingCredits) || 0),
        carousel_mode_default: false,
      };

      const response = await adminFetch<AdminCreateShopResponse>(session, "/admin/shops", {
        method: "POST",
        body: JSON.stringify(payload),
      });

      setShopName("");
      setEmail("");
      setPassword("");
      setOpeningCredits("0");

      setStatusText(
        `Shop created: ${response.shop_name} | Login: ${response.email} | Balance: ${response.balance_after}`
      );

      await loadShops();
    } catch (err) {
      setStatusText(`Create failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setCreating(false);
    }
  }

  return (
    <main className="screen">
      <section className="page-shell">
        <header className="header-row">
          <div>
            <h1>Admin Panel</h1>
            <p className="muted">Onboard shops and manage catalog setup.</p>
            <p className="tiny muted">{statusText}</p>
          </div>
          <div className="row">
            <button className="btn btn-light" onClick={loadShops} disabled={loading}>
              {loading ? "Refreshing..." : "Refresh"}
            </button>
            <button className="btn btn-light" onClick={signOut}>
              Logout
            </button>
          </div>
        </header>

        <section className="card stack">
          <h2>Create Shop Login</h2>
          <form className="stack" onSubmit={handleCreateShop}>
            <div className="grid-2">
              <label className="field">
                <span>Shop Name</span>
                <input
                  value={shopName}
                  onChange={(event) => setShopName(event.target.value)}
                  placeholder="e.g. ABC Mens Wear"
                  disabled={creating}
                />
              </label>

              <label className="field">
                <span>Shop Email (username)</span>
                <input
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="shop@example.com"
                  disabled={creating}
                />
              </label>

              <label className="field">
                <span>Password</span>
                <input
                  type="text"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="Minimum 6 characters"
                  disabled={creating}
                />
              </label>

              <label className="field">
                <span>Opening Credits</span>
                <input
                  type="number"
                  min={0}
                  value={openingCredits}
                  onChange={(event) => setOpeningCredits(event.target.value)}
                  disabled={creating}
                />
              </label>
            </div>

            <button className="btn btn-dark" type="submit" disabled={creating || !canCreate}>
              {creating ? "Creating..." : "Create Shop"}
            </button>
          </form>
        </section>

        <section className="card stack">
          <div className="row">
            <h2 className="grow">Shops</h2>
            <input
              className="search-input"
              placeholder="Search by name"
              value={searchText}
              onChange={(event) => setSearchText(event.target.value)}
            />
            <button className="btn btn-light" onClick={loadShops} disabled={loading}>
              Search
            </button>
          </div>

          {shops.length === 0 ? (
            <div className="empty-box">No shops yet.</div>
          ) : (
            <div className="shop-grid">
              {shops.map((shop) => (
                <article key={shop.id} className="shop-card">
                  <h3>{shop.name}</h3>
                  <p className="tiny muted">Shop ID: {shop.id}</p>
                  <p className="tiny muted">Credits: {shop.credits_balance ?? 0}</p>
                  <p className="tiny muted">Owner UID: {shop.owner_auth_user_id || "-"}</p>
                  <Link className="btn btn-dark" to={`/shops/${encodeURIComponent(shop.id)}`}>
                    Manage Shop
                  </Link>
                </article>
              ))}
            </div>
          )}
        </section>
      </section>
    </main>
  );
}



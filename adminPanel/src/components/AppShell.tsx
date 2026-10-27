import { NavLink, Outlet, useLocation } from "react-router-dom";

import { useAdminAuth } from "../lib/auth";
import { shopStatusLabel, useCurrentShop } from "../lib/currentShop";
import { looksFor } from "../lib/promptLibrary";

function MenuLink({ to, label, end }: { to: string; label: string; end?: boolean }) {
  return (
    <NavLink to={to} end={end} className={({ isActive }) => (isActive ? "menu-link is-active" : "menu-link")}>
      {label}
    </NavLink>
  );
}

/** Top bar + left menu around every screen after login. */
export function AppShell() {
  const { signOut } = useAdminAuth();
  const { current } = useCurrentShop();
  const { pathname } = useLocation();
  const onShopScreen = !!current && pathname.startsWith(`/shops/${current.id}`);
  const row = onShopScreen ? current?.row : null;
  const base = current ? `/shops/${encodeURIComponent(current.id)}` : "";

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-title">
          <NavLink to="/" className="brand">
            MyTryonAi admin
          </NavLink>
          {current ? (
            <>
              <span className="crumb-sep">/</span>
              <span className="crumb-shop">{current.name || "Shop"}</span>
            </>
          ) : null}
        </div>
        {row ? (
          <div className="topbar-meta">
            <span>
              <strong>{row.credits_balance ?? 0}</strong> credits{" "}
              <span className="muted">≈ {looksFor(row.credits_balance ?? 0)} looks</span>
            </span>
            <span className={row.is_suspended ? "pill pill-bad" : "pill pill-ok"}>{shopStatusLabel(row)}</span>
          </div>
        ) : null}
      </header>

      <nav className="sidenav" aria-label="Main menu">
        <MenuLink to="/" label="Shops" end />
        {current ? (
          <div className="menu-group">
            <span className="menu-group-title" title={current.name}>
              {current.name || "Shop"}
            </span>
            <MenuLink to={`${base}/garments`} label="Garments" />
            <MenuLink to={`${base}/bulk-looks`} label="Bulk looks" />
            <MenuLink to={`${base}/settings`} label="Shop settings" />
            <MenuLink to={`${base}/credits`} label="Credits" />
          </div>
        ) : null}
        <hr className="menu-divider" />
        <span className="menu-group-title">All shops</span>
        <MenuLink to="/prompt-library" label="Prompt library" />
        <MenuLink to="/operations" label="Operations" />
        <div className="menu-spacer" />
        <button type="button" className="menu-link menu-logout" onClick={signOut}>
          Log out
        </button>
      </nav>

      <main className="main">
        <Outlet />
      </main>
    </div>
  );
}

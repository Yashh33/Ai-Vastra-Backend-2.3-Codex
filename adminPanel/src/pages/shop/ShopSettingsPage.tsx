import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { adminFetch } from "../../lib/api";
import { copyText } from "../../lib/clipboard";
import { useCurrentShop } from "../../lib/currentShop";
import { errorMessage } from "../../lib/promptLibrary";
import type { AdminShopRow } from "../../lib/types";
import { useShopScreen } from "./ShopLayout";

// Same rule as the backend (admin_api._SCREEN_SLUG_RE).
const SCREEN_SLUG_RE = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/;
const SCREEN_SLUG_RULE =
  "TV slug must be 3–40 characters: lowercase letters, numbers and hyphens, and cannot start or end with a hyphen.";

function CopyValue({ label, value }: { label: string; value: string | null | undefined }) {
  const [copied, setCopied] = useState(false);
  return (
    <div className="kv">
      <span className="kv-label">{label}</span>
      {value ? (
        <span className="row tight">
          <code className="small-code">{value}</code>
          <button
            type="button"
            className="btn btn-small"
            onClick={async () => {
              if (await copyText(value)) {
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1500);
              }
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </span>
      ) : (
        <span className="muted">—</span>
      )}
    </div>
  );
}

export function ShopSettingsPage() {
  const { session, shopId, shop, patchShop, reloadShop } = useShopScreen();
  const { clear } = useCurrentShop();
  const navigate = useNavigate();
  const base = `/admin/shops/${encodeURIComponent(shopId)}`;

  const [shopName, setShopName] = useState(shop.name || "");
  const [headerDisplayText, setHeaderDisplayText] = useState(shop.header_display_text || "");
  const [carouselModeDefault, setCarouselModeDefault] = useState(Boolean(shop.carousel_mode_default));
  const [savingShop, setSavingShop] = useState(false);
  const [detailsStatus, setDetailsStatus] = useState("");

  const [logoFile, setLogoFile] = useState<File | null>(null);
  const [logoInputKey, setLogoInputKey] = useState(0);
  const [uploadingLogo, setUploadingLogo] = useState(false);

  const [editingSlug, setEditingSlug] = useState(false);
  const [slugDraft, setSlugDraft] = useState("");
  const [slugError, setSlugError] = useState("");
  const [savingSlug, setSavingSlug] = useState(false);
  const [suggestingSlug, setSuggestingSlug] = useState(false);
  const [slugStatus, setSlugStatus] = useState("");

  const [newPassword, setNewPassword] = useState("");
  const [resettingPassword, setResettingPassword] = useState(false);
  const [loginStatus, setLoginStatus] = useState("");

  const [processingSuspend, setProcessingSuspend] = useState(false);
  const [deletingShop, setDeletingShop] = useState(false);
  const [dangerStatus, setDangerStatus] = useState("");

  useEffect(() => {
    setShopName(shop.name || "");
    setHeaderDisplayText(shop.header_display_text || "");
    setCarouselModeDefault(Boolean(shop.carousel_mode_default));
  }, [shop.id]);

  async function handleShopUpdate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSavingShop(true);
    try {
      const updated = await adminFetch<AdminShopRow>(session, base, {
        method: "PATCH",
        body: JSON.stringify({
          name: shopName.trim(),
          header_display_text: headerDisplayText.trim() || null,
          carousel_mode_default: carouselModeDefault,
        }),
      });
      patchShop(updated);
      setDetailsStatus("Shop details saved.");
    } catch (err) {
      setDetailsStatus(`Save failed: ${errorMessage(err)}`);
    } finally {
      setSavingShop(false);
    }
  }

  async function handleLogoUpload() {
    if (!logoFile) return;
    setUploadingLogo(true);
    try {
      const formData = new FormData();
      formData.append("file", logoFile);
      const result = await adminFetch<{ logo_path: string }>(session, `${base}/logo`, { method: "POST", body: formData });
      patchShop({ logo_path: result.logo_path });
      setLogoFile(null);
      setLogoInputKey((key) => key + 1);
      setDetailsStatus("Logo uploaded.");
    } catch (err) {
      setDetailsStatus(`Logo upload failed: ${errorMessage(err)}`);
    } finally {
      setUploadingLogo(false);
    }
  }

  function startEditingSlug() {
    setSlugDraft(shop.screen_slug || "");
    setSlugError("");
    setEditingSlug(true);
  }

  async function handleSuggestSlug() {
    setSuggestingSlug(true);
    setSlugError("");
    try {
      const result = await adminFetch<{ screen_slug: string }>(session, `${base}/screen-slug-suggestion`);
      setSlugDraft(result.screen_slug);
    } catch (err) {
      setSlugError(errorMessage(err));
    } finally {
      setSuggestingSlug(false);
    }
  }

  async function handleSaveSlug() {
    const slug = slugDraft.trim().toLowerCase();
    if (!SCREEN_SLUG_RE.test(slug)) {
      setSlugError(SCREEN_SLUG_RULE);
      return;
    }
    const previous = shop.screen_slug || "";
    if (slug === previous) {
      setEditingSlug(false);
      return;
    }
    if (previous && !window.confirm("TVs using the old slug will need to be set up again.")) {
      return;
    }
    setSavingSlug(true);
    setSlugError("");
    try {
      const result = await adminFetch<{ shop_id: string; screen_slug: string }>(session, `${base}/screen-slug`, {
        method: "PATCH",
        body: JSON.stringify({ screen_slug: slug }),
      });
      patchShop({ screen_slug: result.screen_slug });
      setEditingSlug(false);
      setSlugStatus(`TV slug saved: ${result.screen_slug}`);
    } catch (err) {
      setSlugError(errorMessage(err));
    } finally {
      setSavingSlug(false);
    }
  }

  async function handlePasswordReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (newPassword.trim().length < 6) return;
    setResettingPassword(true);
    try {
      await adminFetch<{ password_reset: boolean }>(session, `${base}/reset-password`, {
        method: "POST",
        body: JSON.stringify({ password: newPassword.trim() }),
      });
      setNewPassword("");
      setLoginStatus("Password reset.");
    } catch (err) {
      setLoginStatus(`Password reset failed: ${errorMessage(err)}`);
    } finally {
      setResettingPassword(false);
    }
  }

  async function handleToggleSuspend() {
    const nextSuspended = !shop.is_suspended;
    const label = nextSuspended ? "suspend" : "activate";
    if (!window.confirm(`Are you sure you want to ${label} this shop?`)) return;

    setProcessingSuspend(true);
    try {
      const result = await adminFetch<{ shop_id: string; is_suspended: boolean }>(session, `${base}/suspend`, {
        method: "POST",
        body: JSON.stringify({ suspended: nextSuspended }),
      });
      patchShop({ is_suspended: result.is_suspended });
      setDangerStatus(result.is_suspended ? "Shop suspended." : "Shop activated.");
    } catch (err) {
      setDangerStatus(`Suspend / activate failed: ${errorMessage(err)}`);
    } finally {
      setProcessingSuspend(false);
    }
  }

  async function handleDeleteShop() {
    const firstConfirm = window.confirm(
      "Delete this shop permanently? This will remove all folders, hero images, generations, and credits."
    );
    if (!firstConfirm) return;
    const secondConfirm = window.confirm("Final confirmation: this cannot be undone. Continue?");
    if (!secondConfirm) return;

    setDeletingShop(true);
    try {
      const result = await adminFetch<{ deleted: boolean; shop_name: string; warnings?: string[] }>(session, base, {
        method: "DELETE",
      });
      if (result.warnings?.length) {
        window.alert(`Shop deleted with warnings: ${result.warnings.join(" | ")}`);
      }
      clear();
      navigate("/", { replace: true });
    } catch (err) {
      setDangerStatus(`Delete failed: ${errorMessage(err)}`);
      setDeletingShop(false);
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Shop settings</h1>
          <p className="muted">{shop.name}</p>
        </div>
        <button className="btn" type="button" onClick={() => void reloadShop()}>
          Refresh
        </button>
      </header>

      <div className="settings-grid">
        <section className="section">
          <h2>Shop details</h2>
          <form className="stack" onSubmit={handleShopUpdate}>
            <div className="grid-2">
              <label className="field">
                <span>Shop name</span>
                <input className="input" value={shopName} onChange={(event) => setShopName(event.target.value)} />
              </label>
              <label className="field">
                <span>Header text</span>
                <input
                  className="input"
                  value={headerDisplayText}
                  onChange={(event) => setHeaderDisplayText(event.target.value)}
                  placeholder="e.g. Retero Fashion"
                />
              </label>
              <label className="field">
                <span>Carousel default</span>
                <select
                  className="input"
                  value={carouselModeDefault ? "true" : "false"}
                  onChange={(event) => setCarouselModeDefault(event.target.value === "true")}
                >
                  <option value="false">Off</option>
                  <option value="true">On</option>
                </select>
              </label>
            </div>

            <div className="field">
              <span>Logo</span>
              <p className="tiny muted">Current: {shop.logo_path || "none"}</p>
              <div className="row">
                <input
                  key={logoInputKey}
                  type="file"
                  accept="image/*"
                  onChange={(event) => setLogoFile(event.target.files?.[0] || null)}
                  disabled={uploadingLogo}
                />
                <button className="btn" type="button" onClick={() => void handleLogoUpload()} disabled={uploadingLogo || !logoFile}>
                  {uploadingLogo ? "Uploading…" : "Upload logo"}
                </button>
              </div>
            </div>

            <div className="row">
              <button className="btn btn-primary" type="submit" disabled={savingShop || !shopName.trim()}>
                {savingShop ? "Saving…" : "Save"}
              </button>
              {detailsStatus ? <span className="tiny muted">{detailsStatus}</span> : null}
            </div>
          </form>
        </section>

        <section className="section">
          <h2>TV</h2>
          {editingSlug ? (
            <div className="stack">
              <label className="field">
                <span>TV slug</span>
                <input
                  className="input"
                  value={slugDraft}
                  onChange={(event) => {
                    setSlugDraft(event.target.value);
                    setSlugError("");
                  }}
                  placeholder="e.g. retro-fashion"
                  disabled={savingSlug}
                  autoFocus
                />
              </label>
              {slugError ? <p className="error-text">{slugError}</p> : null}
              <div className="row">
                <button className="btn" type="button" onClick={() => void handleSaveSlug()} disabled={savingSlug || !slugDraft.trim()}>
                  {savingSlug ? "Saving…" : "Save slug"}
                </button>
                <button className="btn" type="button" onClick={() => void handleSuggestSlug()} disabled={savingSlug || suggestingSlug}>
                  {suggestingSlug ? "Suggesting…" : "Suggest from name"}
                </button>
                <button className="btn btn-ghost" type="button" onClick={() => setEditingSlug(false)} disabled={savingSlug}>
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="stack">
              <CopyValue label="TV slug" value={shop.screen_slug} />
              <div>
                <button className="btn" type="button" onClick={startEditingSlug}>
                  {shop.screen_slug ? "Edit" : "Set slug"}
                </button>
              </div>
            </div>
          )}
          <p className="tiny muted">Enter this on the TV app's setup screen.</p>
          {slugStatus ? <p className="tiny muted">{slugStatus}</p> : null}
        </section>

        <section className="section">
          <h2>Login</h2>
          <div className="stack">
            <CopyValue label="Shop ID" value={shopId} />
            <CopyValue label="Owner ID" value={shop.owner_auth_user_id} />
          </div>
          <form className="stack" onSubmit={handlePasswordReset}>
            <label className="field">
              <span>Reset shared password</span>
              <input
                className="input"
                type="text"
                placeholder="New password (at least 6 characters)"
                value={newPassword}
                onChange={(event) => setNewPassword(event.target.value)}
              />
            </label>
            <div className="row">
              <button className="btn" type="submit" disabled={resettingPassword || newPassword.trim().length < 6}>
                {resettingPassword ? "Updating…" : "Reset password"}
              </button>
              {loginStatus ? <span className="tiny muted">{loginStatus}</span> : null}
            </div>
          </form>
        </section>

        <section className="section section-danger">
          <h2>Danger zone</h2>
          <p className="tiny muted">Suspend blocks the shop's logins and keeps its data. Delete removes everything permanently.</p>
          <div className="row">
            <button className="btn" type="button" onClick={() => void handleToggleSuspend()} disabled={processingSuspend || deletingShop}>
              {processingSuspend ? "Updating…" : shop.is_suspended ? "Activate shop" : "Suspend shop"}
            </button>
            <button className="btn btn-danger" type="button" onClick={() => void handleDeleteShop()} disabled={deletingShop || processingSuspend}>
              {deletingShop ? "Deleting…" : "Delete shop"}
            </button>
          </div>
          {dangerStatus ? <p className="tiny muted">{dangerStatus}</p> : null}
        </section>
      </div>
    </div>
  );
}

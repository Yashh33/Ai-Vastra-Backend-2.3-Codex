import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { adminFetch } from "../lib/api";
import { useAdminAuth } from "../lib/auth";
import type { AdminFolderRow, AdminHeroImageRow, AdminShopRow } from "../lib/types";

export function AdminShopDetailPage() {
  const { shopId = "" } = useParams();
  const navigate = useNavigate();
  const { session } = useAdminAuth();

  const [shop, setShop] = useState<AdminShopRow | null>(null);
  const [folders, setFolders] = useState<AdminFolderRow[]>([]);
  const [heroImages, setHeroImages] = useState<AdminHeroImageRow[]>([]);

  const [loading, setLoading] = useState(false);
  const [statusText, setStatusText] = useState("Loading shop...");

  const [shopName, setShopName] = useState("");
  const [headerDisplayText, setHeaderDisplayText] = useState("");
  const [carouselModeDefault, setCarouselModeDefault] = useState(false);
  const [savingShop, setSavingShop] = useState(false);

  const [newPassword, setNewPassword] = useState("");
  const [resettingPassword, setResettingPassword] = useState(false);

  const [logoFile, setLogoFile] = useState<File | null>(null);
  const [uploadingLogo, setUploadingLogo] = useState(false);

  const [newFolderName, setNewFolderName] = useState("");
  const [newPromptTemplate, setNewPromptTemplate] = useState("");
  const [creatingFolder, setCreatingFolder] = useState(false);

  const [selectedFolderId, setSelectedFolderId] = useState("");
  const [heroFile, setHeroFile] = useState<File | null>(null);
  const [uploadingHero, setUploadingHero] = useState(false);

  const [processingSuspend, setProcessingSuspend] = useState(false);
  const [deletingShop, setDeletingShop] = useState(false);

  const canUploadHero = useMemo(() => !!selectedFolderId && !!heroFile, [selectedFolderId, heroFile]);

  async function loadShopData() {
    if (!session || !shopId) return;

    setLoading(true);
    try {
      const [shopRow, folderRows] = await Promise.all([
        adminFetch<AdminShopRow>(session, `/admin/shops/${encodeURIComponent(shopId)}`, { method: "GET" }),
        adminFetch<AdminFolderRow[]>(session, `/admin/shops/${encodeURIComponent(shopId)}/folders`, {
          method: "GET",
        }),
      ]);

      setShop(shopRow);
      setShopName(shopRow.name || "");
      setHeaderDisplayText(shopRow.header_display_text || "");
      setCarouselModeDefault(Boolean(shopRow.carousel_mode_default));
      setFolders(folderRows);

      if (folderRows.length && !selectedFolderId) {
        setSelectedFolderId(folderRows[0].id);
      }

      setStatusText("Shop loaded.");
    } catch (err) {
      setStatusText(`Load failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setLoading(false);
    }
  }

  async function loadHeroImages(folderId: string) {
    if (!session || !shopId || !folderId) {
      setHeroImages([]);
      return;
    }

    try {
      const rows = await adminFetch<AdminHeroImageRow[]>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/hero-images?folder_id=${encodeURIComponent(folderId)}&limit=30`,
        { method: "GET" }
      );
      setHeroImages(rows);
    } catch {
      setHeroImages([]);
    }
  }

  useEffect(() => {
    void loadShopData();
  }, [session, shopId]);

  useEffect(() => {
    if (!selectedFolderId) {
      setHeroImages([]);
      return;
    }
    void loadHeroImages(selectedFolderId);
  }, [selectedFolderId, session, shopId]);

  async function handleShopUpdate(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !shopId) return;

    setSavingShop(true);
    try {
      const updated = await adminFetch<AdminShopRow>(session, `/admin/shops/${encodeURIComponent(shopId)}`, {
        method: "PATCH",
        body: JSON.stringify({
          name: shopName.trim(),
          header_display_text: headerDisplayText.trim() || null,
          carousel_mode_default: carouselModeDefault,
        }),
      });
      setShop((prev) => ({ ...prev, ...updated }));
      setStatusText("Shop details updated.");
    } catch (err) {
      setStatusText(`Update failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setSavingShop(false);
    }
  }

  async function handlePasswordReset(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !shopId || newPassword.trim().length < 6) return;

    setResettingPassword(true);
    try {
      await adminFetch<{ password_reset: boolean }>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/reset-password`,
        {
          method: "POST",
          body: JSON.stringify({ password: newPassword.trim() }),
        }
      );
      setNewPassword("");
      setStatusText("Password reset successful.");
    } catch (err) {
      setStatusText(`Password reset failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setResettingPassword(false);
    }
  }

  async function handleLogoUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !shopId || !logoFile) return;

    setUploadingLogo(true);
    try {
      const formData = new FormData();
      formData.append("file", logoFile);

      const result = await adminFetch<{ logo_path: string }>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/logo`,
        {
          method: "POST",
          body: formData,
        }
      );

      setShop((prev) => (prev ? { ...prev, logo_path: result.logo_path } : prev));
      setLogoFile(null);
      setStatusText("Logo uploaded successfully.");
    } catch (err) {
      setStatusText(`Logo upload failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setUploadingLogo(false);
    }
  }

  async function handleCreateFolder(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !shopId || !newFolderName.trim()) return;

    setCreatingFolder(true);
    try {
      const created = await adminFetch<AdminFolderRow>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/folders`,
        {
          method: "POST",
          body: JSON.stringify({
            name: newFolderName.trim(),
            prompt_template: newPromptTemplate,
          }),
        }
      );

      setNewFolderName("");
      setNewPromptTemplate("");
      setFolders((prev) => [created, ...prev]);
      if (!selectedFolderId) {
        setSelectedFolderId(created.id);
      }
      setStatusText("Folder created.");
    } catch (err) {
      setStatusText(`Create folder failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setCreatingFolder(false);
    }
  }

  async function handleHeroUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !shopId || !heroFile || !selectedFolderId) return;

    setUploadingHero(true);
    try {
      const formData = new FormData();
      formData.append("folder_id", selectedFolderId);
      formData.append("file", heroFile);

      const created = await adminFetch<AdminHeroImageRow>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/hero-images/upload`,
        {
          method: "POST",
          body: formData,
        }
      );

      setHeroFile(null);
      setHeroImages((prev) => [created, ...prev]);
      setStatusText("Hero image uploaded.");
    } catch (err) {
      setStatusText(`Hero upload failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setUploadingHero(false);
    }
  }

  async function handleToggleSuspend() {
    if (!session || !shopId || !shop) return;

    const nextSuspended = !Boolean(shop.is_suspended);
    const label = nextSuspended ? "suspend" : "activate";
    const confirmed = window.confirm(`Are you sure you want to ${label} this shop?`);
    if (!confirmed) return;

    setProcessingSuspend(true);
    try {
      const result = await adminFetch<{ shop_id: string; is_suspended: boolean }>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/suspend`,
        {
          method: "POST",
          body: JSON.stringify({ suspended: nextSuspended }),
        }
      );

      setShop((prev) => (prev ? { ...prev, is_suspended: result.is_suspended } : prev));
      setStatusText(result.is_suspended ? "Shop suspended." : "Shop activated.");
    } catch (err) {
      setStatusText(`Suspend/activate failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setProcessingSuspend(false);
    }
  }

  async function handleDeleteShop() {
    if (!session || !shopId || !shop) return;

    const firstConfirm = window.confirm(
      "Delete this shop permanently? This will remove all folders, hero images, generations, and credits."
    );
    if (!firstConfirm) return;

    const secondConfirm = window.confirm("Final confirmation: this cannot be undone. Continue?");
    if (!secondConfirm) return;

    setDeletingShop(true);
    try {
      const result = await adminFetch<{
        deleted: boolean;
        shop_name: string;
        warnings?: string[];
      }>(session, `/admin/shops/${encodeURIComponent(shopId)}`, {
        method: "DELETE",
      });

      if (result.warnings?.length) {
        setStatusText(`Shop deleted with warnings: ${result.warnings.join(" | ")}`);
      } else {
        setStatusText(`Shop deleted: ${result.shop_name}`);
      }

      navigate("/", { replace: true });
    } catch (err) {
      setStatusText(`Delete failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setDeletingShop(false);
    }
  }

  return (
    <main className="screen">
      <section className="page-shell">
        <header className="header-row">
          <div>
            <h1>Shop Setup</h1>
            <p className="muted">{shop?.name || "-"}</p>
            <p className="tiny muted">{statusText}</p>
          </div>
          <div className="row">
            <Link to="/" className="btn btn-light">
              Back to Dashboard
            </Link>
            <button className="btn btn-light" onClick={loadShopData} disabled={loading}>
              {loading ? "Refreshing..." : "Refresh"}
            </button>
          </div>
        </header>

        <section className="card stack">
          <h2>Shop Details</h2>
          <p className="tiny muted">Shop ID: {shopId}</p>
          <p className="tiny muted">Credits: {shop?.credits_balance ?? 0}</p>
          <p className="tiny muted">Owner UID: {shop?.owner_auth_user_id || "-"}</p>
          <p className="tiny muted">Access: {shop?.is_suspended ? "Suspended" : "Active"}</p>

          <form className="stack" onSubmit={handleShopUpdate}>
            <div className="grid-2">
              <label className="field">
                <span>Shop Name</span>
                <input value={shopName} onChange={(event) => setShopName(event.target.value)} />
              </label>

              <label className="field">
                <span>Header Text</span>
                <input
                  value={headerDisplayText}
                  onChange={(event) => setHeaderDisplayText(event.target.value)}
                  placeholder="e.g. Retero Fashion"
                />
              </label>

              <label className="field">
                <span>Carousel Mode Default</span>
                <select
                  value={carouselModeDefault ? "true" : "false"}
                  onChange={(event) => setCarouselModeDefault(event.target.value === "true")}
                >
                  <option value="false">Off</option>
                  <option value="true">On</option>
                </select>
              </label>
            </div>

            <button className="btn btn-dark" type="submit" disabled={savingShop}>
              {savingShop ? "Saving..." : "Save Shop Details"}
            </button>
          </form>
        </section>

        <section className="card stack">
          <h2>Access Control</h2>
          <p className="tiny muted">Suspend disables user access while keeping data. Delete removes everything permanently.</p>
          <div className="row">
            <button className="btn btn-light" onClick={handleToggleSuspend} disabled={processingSuspend || deletingShop}>
              {processingSuspend
                ? "Updating..."
                : shop?.is_suspended
                  ? "Activate Shop"
                  : "Suspend Shop"}
            </button>
            <button className="btn btn-danger" onClick={handleDeleteShop} disabled={deletingShop || processingSuspend}>
              {deletingShop ? "Deleting..." : "Delete Shop"}
            </button>
          </div>
        </section>

        <section className="card stack">
          <h2>Reset Shared Password</h2>
          <form className="row" onSubmit={handlePasswordReset}>
            <input
              className="grow"
              type="text"
              placeholder="New password"
              value={newPassword}
              onChange={(event) => setNewPassword(event.target.value)}
            />
            <button className="btn btn-dark" type="submit" disabled={resettingPassword || newPassword.length < 6}>
              {resettingPassword ? "Updating..." : "Reset Password"}
            </button>
          </form>
        </section>

        <section className="card stack">
          <h2>Upload Logo</h2>
          <p className="tiny muted">Current logo path: {shop?.logo_path || "(none)"}</p>
          <form className="row" onSubmit={handleLogoUpload}>
            <input type="file" accept="image/*" onChange={(event) => setLogoFile(event.target.files?.[0] || null)} />
            <button className="btn btn-dark" type="submit" disabled={uploadingLogo || !logoFile}>
              {uploadingLogo ? "Uploading..." : "Upload Logo"}
            </button>
          </form>
        </section>

        <section className="card stack">
          <h2>Create Folder</h2>
          <form className="stack" onSubmit={handleCreateFolder}>
            <label className="field">
              <span>Folder Name</span>
              <input
                value={newFolderName}
                onChange={(event) => setNewFolderName(event.target.value)}
                placeholder="e.g. Shirt, Suit"
                disabled={creatingFolder}
              />
            </label>
            <label className="field">
              <span>Prompt Template (optional)</span>
              <textarea
                rows={3}
                value={newPromptTemplate}
                onChange={(event) => setNewPromptTemplate(event.target.value)}
                disabled={creatingFolder}
              />
            </label>
            <button className="btn btn-dark" type="submit" disabled={creatingFolder || !newFolderName.trim()}>
              {creatingFolder ? "Creating..." : "Create Folder"}
            </button>
          </form>
        </section>

        <section className="card stack">
          <h2>Upload Hero Image</h2>
          <div className="grid-2">
            <label className="field">
              <span>Select Folder</span>
              <select
                value={selectedFolderId}
                onChange={(event) => setSelectedFolderId(event.target.value)}
              >
                <option value="">Choose folder</option>
                {folders.map((folder) => (
                  <option key={folder.id} value={folder.id}>
                    {folder.name}
                  </option>
                ))}
              </select>
            </label>

            <label className="field">
              <span>Image File</span>
              <input
                type="file"
                accept="image/*"
                onChange={(event) => setHeroFile(event.target.files?.[0] || null)}
              />
            </label>
          </div>

          <form onSubmit={handleHeroUpload}>
            <button className="btn btn-dark" type="submit" disabled={uploadingHero || !canUploadHero}>
              {uploadingHero ? "Uploading..." : "Upload Hero Image"}
            </button>
          </form>

          {selectedFolderId ? (
            <div className="stack">
              <p className="tiny muted">Recent hero images in selected folder:</p>
              {heroImages.length === 0 ? (
                <div className="empty-box">No hero images found.</div>
              ) : (
                <ul className="hero-list">
                  {heroImages.map((row) => (
                    <li key={row.id}>
                      <span>{row.original_filename || row.id}</span>
                      <code>{row.storage_path}</code>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : null}
        </section>
      </section>
    </main>
  );
}

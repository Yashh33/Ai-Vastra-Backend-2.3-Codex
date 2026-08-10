import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { adminFetch } from "../lib/api";
import { useAdminAuth } from "../lib/auth";
import type {
  AdminCatalogImageRow,
  AdminFabricSlotRow,
  AdminFolderRow,
  AdminHeroImageRow,
  AdminSetDefaultHeroRequest,
  AdminShopRow,
} from "../lib/types";

export function AdminShopDetailPage() {
  const { shopId = "" } = useParams();
  const navigate = useNavigate();
  const { session } = useAdminAuth();

  const [shop, setShop] = useState<AdminShopRow | null>(null);
  const [folders, setFolders] = useState<AdminFolderRow[]>([]);
  const [heroImages, setHeroImages] = useState<AdminHeroImageRow[]>([]);
  const [heroSignedUrls, setHeroSignedUrls] = useState<Record<string, string>>({});
  const [catalogImages, setCatalogImages] = useState<AdminCatalogImageRow[]>([]);

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
  const [catalogFiles, setCatalogFiles] = useState<File[]>([]);
  const [uploadingCatalog, setUploadingCatalog] = useState(false);

  const [fabricSlots, setFabricSlots] = useState<AdminFabricSlotRow[]>([]);
  const [newSlotLabel, setNewSlotLabel] = useState("");
  const [newSlotApplyTo, setNewSlotApplyTo] = useState("");
  const [newSlotSortOrder, setNewSlotSortOrder] = useState(0);
  const [savingSlot, setSavingSlot] = useState(false);
  const [deletingSlotId, setDeletingSlotId] = useState<string | null>(null);

  const [processingSuspend, setProcessingSuspend] = useState(false);
  const [deletingShop, setDeletingShop] = useState(false);

  const [activeTab, setActiveTab] = useState<"overview" | "garments">("overview");
  const [showCreateModal, setShowCreateModal] = useState(false);

  const canUploadHero = useMemo(() => !!selectedFolderId && !!heroFile, [selectedFolderId, heroFile]);
  const canUploadCatalog = useMemo(
    () => !!selectedFolderId && catalogFiles.length > 0,
    [selectedFolderId, catalogFiles]
  );
  const selectedFolder = useMemo(
    () => folders.find((folder) => folder.id === selectedFolderId) || null,
    [folders, selectedFolderId]
  );

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
      setHeroSignedUrls({});
      return;
    }

    try {
      const rows = await adminFetch<AdminHeroImageRow[]>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/hero-images?folder_id=${encodeURIComponent(folderId)}&limit=30`,
        { method: "GET" }
      );
      setHeroImages(rows);
      await loadHeroSignedUrls(rows);
    } catch {
      setHeroImages([]);
      setHeroSignedUrls({});
    }
  }

  async function loadHeroSignedUrls(images: AdminHeroImageRow[]) {
    const urls: Record<string, string> = {};
    for (const img of images) {
      if (img.signed_url) {
        urls[img.id] = img.signed_url;
      }
    }
    setHeroSignedUrls(urls);
  }

  async function loadCatalogImages(folderId: string) {
    if (!session || !shopId || !folderId) {
      setCatalogImages([]);
      return;
    }

    try {
      const rows = await adminFetch<AdminCatalogImageRow[]>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/catalog-images?folder_id=${encodeURIComponent(folderId)}&limit=30`,
        { method: "GET" }
      );
      setCatalogImages(rows);
    } catch {
      setCatalogImages([]);
    }
  }

  async function loadFabricSlots(folderId: string) {
    if (!session || !shopId || !folderId) {
      setFabricSlots([]);
      return;
    }

    try {
      const rows = await adminFetch<AdminFabricSlotRow[]>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/folders/${encodeURIComponent(folderId)}/fabric-slots`,
        { method: "GET" }
      );
      setFabricSlots(rows);
    } catch {
      setFabricSlots([]);
    }
  }

  useEffect(() => {
    void loadShopData();
  }, [session, shopId]);

  useEffect(() => {
    if (!selectedFolderId) {
      setHeroImages([]);
      setCatalogImages([]);
      setFabricSlots([]);
      return;
    }
    void Promise.all([
      loadHeroImages(selectedFolderId),
      loadCatalogImages(selectedFolderId),
      loadFabricSlots(selectedFolderId),
    ]);
  }, [selectedFolderId, session, shopId]);

  useEffect(() => {
    if (!selectedFolderId && folders.length > 0) {
      setSelectedFolderId(folders[0].id);
    }
  }, [folders]);

  useEffect(() => {
    if (showCreateModal) {
      setShowCreateModal(false);
    }
  }, [folders]);

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
      setStatusText("Garment type created.");
    } catch (err) {
      setStatusText(`Create garment type failed: ${err instanceof Error ? err.message : "Unknown error"}`);
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

  async function handleCatalogBulkUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !shopId || !selectedFolderId || !catalogFiles.length) return;

    setUploadingCatalog(true);
    try {
      const formData = new FormData();
      formData.append("folder_id", selectedFolderId);
      for (const file of catalogFiles) {
        formData.append("files", file);
      }

      const result = await adminFetch<{
        uploaded_count: number;
        items: AdminCatalogImageRow[];
      }>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/catalog-images/upload-bulk`,
        {
          method: "POST",
          body: formData,
        }
      );

      setCatalogFiles([]);
      setCatalogImages((prev) => [...result.items, ...prev]);
      setStatusText(`Catalog upload completed. ${result.uploaded_count} image(s) added.`);
    } catch (err) {
      setStatusText(`Catalog upload failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setUploadingCatalog(false);
    }
  }

  async function handleCreateFabricSlot(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session || !shopId || !selectedFolderId || !newSlotLabel.trim()) return;

    if (!newSlotApplyTo.trim()) {
      setStatusText("Apply To is required");
      return;
    }

    setSavingSlot(true);
    try {
      await adminFetch<AdminFabricSlotRow>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/folders/${encodeURIComponent(selectedFolderId)}/fabric-slots`,
        {
          method: "POST",
          body: JSON.stringify({
            label: newSlotLabel.trim(),
            apply_to: newSlotApplyTo,
            sort_order: newSlotSortOrder,
          }),
        }
      );

      setNewSlotLabel("");
      setNewSlotApplyTo("");
      setNewSlotSortOrder(0);
      await loadFabricSlots(selectedFolderId);
      setStatusText("Fabric slot added.");
    } catch (err) {
      setStatusText(`Failed to add fabric slot: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setSavingSlot(false);
    }
  }

  async function handleDeleteFabricSlot(slotId: string, label: string) {
    if (!session || !shopId || !selectedFolderId) return;
    const confirmed = window.confirm(`Delete fabric slot "${label}"? This cannot be undone.`);
    if (!confirmed) return;

    setDeletingSlotId(slotId);
    try {
      await adminFetch(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/folders/${encodeURIComponent(selectedFolderId)}/fabric-slots/${encodeURIComponent(slotId)}`,
        { method: "DELETE" }
      );
      setStatusText(`Deleted fabric slot "${label}".`);
      await loadFabricSlots(selectedFolderId);
    } catch (err) {
      setStatusText(`Failed to delete fabric slot: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setDeletingSlotId(null);
    }
  }

  async function handleSetDefaultHero(folderId: string, heroImageId: string) {
    if (!session) return;
    try {
      const payload: AdminSetDefaultHeroRequest = {
        default_hero_image_id: heroImageId,
      };
      await adminFetch(
        session,
        `/admin/shops/${shopId}/folders/${folderId}/default-hero`,
        {
          method: "PATCH",
          body: JSON.stringify(payload),
        }
      );
      // Refresh folders to show updated default
      await loadShopData();
      setStatusText("Default hero image updated.");
    } catch (err) {
      setStatusText(`Failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    }
  }

  async function handleDeleteFolder(folderId: string, folderName: string) {
    if (!session) return;
    const confirmed = window.confirm(
      `Delete garment type "${folderName}"? This cannot be undone.`
    );
    if (!confirmed) return;
    try {
      await adminFetch(
        session,
        `/admin/shops/${shopId}/folders/${folderId}`,
        { method: "DELETE" }
      );
      setStatusText(`Deleted "${folderName}".`);
      await loadShopData();
    } catch (err) {
      setStatusText(
        `Failed to delete: ${err instanceof Error ? err.message : "Unknown error"}`
      );
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

        <div className="row">
          <button
            className={activeTab === "overview" ? "tab tab-active" : "tab"}
            onClick={() => setActiveTab("overview")}
          >
            Overview
          </button>
          <button
            className={activeTab === "garments" ? "tab tab-active" : "tab"}
            onClick={() => setActiveTab("garments")}
          >
            Garment Types
          </button>
        </div>

        {activeTab === "overview" && (
        <>
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
        </>
        )}

        {activeTab === "garments" && (
        <div className="gt-layout">
          <div className="card stack gt-list-panel">
            <div className="gt-list-header">
              <h2>Garment Types</h2>
              <button
                type="button"
                className="gt-add-btn"
                onClick={() => setShowCreateModal(true)}
                aria-label="Create garment type"
              >
                +
              </button>
            </div>

            {folders.length === 0 ? (
              <p className="tiny muted">No garment types yet. Click + to create one.</p>
            ) : (
              <ul className="gt-list">
                {folders.map((folder) => (
                  <li
                    key={folder.id}
                    className={
                      folder.id === selectedFolderId
                        ? "gt-list-item gt-list-item-active"
                        : "gt-list-item"
                    }
                    onClick={() => setSelectedFolderId(folder.id)}
                  >
                    <span>{folder.name}</span>
                    {folder.default_hero_image_id ? (
                      <span className="gt-default-badge">Default set ✓</span>
                    ) : null}
                  </li>
                ))}
              </ul>
            )}
          </div>

          <div className="card stack gt-detail-panel">
            {!selectedFolder ? (
              <p className="tiny muted">Select a garment type on the left, or create one with +.</p>
            ) : (
              <>
                <div className="gt-detail-header">
                  <h2>{selectedFolder.name}</h2>
                  <button
                    className="btn btn-danger"
                    type="button"
                    onClick={() => handleDeleteFolder(selectedFolder.id, selectedFolder.name)}
                  >
                    Delete garment type
                  </button>
                </div>

                <div className="stack">
                  <h3>Hero Images</h3>
                  <div className="grid-2">
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

                  <div className="stack">
                    <p className="tiny muted">Recent hero images in selected garment type:</p>
                    {heroImages.length === 0 ? (
                      <div className="empty-box">No hero images found.</div>
                    ) : (
                      <ul className="hero-list">
                        {heroImages.map((row) => (
                          <li key={row.id}>
                            <div className="row">
                              {heroSignedUrls[row.id] ? (
                                <img
                                  src={heroSignedUrls[row.id]}
                                  alt={row.original_filename || "Hero image"}
                                  className="gt-hero-thumb"
                                />
                              ) : (
                                <div className="empty-box gt-hero-thumb-placeholder">Preview</div>
                              )}
                              <div className="stack grow">
                                <span>{row.original_filename || row.id}</span>
                                <code>{row.storage_path}</code>
                              </div>
                              {row.id === selectedFolder?.default_hero_image_id ? (
                                <span style={{ color: "#15803d", fontWeight: 700 }}>✓ Default</span>
                              ) : (
                                <button
                                  className="btn btn-light"
                                  type="button"
                                  onClick={() => void handleSetDefaultHero(selectedFolderId, row.id)}
                                >
                                  Set as Default
                                </button>
                              )}
                            </div>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>

                <div className="stack">
                  <h3>Fabric slots</h3>

                  {fabricSlots.length === 0 ? (
                    <div className="empty-box">No fabric slots found.</div>
                  ) : (
                    <ul className="hero-list">
                      {fabricSlots.map((slot) => (
                        <li
                          key={slot.id}
                          style={{
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "space-between",
                            gap: "8px",
                            padding: "4px 0",
                          }}
                        >
                          <span>
                            {slot.sort_order}. {slot.label}{" "}
                            <span
                              style={{
                                background: "#e5e7eb",
                                color: "#374151",
                                borderRadius: "6px",
                                padding: "2px 8px",
                                fontSize: "12px",
                                fontWeight: 600,
                              }}
                            >
                              {slot.apply_to}
                            </span>
                          </span>
                          <button
                            onClick={() => void handleDeleteFabricSlot(slot.id, slot.label)}
                            disabled={deletingSlotId === slot.id}
                            style={{
                              background: "#991B1B",
                              color: "white",
                              border: "none",
                              borderRadius: "6px",
                              padding: "3px 10px",
                              fontSize: "12px",
                              fontWeight: 600,
                              cursor: "pointer",
                              flexShrink: 0,
                            }}
                          >
                            {deletingSlotId === slot.id ? "Deleting..." : "Delete"}
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}

                  <form className="stack" onSubmit={handleCreateFabricSlot}>
                    <div className="grid-2">
                      <label className="field">
                        <span>Label</span>
                        <input
                          value={newSlotLabel}
                          onChange={(event) => setNewSlotLabel(event.target.value)}
                          placeholder="e.g. Shirt fabric"
                          disabled={savingSlot}
                        />
                      </label>

                      <label className="field">
                        <span>Apply To</span>
                        <input
                          value={newSlotApplyTo}
                          onChange={(event) => setNewSlotApplyTo(event.target.value)}
                          placeholder="e.g. shirt, pant, outer koti, kurta"
                          disabled={savingSlot}
                        />
                      </label>

                      <label className="field">
                        <span>Sort Order</span>
                        <input
                          type="number"
                          min={0}
                          value={newSlotSortOrder}
                          onChange={(event) => setNewSlotSortOrder(Number(event.target.value))}
                          disabled={savingSlot}
                        />
                      </label>
                    </div>

                    <button className="btn btn-dark" type="submit" disabled={savingSlot || !newSlotLabel.trim()}>
                      {savingSlot ? "Adding..." : "Add slot"}
                    </button>
                  </form>
                </div>

                <div className="stack">
                  <h3>Upload Catalog Images (Bulk)</h3>
                  <p className="tiny muted">These images appear in customer Catalog under the selected garment type.</p>

                  <label className="field">
                    <span>Select Images (multiple)</span>
                    <input
                      type="file"
                      accept="image/*"
                      multiple
                      onChange={(event) => {
                        const nextFiles = Array.from(event.target.files ?? []);
                        setCatalogFiles(nextFiles);
                      }}
                    />
                  </label>

                  <form onSubmit={handleCatalogBulkUpload}>
                    <button className="btn btn-dark" type="submit" disabled={uploadingCatalog || !canUploadCatalog}>
                      {uploadingCatalog ? "Uploading..." : "Upload Catalog Images"}
                    </button>
                  </form>

                  <div className="stack">
                    <p className="tiny muted">Recent catalog images in selected garment type:</p>
                    {catalogImages.length === 0 ? (
                      <div className="empty-box">No catalog images found.</div>
                    ) : (
                      <ul className="hero-list">
                        {catalogImages.map((row) => (
                          <li key={row.id}>
                            <span>{row.original_filename || row.id}</span>
                            <code>{row.storage_path}</code>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
        )}

        {showCreateModal && (
          <div className="modal-overlay" onClick={() => setShowCreateModal(false)}>
            <div className="card stack modal-card" onClick={(event) => event.stopPropagation()}>
              <h2>Create Garment Type</h2>
              <form className="stack" onSubmit={handleCreateFolder}>
                <label className="field">
                  <span>Garment Type Name</span>
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
                <div className="row">
                  <button className="btn btn-dark" type="submit" disabled={creatingFolder || !newFolderName.trim()}>
                    {creatingFolder ? "Creating..." : "Create"}
                  </button>
                  <button className="btn btn-light" type="button" onClick={() => setShowCreateModal(false)}>
                    Cancel
                  </button>
                </div>
              </form>
            </div>
          </div>
        )}
      </section>
    </main>
  );
}

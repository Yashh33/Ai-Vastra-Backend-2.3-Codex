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
  GenerationInspect,
  PromptVersion,
} from "../lib/types";

export function AdminShopDetailPage() {
  const { shopId = "" } = useParams();
  const navigate = useNavigate();
  const { session } = useAdminAuth();

  const [shop, setShop] = useState<AdminShopRow | null>(null);
  const [folders, setFolders] = useState<AdminFolderRow[]>([]);
  const [showArchived, setShowArchived] = useState(false);
  const [heroImages, setHeroImages] = useState<AdminHeroImageRow[]>([]);
  const [heroSignedUrls, setHeroSignedUrls] = useState<Record<string, string>>({});
  const [catalogImages, setCatalogImages] = useState<AdminCatalogImageRow[]>([]);

  const [loading, setLoading] = useState(false);
  const [statusText, setStatusText] = useState("Loading shop...");

  const [shopName, setShopName] = useState("");
  const [headerDisplayText, setHeaderDisplayText] = useState("");
  const [carouselModeDefault, setCarouselModeDefault] = useState(false);
  const [savingShop, setSavingShop] = useState(false);

  const [creditDelta, setCreditDelta] = useState("");
  const [creditReason, setCreditReason] = useState("");
  const [grantingCredits, setGrantingCredits] = useState(false);

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
  const [togglingWhatsappMenu, setTogglingWhatsappMenu] = useState(false);

  const [editCategory, setEditCategory] = useState("unisex");
  const [editLookPrompt, setEditLookPrompt] = useState("");
  const [editTryonPrompt, setEditTryonPrompt] = useState("");
  const [editPromptNote, setEditPromptNote] = useState("");
  const [savingPrompt, setSavingPrompt] = useState(false);

  const [showPromptHistory, setShowPromptHistory] = useState(false);
  const [promptVersions, setPromptVersions] = useState<PromptVersion[]>([]);
  const [loadingPromptVersions, setLoadingPromptVersions] = useState(false);
  const [revertingVersionId, setRevertingVersionId] = useState<string | null>(null);

  const [processingSuspend, setProcessingSuspend] = useState(false);
  const [deletingShop, setDeletingShop] = useState(false);
  const [togglingMultifabric, setTogglingMultifabric] = useState(false);

  const [activeTab, setActiveTab] = useState<"overview" | "garments" | "generations">("overview");
  const [showCreateModal, setShowCreateModal] = useState(false);

  const [generations, setGenerations] = useState<GenerationInspect[]>([]);
  const [loadingGenerations, setLoadingGenerations] = useState(false);
  const [generationsFilterFolderId, setGenerationsFilterFolderId] = useState("");
  const [expandedGenerationId, setExpandedGenerationId] = useState<string | null>(null);

  const canUploadHero = useMemo(() => !!selectedFolderId && !!heroFile, [selectedFolderId, heroFile]);
  const canUploadCatalog = useMemo(
    () => !!selectedFolderId && catalogFiles.length > 0,
    [selectedFolderId, catalogFiles]
  );
  const selectedFolder = useMemo(
    () => folders.find((folder) => folder.id === selectedFolderId) || null,
    [folders, selectedFolderId]
  );

  const imageOrderLines = useMemo(() => {
    const sortedSlots = [...fabricSlots].sort((a, b) => a.sort_order - b.sort_order);
    const lines = ["Image 1 → Hero (garment reference)"];
    sortedSlots.forEach((slot, index) => {
      lines.push(`Image ${index + 2} → ${slot.label} (slot)`);
    });
    lines.push(`Image ${sortedSlots.length + 2} → Person (only on TRY-ON)`);
    return lines;
  }, [fabricSlots]);

  async function loadShopData() {
    if (!session || !shopId) return;

    setLoading(true);
    try {
      const [shopRow, folderRows] = await Promise.all([
        adminFetch<AdminShopRow>(session, `/admin/shops/${encodeURIComponent(shopId)}`, { method: "GET" }),
        adminFetch<AdminFolderRow[]>(
          session,
          `/admin/shops/${encodeURIComponent(shopId)}/folders?include_archived=${showArchived}`,
          { method: "GET" }
        ),
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

  async function loadGenerations() {
    if (!session || !shopId) return;

    setLoadingGenerations(true);
    try {
      const query = generationsFilterFolderId
        ? `&garment_type_id=${encodeURIComponent(generationsFilterFolderId)}`
        : "";
      const rows = await adminFetch<GenerationInspect[]>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/generations?limit=30${query}`,
        { method: "GET" }
      );
      setGenerations(rows);
    } catch (err) {
      setGenerations([]);
      setStatusText(`Failed to load generations: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setLoadingGenerations(false);
    }
  }

  useEffect(() => {
    void loadShopData();
  }, [session, shopId, showArchived]);

  useEffect(() => {
    if (activeTab === "generations") {
      void loadGenerations();
    }
  }, [activeTab, generationsFilterFolderId, session, shopId]);

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
    const folder = folders.find((item) => item.id === selectedFolderId) || null;
    setEditCategory(folder?.category || "unisex");
    setEditLookPrompt(folder?.look_prompt || "");
    setEditTryonPrompt(folder?.tryon_prompt || "");
    setEditPromptNote("");
    setShowPromptHistory(false);
    setPromptVersions([]);
  }, [selectedFolderId]);

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

  async function handleGrantCredits() {
    if (!session || !shopId) return;

    const reason = creditReason.trim();
    const delta = Number(creditDelta);

    if (!reason || !delta || Number.isNaN(delta)) {
      setStatusText("Enter a non-zero amount and a reason to update credits.");
      return;
    }

    setGrantingCredits(true);
    try {
      const result = await adminFetch<{
        shop_id: string;
        delta: number;
        reason: string;
        balance_before: number;
        balance_after: number;
      }>(session, `/admin/shops/${encodeURIComponent(shopId)}/credits`, {
        method: "POST",
        body: JSON.stringify({ delta, reason }),
      });

      setShop((prev) => (prev ? { ...prev, credits_balance: result.balance_after } : prev));
      setCreditDelta("");
      setCreditReason("");
      setStatusText(`Credits updated. New balance: ${result.balance_after}`);
    } catch (err) {
      setStatusText(`Credits update failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setGrantingCredits(false);
    }
  }

  async function handleToggleMultifabric() {
    if (!session || !shopId || !shop) return;

    const nextEnabled = !Boolean(shop.whatsapp_multifabric_enabled);

    setTogglingMultifabric(true);
    try {
      const result = await adminFetch<{
        shop_id: string;
        whatsapp_multifabric_enabled: boolean;
      }>(session, `/admin/shops/${encodeURIComponent(shopId)}/whatsapp-multifabric`, {
        method: "POST",
        body: JSON.stringify({ enabled: nextEnabled }),
      });

      setShop((prev) =>
        prev ? { ...prev, whatsapp_multifabric_enabled: result.whatsapp_multifabric_enabled } : prev
      );
      setStatusText(
        result.whatsapp_multifabric_enabled
          ? "WhatsApp multi-fabric enabled."
          : "WhatsApp multi-fabric disabled."
      );
    } catch (err) {
      setStatusText(`WhatsApp multi-fabric update failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setTogglingMultifabric(false);
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

  async function handleArchiveFolder(folderId: string, folderName: string) {
    if (!session) return;
    const confirmed = window.confirm(
      `Archive garment type "${folderName}"? Types with no generation history are deleted outright; ` +
        `types with history are archived and can be restored later.`
    );
    if (!confirmed) return;
    try {
      const result = await adminFetch<{
        deleted: string | false;
        archived: boolean;
        generations?: number;
      }>(session, `/admin/shops/${shopId}/folders/${folderId}`, { method: "DELETE" });

      if (result.archived) {
        setStatusText(`Archived "${folderName}" (has ${result.generations ?? 0} generations in history).`);
      } else {
        setStatusText(`Deleted "${folderName}".`);
      }
      await loadShopData();
    } catch (err) {
      setStatusText(
        `Failed to archive: ${err instanceof Error ? err.message : "Unknown error"}`
      );
    }
  }

  async function handleRestoreFolder(folderId: string, folderName: string) {
    if (!session || !shopId) return;
    try {
      await adminFetch(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/folders/${encodeURIComponent(folderId)}/restore`,
        { method: "PATCH" }
      );
      setStatusText(`Restored "${folderName}".`);
      await loadShopData();
    } catch (err) {
      setStatusText(
        `Failed to restore: ${err instanceof Error ? err.message : "Unknown error"}`
      );
    }
  }

  async function handleSavePrompt(folderId: string) {
    if (!session || !shopId) return;

    if (!editLookPrompt.trim() || !editTryonPrompt.trim()) {
      setStatusText("Both look and try-on prompts are required.");
      return;
    }

    setSavingPrompt(true);
    try {
      const payload = {
        look_prompt: editLookPrompt,
        tryon_prompt: editTryonPrompt,
        category: editCategory,
        note: editPromptNote.trim() || undefined,
      };
      const updated = await adminFetch<{
        look_prompt: string;
        tryon_prompt: string;
        category: string;
      }>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/folders/${encodeURIComponent(folderId)}/prompt`,
        { method: "PATCH", body: JSON.stringify(payload) }
      );

      setFolders((prev) =>
        prev.map((folder) =>
          folder.id === folderId
            ? {
                ...folder,
                look_prompt: updated.look_prompt,
                tryon_prompt: updated.tryon_prompt,
                category: updated.category,
              }
            : folder
        )
      );
      setEditPromptNote("");
      setStatusText("Prompt saved (new version recorded).");
      if (showPromptHistory) {
        await loadPromptVersions(folderId);
      }
    } catch (err) {
      setStatusText(`Failed to save prompt: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setSavingPrompt(false);
    }
  }

  async function loadPromptVersions(folderId: string) {
    if (!session || !shopId || !folderId) {
      setPromptVersions([]);
      return;
    }

    setLoadingPromptVersions(true);
    try {
      const rows = await adminFetch<PromptVersion[]>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/folders/${encodeURIComponent(folderId)}/prompt-versions`,
        { method: "GET" }
      );
      setPromptVersions(rows);
    } catch (err) {
      setPromptVersions([]);
      setStatusText(`Failed to load prompt history: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setLoadingPromptVersions(false);
    }
  }

  function handleTogglePromptHistory() {
    const next = !showPromptHistory;
    setShowPromptHistory(next);
    if (next && selectedFolderId) {
      void loadPromptVersions(selectedFolderId);
    }
  }

  async function handleRevertPromptVersion(versionId: string) {
    if (!session || !shopId || !selectedFolderId) return;

    setRevertingVersionId(versionId);
    try {
      await adminFetch(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/folders/${encodeURIComponent(selectedFolderId)}/prompt-versions/${encodeURIComponent(versionId)}/revert`,
        { method: "POST" }
      );
      setStatusText("Reverted to selected prompt version.");
      await loadShopData();
      await loadPromptVersions(selectedFolderId);
    } catch (err) {
      setStatusText(`Failed to revert: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setRevertingVersionId(null);
    }
  }

  async function handleToggleWhatsappMenu(folderId: string, current: boolean) {
    if (!session || !shopId) return;

    setTogglingWhatsappMenu(true);
    try {
      const result = await adminFetch<{
        folder_id: string;
        show_in_whatsapp_menu: boolean;
      }>(session, `/admin/shops/${encodeURIComponent(shopId)}/folders/${encodeURIComponent(folderId)}/whatsapp-menu`, {
        method: "POST",
        body: JSON.stringify({ enabled: !current }),
      });

      setFolders((prev) =>
        prev.map((folder) =>
          folder.id === folderId
            ? { ...folder, show_in_whatsapp_menu: result.show_in_whatsapp_menu }
            : folder
        )
      );
      setStatusText(
        result.show_in_whatsapp_menu
          ? "Garment type added to WhatsApp menu."
          : "Garment type removed from WhatsApp menu."
      );
    } catch (err) {
      setStatusText(`WhatsApp menu update failed: ${err instanceof Error ? err.message : "Unknown error"}`);
    } finally {
      setTogglingWhatsappMenu(false);
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
          <button
            className={activeTab === "generations" ? "tab tab-active" : "tab"}
            onClick={() => setActiveTab("generations")}
          >
            Generations
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
          <h2>Credits</h2>
          <p className="tiny muted">Current balance: {shop?.credits_balance ?? 0} credits</p>

          <div className="grid-2">
            <label className="field">
              <span>Amount to Add</span>
              <input
                type="number"
                value={creditDelta}
                onChange={(event) => setCreditDelta(event.target.value)}
                placeholder="e.g. 500 or -100"
                disabled={grantingCredits}
              />
            </label>

            <label className="field">
              <span>Reason</span>
              <input
                value={creditReason}
                onChange={(event) => setCreditReason(event.target.value)}
                placeholder="e.g. Monthly grant - MaleHub"
                disabled={grantingCredits}
              />
            </label>
          </div>

          <p className="tiny muted">≈ {Math.round((Number(creditDelta) || 0) / 50)} looks</p>

          <button className="btn btn-dark" type="button" onClick={handleGrantCredits} disabled={grantingCredits}>
            {grantingCredits ? "Updating..." : "Update Credits"}
          </button>
        </section>

        <section className="card stack">
          <h2>WhatsApp Multi-Fabric</h2>
          <p className="tiny muted">
            Status: {shop?.whatsapp_multifabric_enabled ? "Enabled" : "Disabled"}
          </p>
          <button
            className="btn btn-light"
            type="button"
            onClick={handleToggleMultifabric}
            disabled={togglingMultifabric}
          >
            {togglingMultifabric
              ? "Updating..."
              : shop?.whatsapp_multifabric_enabled
                ? "Disable"
                : "Enable"}
          </button>
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

            <label className="row tiny" style={{ margin: "0.1rem 0 0.3rem" }}>
              <input
                type="checkbox"
                checked={showArchived}
                onChange={(event) => setShowArchived(event.target.checked)}
              />
              Show archived
            </label>

            {folders.length === 0 ? (
              <p className="tiny muted">No garment types yet. Click + to create one.</p>
            ) : (
              <ul className="gt-list">
                {folders.map((folder) => {
                  const archived = folder.is_active === false;
                  return (
                    <li
                      key={folder.id}
                      className={
                        folder.id === selectedFolderId
                          ? "gt-list-item gt-list-item-active"
                          : "gt-list-item"
                      }
                      style={archived ? { opacity: 0.6 } : undefined}
                      onClick={() => setSelectedFolderId(folder.id)}
                    >
                      <span>{folder.name}</span>
                      {archived ? (
                        <span className="tiny" style={{ color: "#92400e", fontWeight: 700 }}>
                          Archived
                        </span>
                      ) : null}
                      {folder.default_hero_image_id ? (
                        <span className="gt-default-badge">Default set ✓</span>
                      ) : null}
                      {archived ? (
                        <button
                          type="button"
                          className="btn btn-light"
                          style={{ minHeight: "auto", padding: "0.2rem 0.5rem", fontSize: "0.75rem" }}
                          onClick={(event) => {
                            event.stopPropagation();
                            void handleRestoreFolder(folder.id, folder.name);
                          }}
                        >
                          Restore
                        </button>
                      ) : null}
                    </li>
                  );
                })}
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
                  <label className="row tiny">
                    <input
                      type="checkbox"
                      checked={Boolean(selectedFolder.show_in_whatsapp_menu)}
                      onChange={() =>
                        handleToggleWhatsappMenu(selectedFolder.id, Boolean(selectedFolder.show_in_whatsapp_menu))
                      }
                      disabled={togglingWhatsappMenu}
                    />
                    Show in WhatsApp menu
                  </label>
                  <button
                    className="btn btn-danger"
                    type="button"
                    onClick={() => handleArchiveFolder(selectedFolder.id, selectedFolder.name)}
                  >
                    Archive garment type
                  </button>
                </div>

                <div className="stack">
                  <h3>Prompt & Category</h3>
                  <div className="grid-2">
                    <label className="field">
                      <span>Category</span>
                      <select value={editCategory} onChange={(event) => setEditCategory(event.target.value)}>
                        <option value="men">Men</option>
                        <option value="women">Women</option>
                        <option value="unisex">Unisex</option>
                      </select>
                    </label>
                  </div>

                  <div className="grid-2">
                    <label className="field">
                      <span>Look Prompt</span>
                      <textarea
                        value={editLookPrompt}
                        onChange={(event) => setEditLookPrompt(event.target.value)}
                      />
                    </label>
                    <label className="field">
                      <span>Try-on Prompt</span>
                      <textarea
                        value={editTryonPrompt}
                        onChange={(event) => setEditTryonPrompt(event.target.value)}
                      />
                    </label>
                  </div>

                  <div className="stack">
                    <p className="tiny muted">Image order Gemini receives:</p>
                    <ul className="hero-list">
                      {imageOrderLines.map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  </div>

                  <label className="field">
                    <span>Note (optional, saved with this version)</span>
                    <input
                      value={editPromptNote}
                      onChange={(event) => setEditPromptNote(event.target.value)}
                      placeholder="e.g. Fixed collar wording"
                    />
                  </label>

                  <button
                    className="btn btn-dark"
                    type="button"
                    disabled={savingPrompt}
                    onClick={() => void handleSavePrompt(selectedFolder.id)}
                  >
                    {savingPrompt ? "Saving..." : "Save Prompt"}
                  </button>

                  <button className="btn btn-light" type="button" onClick={handleTogglePromptHistory}>
                    {showPromptHistory ? "Hide prompt history" : "Show prompt history"}
                  </button>

                  {showPromptHistory ? (
                    <div className="stack" style={{ marginTop: "0.4rem" }}>
                      {loadingPromptVersions ? (
                        <p className="tiny muted">Loading prompt history...</p>
                      ) : promptVersions.length === 0 ? (
                        <div className="empty-box">No saved versions yet.</div>
                      ) : (
                        <ul className="hero-list">
                          {promptVersions.map((version) => (
                            <li key={version.id}>
                              <div className="row" style={{ justifyContent: "space-between" }}>
                                <div className="stack">
                                  <span className="tiny">{new Date(version.created_at).toLocaleString()}</span>
                                  <span className="tiny muted">{version.note || "(no note)"}</span>
                                </div>
                                <button
                                  className="btn btn-light"
                                  type="button"
                                  disabled={revertingVersionId === version.id}
                                  onClick={() => void handleRevertPromptVersion(version.id)}
                                >
                                  {revertingVersionId === version.id ? "Reverting..." : "Revert to this"}
                                </button>
                              </div>
                            </li>
                          ))}
                        </ul>
                      )}
                    </div>
                  ) : null}
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

        {activeTab === "generations" && (
        <section className="card stack">
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h2>Generations</h2>
            <div className="row">
              <label className="field" style={{ minWidth: "220px" }}>
                <span>Garment type</span>
                <select
                  value={generationsFilterFolderId}
                  onChange={(event) => setGenerationsFilterFolderId(event.target.value)}
                >
                  <option value="">All garment types</option>
                  {folders.map((folder) => (
                    <option key={folder.id} value={folder.id}>
                      {folder.name}
                    </option>
                  ))}
                </select>
              </label>
              <button className="btn btn-light" onClick={loadGenerations} disabled={loadingGenerations}>
                {loadingGenerations ? "Loading..." : "Refresh"}
              </button>
            </div>
          </div>

          {generations.length === 0 ? (
            <div className="empty-box">
              {loadingGenerations ? "Loading generations..." : "No generations found."}
            </div>
          ) : (
            <ul className="hero-list">
              {generations.map((gen) => {
                const expanded = expandedGenerationId === gen.id;
                return (
                  <li key={gen.id}>
                    <div
                      className="row"
                      style={{ cursor: "pointer" }}
                      onClick={() => setExpandedGenerationId(expanded ? null : gen.id)}
                    >
                      {gen.output_signed_url ? (
                        <img src={gen.output_signed_url} alt="Generation output" className="gt-hero-thumb" />
                      ) : (
                        <div className="empty-box gt-hero-thumb-placeholder">No image</div>
                      )}
                      <div className="stack grow">
                        <span>{gen.garment_name || "(unknown garment)"}</span>
                        <span className="tiny muted">
                          {gen.model_used || "unknown model"} ·{" "}
                          {new Date(gen.created_at).toLocaleString()}
                        </span>
                      </div>
                      <span
                        className="tiny"
                        style={{
                          background: gen.generation_type === "tryon" ? "#fde68a" : "#bbf7d0",
                          color: "#111827",
                          borderRadius: "6px",
                          padding: "2px 8px",
                          fontWeight: 700,
                          flexShrink: 0,
                        }}
                      >
                        {gen.generation_type === "tryon" ? "TRY-ON" : "LOOK"}
                      </span>
                    </div>

                    {expanded ? (
                      <div className="stack" style={{ marginTop: "0.5rem", paddingLeft: "0.25rem" }}>
                        <div className="row">
                          <div className="stack">
                            <span className="tiny muted">Hero input</span>
                            {gen.hero_signed_url ? (
                              <img src={gen.hero_signed_url} alt="Hero input" className="gt-hero-thumb" />
                            ) : (
                              <div className="empty-box gt-hero-thumb-placeholder">None</div>
                            )}
                          </div>
                          <div className="stack">
                            <span className="tiny muted">Fabric input</span>
                            {gen.fabric_signed_url ? (
                              <img src={gen.fabric_signed_url} alt="Fabric input" className="gt-hero-thumb" />
                            ) : (
                              <div className="empty-box gt-hero-thumb-placeholder">None</div>
                            )}
                          </div>
                        </div>
                        <p className="tiny muted">
                          Type: {gen.generation_type || "-"} · Model: {gen.model_used || "-"} · Status: {gen.status}
                        </p>
                        <div className="stack">
                          <span className="tiny muted">Prompt used:</span>
                          <pre
                            style={{
                              maxHeight: "260px",
                              overflowY: "auto",
                              whiteSpace: "pre-wrap",
                              background: "#f9fafb",
                              border: "1px solid #e5e7eb",
                              borderRadius: "8px",
                              padding: "0.6rem",
                              fontSize: "0.8rem",
                            }}
                          >
                            {gen.prompt_used || "(no prompt recorded)"}
                          </pre>
                        </div>
                      </div>
                    ) : null}
                  </li>
                );
              })}
            </ul>
          )}
        </section>
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

import { useCallback, useEffect, useState } from "react";

import { adminFetch } from "../../lib/api";
import { errorMessage, fabricsSummary, garmentStatus, promptsSummary } from "../../lib/promptLibrary";
import type { GarmentOverviewRow, GarmentsOverviewResponse, PromptLibraryResponse } from "../../lib/types";
import { GarmentPanel } from "./GarmentPanel";
import { useShopScreen } from "./ShopLayout";

const CATEGORY_LABELS: Record<string, string> = { men: "Men", women: "Women", unisex: "Unisex" };

export function GarmentsPage() {
  const { session, shopId, shop } = useShopScreen();

  const [data, setData] = useState<GarmentsOverviewResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [statusText, setStatusText] = useState("");
  const [showArchived, setShowArchived] = useState(false);

  const [library, setLibrary] = useState<PromptLibraryResponse | null>(null);
  const [libraryError, setLibraryError] = useState("");

  // undefined = closed, null = new garment.
  const [panelGarment, setPanelGarment] = useState<GarmentOverviewRow | null | undefined>(undefined);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      setData(
        await adminFetch<GarmentsOverviewResponse>(
          session,
          `/admin/shops/${encodeURIComponent(shopId)}/garments-overview?include_archived=${showArchived}`
        )
      );
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [session, shopId, showArchived]);

  const loadLibrary = useCallback(async () => {
    try {
      setLibrary(await adminFetch<PromptLibraryResponse>(session, "/admin/prompt-library"));
      setLibraryError("");
    } catch (err) {
      setLibraryError(errorMessage(err));
    }
  }, [session]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    void loadLibrary();
  }, [loadLibrary]);

  function openPanel(garment: GarmentOverviewRow | null) {
    setStatusText("");
    void loadLibrary();
    setPanelGarment(garment);
  }

  function closePanel(message?: string) {
    setPanelGarment(undefined);
    if (message) setStatusText(message);
    void load();
  }

  const garments = data?.garments ?? [];
  const latest = data?.latest_versions;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Garments</h1>
          <p className="muted">{shop.name}</p>
        </div>
        <div className="row">
          <label className="check">
            <input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} />
            Show archived
          </label>
          <button className="btn" type="button" onClick={() => void load()} disabled={loading}>
            Refresh
          </button>
          <button className="btn btn-primary" type="button" onClick={() => openPanel(null)}>
            + Add garment
          </button>
        </div>
      </header>

      {statusText ? <p className="notice">{statusText}</p> : null}
      {error ? <p className="error-text">Could not load garments: {error}</p> : null}

      <div className="table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th className="col-thumb">Hero</th>
              <th>Name</th>
              <th>Category</th>
              <th>Fabrics</th>
              <th>Prompts</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {garments.length === 0 ? (
              <tr>
                <td colSpan={6} className="empty-cell">
                  {loading ? "Loading…" : "No garments yet. Click “+ Add garment”."}
                </td>
              </tr>
            ) : (
              garments.map((garment) => {
                const status = garmentStatus(garment);
                const latestVersion = garment.prompt_set && latest ? latest[garment.prompt_set] : null;
                const newer =
                  latestVersion && garment.prompt_set_version && latestVersion > garment.prompt_set_version ? latestVersion : null;
                return (
                  <tr
                    key={garment.id}
                    className={garment.is_active === false ? "row-link is-archived" : "row-link"}
                    tabIndex={0}
                    onClick={() => openPanel(garment)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") openPanel(garment);
                    }}
                  >
                    <td className="col-thumb">
                      {garment.hero_thumb_url ? (
                        <img className="thumb" src={garment.hero_thumb_url} alt="" loading="lazy" />
                      ) : (
                        <div className="thumb thumb-empty">—</div>
                      )}
                    </td>
                    <td className="cell-title">{garment.name}</td>
                    <td>{CATEGORY_LABELS[garment.category || ""] ?? garment.category ?? "—"}</td>
                    <td>{fabricsSummary(garment)}</td>
                    <td>
                      {promptsSummary(garment)}
                      {newer ? <div className="tiny muted">Version {newer} available</div> : null}
                    </td>
                    <td>
                      <span className={status.ready ? "pill pill-ok" : garment.is_active === false ? "pill" : "pill pill-warn"}>
                        {status.text}
                      </span>
                    </td>
                  </tr>
                );
              })
            )}
          </tbody>
        </table>
      </div>

      {panelGarment !== undefined ? (
        <GarmentPanel
          session={session}
          shopId={shopId}
          garment={panelGarment}
          library={library}
          libraryError={libraryError}
          onClose={closePanel}
        />
      ) : null}
    </div>
  );
}

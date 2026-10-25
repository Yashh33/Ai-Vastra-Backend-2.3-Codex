import { useCallback, useEffect, useState } from "react";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { adminFetch } from "../lib/api";
import { useAdminAuth } from "../lib/auth";
import {
  ATTEMPT_TYPE_LABELS,
  NOT_RECORDED,
  STATUS_LABELS,
  formatCredits,
  formatDuration,
  formatIst,
  garmentLabel,
  istDay,
  istDayStartIso,
  istNextDayStartIso,
  statusPillClass,
} from "../lib/operations";
import type { AttemptListResponse, ShopNameRow } from "../lib/types";

const PAGE_SIZE = 15;

export function OperationsPage() {
  const { session } = useAdminAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();

  // Filters live in the URL, so a filtered view can be refreshed or shared.
  const shopId = searchParams.get("shop") ?? "";
  const status = searchParams.get("status") ?? "";
  const attemptType = searchParams.get("type") ?? "";
  const fromDay = searchParams.get("from") ?? istDay(-6);
  const toDay = searchParams.get("to") ?? istDay(0);
  const page = Math.max(1, Number(searchParams.get("page")) || 1);

  const [shops, setShops] = useState<ShopNameRow[]>([]);
  const [data, setData] = useState<AttemptListResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function setFilter(key: string, value: string) {
    const next = new URLSearchParams(searchParams);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== "page") next.delete("page");
    setSearchParams(next);
  }

  useEffect(() => {
    if (!session) return;
    adminFetch<ShopNameRow[]>(session, "/admin/shop-names")
      .then(setShops)
      .catch(() => setShops([]));
  }, [session]);

  const load = useCallback(async () => {
    if (!session) return;
    setLoading(true);
    setError(null);
    try {
      const params = new URLSearchParams({ page: String(page), page_size: String(PAGE_SIZE) });
      if (shopId) params.set("shop_id", shopId);
      if (status) params.set("status", status);
      if (attemptType) params.set("attempt_type", attemptType);
      if (fromDay) params.set("from", istDayStartIso(fromDay));
      if (toDay) params.set("to", istNextDayStartIso(toDay));
      setData(await adminFetch<AttemptListResponse>(session, `/admin/attempts?${params.toString()}`));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }, [session, page, shopId, status, attemptType, fromDay, toDay]);

  useEffect(() => {
    void load();
  }, [load]);

  const totalPages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;
  const summary = data?.summary;

  return (
    <main className="screen">
      <section className="page-shell">
        <header className="header-row">
          <div>
            <h1>Generation logs</h1>
            <p className="muted">Every Generate and Try-on attempt, from arrival to its final result.</p>
          </div>
          <button className="btn btn-light" onClick={() => void load()} disabled={loading}>
            {loading ? "Refreshing..." : "Refresh"}
          </button>
        </header>

        <section className="card ops-filters">
          <label className="field">
            <span>Shop</span>
            <select value={shopId} onChange={(event) => setFilter("shop", event.target.value)}>
              <option value="">All shops</option>
              {shops.map((shop) => (
                <option key={shop.id} value={shop.id}>
                  {shop.name || shop.id}
                </option>
              ))}
            </select>
          </label>
          <label className="field">
            <span>Status</span>
            <select value={status} onChange={(event) => setFilter("status", event.target.value)}>
              <option value="">All</option>
              <option value="succeeded">Succeeded</option>
              <option value="failed">Failed (incl. timed out)</option>
              <option value="running">Running</option>
            </select>
          </label>
          <label className="field">
            <span>Type</span>
            <select value={attemptType} onChange={(event) => setFilter("type", event.target.value)}>
              <option value="">All</option>
              <option value="look">Look</option>
              <option value="tryon">Try-on</option>
              <option value="look_tryon">Look try-on</option>
            </select>
          </label>
          <label className="field">
            <span>From (IST)</span>
            <input type="date" value={fromDay} max={toDay} onChange={(event) => setFilter("from", event.target.value)} />
          </label>
          <label className="field">
            <span>To (IST)</span>
            <input type="date" value={toDay} min={fromDay} onChange={(event) => setFilter("to", event.target.value)} />
          </label>
        </section>

        <section className="ops-tiles">
          <div className="card ops-tile">
            <span className="tiny muted">Attempts</span>
            <strong>{summary ? summary.attempts : "—"}</strong>
          </div>
          <div className="card ops-tile">
            <span className="tiny muted">Succeeded</span>
            <strong>{summary ? summary.succeeded : "—"}</strong>
          </div>
          <div className="card ops-tile">
            <span className="tiny muted">Failed (incl. timed out)</span>
            <strong>{summary ? summary.failed : "—"}</strong>
          </div>
          <div className="card ops-tile">
            <span className="tiny muted">Success rate</span>
            <strong>{summary?.success_rate === null || !summary ? "—" : `${summary.success_rate}%`}</strong>
          </div>
        </section>
        <p className="tiny muted">Tiles count every status for the selected shop, type and dates.</p>

        <section className="card stack">
          {error ? <p className="error-text">Could not load the logs: {error}</p> : null}

          {!data && loading ? (
            <div className="empty-box">Loading…</div>
          ) : data && data.items.length === 0 ? (
            <div className="empty-box">No attempts match these filters.</div>
          ) : data ? (
            <div className="ops-table-wrap">
              <table className="ops-table">
                <thead>
                  <tr>
                    <th>Time (IST)</th>
                    <th>Shop</th>
                    <th>User</th>
                    <th>Type</th>
                    <th>Garment</th>
                    <th>Status</th>
                    <th>Credits</th>
                    <th>Time taken</th>
                    <th>Output</th>
                  </tr>
                </thead>
                <tbody>
                  {data.items.map((row) => (
                    <tr
                      key={row.id}
                      className="ops-row"
                      tabIndex={0}
                      onClick={() => navigate(`/operations/${row.id}`, { state: { from: location.search } })}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") navigate(`/operations/${row.id}`, { state: { from: location.search } });
                      }}
                    >
                      <td className="ops-nowrap">
                        {formatIst(row.created_at)}
                        {row.backfilled ? <span className="tag-imported">imported</span> : null}
                      </td>
                      <td>{row.shop_name || NOT_RECORDED}</td>
                      <td>{row.user_email || row.user_label || NOT_RECORDED}</td>
                      <td className="ops-nowrap">{ATTEMPT_TYPE_LABELS[row.attempt_type] ?? row.attempt_type}</td>
                      <td>{garmentLabel(row.garment_type_name, row.fabric_mode)}</td>
                      <td>
                        <span className={statusPillClass(row.status)}>{STATUS_LABELS[row.status] ?? row.status}</span>
                      </td>
                      <td className="ops-nowrap">{formatCredits(row.credits_charged, row.credits_refunded)}</td>
                      <td className="ops-nowrap">
                        {row.status === "running" || row.status === "started" ? "running…" : formatDuration(row.duration_ms)}
                      </td>
                      <td>
                        {row.thumb_url ? (
                          <img className="ops-thumb" src={row.thumb_url} alt="Output" loading="lazy" />
                        ) : (
                          <span className="tiny muted">{row.has_result ? "image missing" : "—"}</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}

          <div className="row">
            <button
              className="btn btn-light"
              disabled={loading || page <= 1}
              onClick={() => setFilter("page", String(page - 1))}
            >
              Prev
            </button>
            <span className="tiny muted">
              Page {page} of {totalPages}
              {data ? ` · ${data.total} attempt(s)` : ""}
            </span>
            <button
              className="btn btn-light"
              disabled={loading || page >= totalPages}
              onClick={() => setFilter("page", String(page + 1))}
            >
              Next
            </button>
          </div>
        </section>
      </section>
    </main>
  );
}

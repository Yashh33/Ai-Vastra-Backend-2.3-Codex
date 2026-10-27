import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";

import { adminFetch } from "../../lib/api";
import { errorMessage } from "../../lib/promptLibrary";
import type { AdminFolderRow, LookUploadItem, LookUploadResponse, LookUploadStatus } from "../../lib/types";
import { useShopScreen } from "./ShopLayout";

const LOOK_UPLOAD_BATCH_SIZE = 10;

const LOOK_STATUS_STYLE: Record<LookUploadStatus, { label: string; className: string }> = {
  uploaded: { label: "Uploaded", className: "pill pill-ok" },
  duplicate: { label: "Duplicate", className: "pill pill-warn" },
  failed: { label: "Failed", className: "pill pill-bad" },
};

// Mirrors the backend rule: name without extension, trimmed, trailing copy suffixes stripped.
function barcodeFromFilename(filename: string): string {
  let name = (filename.split(/[\\/]/).pop() ?? "").trim();
  const dot = name.lastIndexOf(".");
  if (dot >= 0) name = name.slice(0, dot);
  name = name.trim();

  for (;;) {
    const stripped = name.replace(/(\s*\(\d+\)|\s*-\s*copy)$/i, "").trim();
    if (stripped === name) return name;
    name = stripped;
  }
}

function formatFileSize(bytes: number): string {
  return bytes >= 1024 * 1024 ? `${(bytes / (1024 * 1024)).toFixed(1)} MB` : `${Math.round(bytes / 1024)} KB`;
}

export function BulkLooksPage() {
  const { session, shopId, shop } = useShopScreen();
  const fileInput = useRef<HTMLInputElement | null>(null);

  const [folders, setFolders] = useState<AdminFolderRow[]>([]);
  const [loadError, setLoadError] = useState("");
  const [selectedFolderId, setSelectedFolderId] = useState("");
  const [lookFiles, setLookFiles] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [progress, setProgress] = useState("");
  const [results, setResults] = useState<{ folderName: string; items: LookUploadItem[] } | null>(null);

  const selectedFolder = useMemo(
    () => folders.find((folder) => folder.id === selectedFolderId) || null,
    [folders, selectedFolderId]
  );

  useEffect(() => {
    adminFetch<AdminFolderRow[]>(session, `/admin/shops/${encodeURIComponent(shopId)}/folders?include_archived=false`)
      .then((rows) => {
        setFolders(rows);
        setSelectedFolderId((current) => current || rows[0]?.id || "");
      })
      .catch((err) => setLoadError(errorMessage(err)));
  }, [session, shopId]);

  // A selection made for one garment must never be uploaded into another.
  useEffect(() => {
    setLookFiles([]);
    setResults(null);
    if (fileInput.current) fileInput.current.value = "";
  }, [selectedFolderId]);

  function addFiles(files: FileList | File[] | null) {
    // Dropped folders / documents are skipped; files with no type (e.g. some HEIC) go to the backend as before.
    const images = Array.from(files ?? []).filter((file) => !file.type || file.type.startsWith("image/"));
    if (!images.length) return;
    setResults(null);
    setLookFiles((prev) => {
      const seen = new Set(prev.map((file) => `${file.name}:${file.size}`));
      return [...prev, ...images.filter((file) => !seen.has(`${file.name}:${file.size}`))];
    });
  }

  function handleDrop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    if (!uploading) addFiles(event.dataTransfer.files);
  }

  async function handleUpload() {
    if (!selectedFolder || !lookFiles.length || uploading) return;

    const folderId = selectedFolder.id;
    const folderName = selectedFolder.name;
    const files = lookFiles;
    if (!window.confirm(`Upload ${files.length} look${files.length === 1 ? "" : "s"} to ${folderName}?`)) return;

    const batches: File[][] = [];
    for (let i = 0; i < files.length; i += LOOK_UPLOAD_BATCH_SIZE) {
      batches.push(files.slice(i, i + LOOK_UPLOAD_BATCH_SIZE));
    }

    setUploading(true);
    setResults(null);
    const items: LookUploadItem[] = [];
    try {
      for (let index = 0; index < batches.length; index += 1) {
        const batch = batches[index];
        setProgress(`Batch ${index + 1} of ${batches.length}…`);
        const formData = new FormData();
        formData.append("folder_id", folderId);
        for (const file of batch) formData.append("files", file);
        try {
          const result = await adminFetch<LookUploadResponse>(
            session,
            `/admin/shops/${encodeURIComponent(shopId)}/looks/upload-bulk`,
            { method: "POST", body: formData }
          );
          items.push(...result.items);
        } catch (err) {
          for (const file of batch) {
            items.push({ filename: file.name, barcode: barcodeFromFilename(file.name) || null, status: "failed", error: errorMessage(err) });
          }
        }
      }
    } finally {
      setResults({ folderName, items });
      setLookFiles([]);
      if (fileInput.current) fileInput.current.value = "";
      setProgress("");
      setUploading(false);
    }
  }

  const count = (status: LookUploadStatus) => results?.items.filter((item) => item.status === status).length ?? 0;

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Bulk looks</h1>
          <p className="muted">Upload finished look photos to Browse for {shop.name}.</p>
        </div>
      </header>

      {loadError ? <p className="error-text">Could not load garments: {loadError}</p> : null}

      <section className="section">
        <div className="grid-2">
          <label className="field">
            <span>Garment</span>
            <select className="input" value={selectedFolderId} onChange={(event) => setSelectedFolderId(event.target.value)} disabled={uploading}>
              {folders.length === 0 ? <option value="">No garments yet</option> : null}
              {folders.map((folder) => (
                <option key={folder.id} value={folder.id}>
                  {folder.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div
          className={dragging ? "dropzone is-dragging" : "dropzone"}
          onDragOver={(event) => {
            event.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={handleDrop}
          onClick={() => !uploading && fileInput.current?.click()}
          role="button"
          tabIndex={0}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !uploading) fileInput.current?.click();
          }}
        >
          <strong>Drop images here, or click to choose</strong>
          <span className="tiny muted">
            File name = barcode (e.g. S945132010.jpg). Images are compressed automatically. MRP and description are added
            later by the shop.
          </span>
          <input
            ref={fileInput}
            type="file"
            accept="image/*"
            multiple
            hidden
            disabled={uploading}
            onChange={(event) => addFiles(event.target.files)}
          />
        </div>

        {lookFiles.length > 0 ? (
          <div className="stack">
            <div className="row">
              <span className="tiny muted">{lookFiles.length} selected</span>
              <button className="btn btn-small" type="button" onClick={() => setLookFiles([])} disabled={uploading}>
                Clear
              </button>
            </div>
            <div className="table-wrap">
              <table className="table table-compact">
                <thead>
                  <tr>
                    <th>File</th>
                    <th>Barcode</th>
                    <th className="num">Size</th>
                  </tr>
                </thead>
                <tbody>
                  {lookFiles.map((file, index) => {
                    const barcode = barcodeFromFilename(file.name);
                    return (
                      <tr key={`${file.name}-${index}`}>
                        <td>{file.name}</td>
                        <td>{barcode ? <code>{barcode}</code> : <span className="error-text">no barcode</span>}</td>
                        <td className="num">{formatFileSize(file.size)}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </div>
        ) : null}

        <div>
          <button
            className="btn btn-primary"
            type="button"
            disabled={uploading || lookFiles.length === 0 || !selectedFolder}
            onClick={() => void handleUpload()}
          >
            {uploading
              ? progress || "Uploading…"
              : `Upload ${lookFiles.length} look${lookFiles.length === 1 ? "" : "s"}${selectedFolder ? ` to ${selectedFolder.name}` : ""}`}
          </button>
        </div>
      </section>

      {results ? (
        <section className="section">
          <h2>
            Uploaded {count("uploaded")} · Duplicates {count("duplicate")} · Failed {count("failed")}
            <span className="tiny muted"> (into {results.folderName})</span>
          </h2>
          <div className="table-wrap">
            <table className="table table-compact">
              <thead>
                <tr>
                  <th>File</th>
                  <th>Barcode</th>
                  <th>Status</th>
                  <th>Error</th>
                </tr>
              </thead>
              <tbody>
                {results.items.map((item, index) => (
                  <tr key={`${item.filename}-${index}`}>
                    <td>{item.filename}</td>
                    <td>{item.barcode || "—"}</td>
                    <td>
                      <span className={LOOK_STATUS_STYLE[item.status].className}>{LOOK_STATUS_STYLE[item.status].label}</span>
                    </td>
                    <td>{item.error || ""}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      ) : null}
    </div>
  );
}

import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";

import { adminFetch } from "../lib/api";
import { useAdminAuth } from "../lib/auth";
import { copyText } from "../lib/clipboard";
import {
  ATTEMPT_TYPE_LABELS,
  FAILED_STEP_LABELS,
  NOT_RECORDED,
  STATUS_LABELS,
  formatCredits,
  formatDuration,
  formatInr,
  formatIst,
  garmentLabel,
  statusPillClass,
} from "../lib/operations";
import type { AttemptDetail } from "../lib/types";

function yesNo(value: boolean | null | undefined): string {
  if (value === null || value === undefined) return NOT_RECORDED;
  return value ? "yes" : "no";
}

function CopyButton({ text, label = "Copy" }: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <button
      type="button"
      className="btn btn-light ops-small-btn"
      onClick={async () => {
        if (await copyText(text)) {
          setCopied(true);
          window.setTimeout(() => setCopied(false), 1500);
        }
      }}
    >
      {copied ? "Copied" : label}
    </button>
  );
}

function ImageBox({
  url,
  alt,
  missingText,
  onOpen,
}: {
  url: string | null;
  alt: string;
  missingText: string;
  onOpen: (url: string) => void;
}) {
  if (!url) return <div className="ops-image-missing tiny muted">{missingText}</div>;
  return (
    <button type="button" className="ops-image-btn" onClick={() => onOpen(url)} title="Open full size">
      <img src={url} alt={alt} loading="lazy" />
    </button>
  );
}

export function OperationsDetailPage() {
  const { attemptId } = useParams<{ attemptId: string }>();
  const { session } = useAdminAuth();
  const location = useLocation();
  const backSearch = (location.state as { from?: string } | null)?.from ?? "";

  const [attempt, setAttempt] = useState<AttemptDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [fullImage, setFullImage] = useState<string | null>(null);

  useEffect(() => {
    if (!session || !attemptId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    adminFetch<AttemptDetail>(session, `/admin/attempts/${encodeURIComponent(attemptId)}`)
      .then((row) => {
        if (!cancelled) setAttempt(row);
      })
      .catch((err) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Unknown error");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [session, attemptId]);

  const backLink = (
    <Link className="btn btn-light" to={`/operations${backSearch}`}>
      ← Back to logs
    </Link>
  );

  if (loading) {
    return (
      <main className="screen">
        <section className="page-shell">
          {backLink}
          <div className="empty-box">Loading…</div>
        </section>
      </main>
    );
  }

  if (error || !attempt) {
    return (
      <main className="screen">
        <section className="page-shell">
          {backLink}
          <p className="error-text">Could not load this attempt: {error ?? "not found"}</p>
        </section>
      </main>
    );
  }

  const isTryon = attempt.attempt_type !== "look";
  const failed = attempt.status === "failed" || attempt.status === "timed_out";
  const resultUrl = isTryon ? attempt.tryon_result_url : attempt.output_url;
  const resultPath = isTryon ? attempt.tryon_result_path : attempt.output_path;
  const stepLabel = attempt.failed_step ? FAILED_STEP_LABELS[attempt.failed_step] ?? attempt.failed_step : null;
  const timedOutBeforeSweep = attempt.status === "timed_out" && attempt.raw_status !== "timed_out";

  return (
    <main className="screen">
      <section className="page-shell">
        <div className="row">{backLink}</div>

        <header className="card stack">
          <div className="row">
            <h1 className="grow">
              {ATTEMPT_TYPE_LABELS[attempt.attempt_type] ?? attempt.attempt_type} ·{" "}
              {garmentLabel(attempt.garment_type_name, attempt.fabric_mode)}
            </h1>
            {attempt.backfilled ? <span className="tag-imported">imported</span> : null}
            <span className={statusPillClass(attempt.status)}>{STATUS_LABELS[attempt.status] ?? attempt.status}</span>
          </div>
          <p className="muted">
            {attempt.shop_name || NOT_RECORDED} · {attempt.user_email || attempt.user_label || NOT_RECORDED} ·{" "}
            {formatIst(attempt.started_at || attempt.created_at)} · {attempt.device_label || NOT_RECORDED}
          </p>
          {attempt.backfilled ? (
            <p className="tiny muted">
              Imported from an existing look made before this log existed. Fields that were never recorded show
              “{NOT_RECORDED}”.
            </p>
          ) : null}
        </header>

        <section className="ops-tiles">
          <div className="card ops-tile">
            <span className="tiny muted">Credits charged</span>
            <strong>{attempt.credits_charged ?? (attempt.backfilled ? NOT_RECORDED : 0)}</strong>
          </div>
          <div className="card ops-tile">
            <span className="tiny muted">Refunded</span>
            <strong>{attempt.credits_refunded ?? (failed ? NOT_RECORDED : 0)}</strong>
          </div>
          <div className="card ops-tile">
            <span className="tiny muted">AI cost</span>
            <strong>{formatInr(attempt.ai_cost_inr)}</strong>
          </div>
          <div className="card ops-tile">
            <span className="tiny muted">Time taken</span>
            <strong>
              {attempt.status === "running" || attempt.status === "started"
                ? "running…"
                : formatDuration(attempt.duration_ms)}
            </strong>
          </div>
        </section>
        <p className="tiny muted">
          Net credits: {formatCredits(attempt.credits_charged, attempt.credits_refunded)} · AI tokens in/out:{" "}
          {attempt.ai_input_tokens ?? NOT_RECORDED} / {attempt.ai_output_tokens ?? NOT_RECORDED} · AI time:{" "}
          {attempt.ai_started_at && attempt.ai_finished_at
            ? formatDuration(new Date(attempt.ai_finished_at).getTime() - new Date(attempt.ai_started_at).getTime())
            : NOT_RECORDED}
        </p>

        {failed ? (
          <section className="card stack ops-error-box">
            <h2>What went wrong</h2>
            <p>
              <strong>{stepLabel ?? NOT_RECORDED}</strong>
              {attempt.http_status ? <span className="tiny muted"> · HTTP {attempt.http_status}</span> : null}
            </p>
            {timedOutBeforeSweep ? (
              <p className="tiny muted">
                Still marked “{attempt.raw_status}” in the database, but it has had no result for over 5 minutes.
              </p>
            ) : null}
            <div className="stack" style={{ gap: "0.25rem" }}>
              <span className="tiny muted">What the user was shown</span>
              <p>{attempt.user_message || NOT_RECORDED}</p>
            </div>
            {attempt.ai_finish_reason ? (
              <p className="tiny muted">AI finish reason: {attempt.ai_finish_reason}</p>
            ) : null}
            {attempt.error_detail ? (
              <details>
                <summary className="tiny">Technical error (raw)</summary>
                <pre className="ops-pre">{attempt.error_detail}</pre>
              </details>
            ) : (
              <p className="tiny muted">Technical error: {NOT_RECORDED}</p>
            )}
          </section>
        ) : null}

        <section className="card stack">
          <h2>Inputs</h2>
          <div className="ops-inputs">
            <figure className="ops-figure">
              <ImageBox
                url={attempt.hero_url}
                alt="Hero"
                missingText={attempt.hero_path ? "image no longer in storage" : NOT_RECORDED}
                onOpen={setFullImage}
              />
              <figcaption className="tiny">
                <strong>{attempt.attempt_type === "look_tryon" ? "Look used as input" : "Hero image"}</strong>
                <br />
                colour fixed: {yesNo(attempt.hero_colour_corrected)}
              </figcaption>
            </figure>

            {attempt.fabric_inputs.map((fabric, index) => (
              <figure className="ops-figure" key={`${fabric.storage_path ?? "fabric"}-${index}`}>
                <ImageBox
                  url={fabric.url}
                  alt={fabric.slot_label || "Fabric"}
                  missingText={fabric.storage_path ? "image no longer in storage" : NOT_RECORDED}
                  onOpen={setFullImage}
                />
                <figcaption className="tiny">
                  <strong>{fabric.slot_label || fabric.apply_to || `Fabric ${index + 1}`}</strong>
                  {fabric.slot_label && fabric.apply_to ? <span className="muted"> ({fabric.apply_to})</span> : null}
                  <br />
                  colour fixed: {yesNo(fabric.colour_corrected)}
                </figcaption>
              </figure>
            ))}

            {attempt.attempt_type === "look" && attempt.fabric_inputs.length === 0 ? (
              <p className="tiny muted">Fabrics: {NOT_RECORDED}</p>
            ) : null}

            {isTryon ? (
              <figure className="ops-figure">
                <div className="ops-image-missing tiny muted">Customer photo not stored</div>
                <figcaption className="tiny">
                  <strong>Customer photo</strong>
                </figcaption>
              </figure>
            ) : null}
          </div>
        </section>

        <section className="card stack">
          <h2>Result</h2>
          {resultUrl ? (
            <ImageBox url={resultUrl} alt="Result" missingText="" onOpen={setFullImage} />
          ) : (
            <div className="empty-box">
              {resultPath
                ? "The result image is no longer in storage."
                : isTryon && attempt.status === "succeeded"
                  ? "Try-on results are not stored unless they are pushed to the TV."
                  : "No result image."}
            </div>
          )}
          {resultUrl ? <p className="tiny muted">Click the image to open it full size.</p> : null}
        </section>

        <section className="card stack">
          <div className="row">
            <h2 className="grow">Prompt</h2>
            {attempt.prompt_text ? <CopyButton text={attempt.prompt_text} /> : null}
          </div>
          <p className="tiny muted">
            Version: {attempt.prompt_version_label || NOT_RECORDED} · Model: {attempt.model_id || NOT_RECORDED}
          </p>
          {attempt.prompt_text ? (
            <pre className="ops-pre">{attempt.prompt_text}</pre>
          ) : (
            <p className="tiny muted">{NOT_RECORDED}</p>
          )}
        </section>

        <section className="card stack">
          <h2>Request</h2>
          <div className="row">
            <span className="tiny muted">Request ID</span>
            <code>{attempt.request_id}</code>
            <CopyButton text={attempt.request_id} />
          </div>
          <p className="tiny muted">Search this ID in the Render logs to find the server log lines for this attempt.</p>
          {attempt.client_request_id && attempt.client_request_id !== attempt.request_id ? (
            <div className="row">
              <span className="tiny muted">App's request ID</span>
              <code>{attempt.client_request_id}</code>
              <CopyButton text={attempt.client_request_id} />
            </div>
          ) : null}
          <p className="tiny muted">
            Endpoint: {attempt.endpoint || NOT_RECORDED} · Generation: {attempt.generation_id || "—"} · Finished:{" "}
            {attempt.finished_at ? formatIst(attempt.finished_at) : "—"}
          </p>
        </section>
      </section>

      {fullImage ? (
        <div className="modal-overlay" role="dialog" aria-label="Full size image" onClick={() => setFullImage(null)}>
          <img className="ops-full-image" src={fullImage} alt="Full size" />
        </div>
      ) : null}
    </main>
  );
}

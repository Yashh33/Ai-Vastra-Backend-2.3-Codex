import { useState, type FormEvent } from "react";

import { adminFetch } from "../../lib/api";
import { CREDITS_PER_LOOK, errorMessage, looksFor } from "../../lib/promptLibrary";
import { useShopScreen } from "./ShopLayout";

export function CreditsPage() {
  const { session, shopId, shop, patchShop } = useShopScreen();

  const [creditDelta, setCreditDelta] = useState("");
  const [creditReason, setCreditReason] = useState("");
  const [granting, setGranting] = useState(false);
  const [statusText, setStatusText] = useState("");

  const balance = shop.credits_balance ?? 0;
  const delta = Number(creditDelta) || 0;

  async function handleGrantCredits(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const reason = creditReason.trim();
    if (!reason || !delta) {
      setStatusText("Enter a non-zero amount and a reason to update credits.");
      return;
    }

    setGranting(true);
    try {
      const result = await adminFetch<{ balance_before: number; balance_after: number }>(
        session,
        `/admin/shops/${encodeURIComponent(shopId)}/credits`,
        { method: "POST", body: JSON.stringify({ delta, reason }) }
      );
      patchShop({ credits_balance: result.balance_after });
      setCreditDelta("");
      setCreditReason("");
      setStatusText(`Credits updated: ${result.balance_before} → ${result.balance_after}.`);
    } catch (err) {
      setStatusText(`Credits update failed: ${errorMessage(err)}`);
    } finally {
      setGranting(false);
    }
  }

  return (
    <div className="page">
      <header className="page-head">
        <div>
          <h1>Credits</h1>
          <p className="muted">{shop.name}</p>
        </div>
      </header>

      <section className="section">
        <span className="kv-label">Current balance</span>
        <div className="balance">
          <strong>{balance}</strong> credits
          <span className="muted"> ≈ {looksFor(balance)} looks</span>
        </div>
        <p className="tiny muted">{CREDITS_PER_LOOK} credits = 1 look.</p>
      </section>

      <section className="section">
        <h2>Add or remove credits</h2>
        <form className="stack" onSubmit={handleGrantCredits}>
          <div className="grid-2">
            <label className="field">
              <span>Amount (use a minus sign to remove)</span>
              <input
                className="input"
                type="number"
                value={creditDelta}
                onChange={(event) => setCreditDelta(event.target.value)}
                placeholder="e.g. 500 or -100"
                disabled={granting}
              />
              <span className="tiny muted">
                ≈ {looksFor(delta)} looks{delta ? ` · new balance ${balance + delta}` : ""}
              </span>
            </label>
            <label className="field">
              <span>Reason</span>
              <input
                className="input"
                value={creditReason}
                onChange={(event) => setCreditReason(event.target.value)}
                placeholder="e.g. Monthly grant"
                disabled={granting}
              />
            </label>
          </div>
          <div className="row">
            <button className="btn btn-primary" type="submit" disabled={granting || !delta || !creditReason.trim()}>
              {granting ? "Updating…" : "Update"}
            </button>
            {statusText ? <span className="tiny muted">{statusText}</span> : null}
          </div>
        </form>
      </section>
    </div>
  );
}

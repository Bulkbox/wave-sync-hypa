# wave_sync_hypa — Project Status

**As of 2026‑06‑08.** Status reflects what is actually merged to `main` (verified
against git history), not work-in-progress branches.

---

## 1. What this app does

`wave_sync_hypa` is a Frappe/ERPNext app that bridges the **Wave grocery
storefront** with **ERPNext** for Bulkbox (Three Spears Limited). It keeps the
two systems in step in both directions:

- **Wave → ERPNext (inbound):** Wave webhooks create/update ERP Customers,
  Addresses, and Sales Orders, and reconcile Pick Lists.
- **ERPNext → Wave (outbound):** ERP document events push order **status**,
  **stock levels**, **picker batch IDs**, and prepaid **payment status** back to
  Wave; ERP-raised orders can be pushed to create Wave orders.

Everything is configured from one **Wave Settings** page (see
`WAVE_SETTINGS_GUIDE.md`) so behaviour changes without a deploy.

---

## 2. How it works (at a glance)

**Inbound:** Wave → `api/webhook` (authenticate + log + enqueue) → background
`processor` (master-switch + duplicate check) → a handler per event
(`order_create`, `customer`, `order_update`, …) → ERP records. Every step is
written to the **Wave Sync Log** with a shared correlation id for traceability.

**Outbound:** an ERP document event (SO submit, DN submit, SI submit, stock
move, PE submit) → a handler resolves what to send via the **rules tables** →
an async worker calls the Wave REST API → logged. All outbound workers
**never raise** (a Wave outage can't break an ERP save) and re-read the master
switch mid-queue.

**Status ladder:** `ACCEPTED` (Pick List) → `INVOICING` (DN submit) →
`UNDER_DELIVERY` (SI submit) → `COMPLETED` (Shipday "Delivered"); `CANCELLED`
on a full-value credit note or SO cancel.

---

## 3. Live capabilities (merged to `main`)

| Area | Capability |
|---|---|
| **Inbound** | Authenticated webhook intake; idempotent processing; customer upsert + append-only addresses (incl. B2B classification); order intake → draft Sales Order with fees/taxes; ORDER.UPDATE; Pick List COLLECTED reconciliation. |
| **Outbound status** | SO/DN/SI/Pick List events push the right Wave status via editable rules; terminal-state rejections soft-skipped; full credit note → CANCELLED. |
| **Stock** | ERP stock movements push to Wave (resolve product by SKU, cache the id); optional max-quantity cap mirror; 3 manual resync entry points. |
| **Pick List** | Picker batch-IDs / barcode push; ERP submit/cancel lockdown behind a role; picker-identifier modes. |
| **Payments** | PE submit pushes Wave **paymentStatus** (decoupled from order status); a before-submit validator guards prepaid PEs. |
| **iPay** | Prepaid orders are **verified** against iPay (button + auto on creation), payment details stamped, unverified ones flagged for accounting. |
| **Completion** | Shipday "Delivered" pushes Wave **COMPLETED**. |
| **Resilience** | **Resilient intake** — a resolvable order is never dropped (bad items soft-skip, customer falls back to walk-in, disabled customer re-enabled, delivery date clamped); **operator replay** of any failed order from the Wave Sync Log; reused soft-deleted addresses are re-linked. |
| **Safety** | Master kill-switch (default off on fresh install); per-channel toggles; full audit log with retention. |

**Tests:** ~420+ unit tests (fast, mocked) plus an integration suite; green on
`main`.

---

## 4. In progress (open PRs, awaiting merge)

| PR | What | Notes |
|---|---|---|
| **#150** | **Wave Settings config pre-flight** — block saving an unusable Default Warehouse/Company/Price List/Currency/Walk-in, and can't *enable* with one missing. | Closes #149. Save-time only. |
| **#151** | **Non-Shipday completion** — a "Mark Delivered on Wave" button on the Sales Order, plus an opt-in auto-push to COMPLETED when a fully-settled Payment Entry submits (new setting, default off). | Closes #118. |

> Both carry two **pre-existing, unrelated** red CI checks (a repo-wide
> code-formatting drift, and a CI-runner MariaDB install that broke when the
> runner image updated) — neither is caused by these PRs; both are noted in the
> backlog below.

---

## 5. Backlog (open issues)

| Issue | Summary | Priority |
|---|---|---|
| **#10** | Phase 9 — promotions & coupons mapping (the last unbuilt feature phase). | Feature |
| **#133** | Prepaid SI has no retry path to (re)create the iPay Payment Entry → operator deadlock. Parked for team design. | Team |
| **#105** | Polish follow-ups from an earlier self-review. | Low |
| **#122** | Test suite hits a `TooManyWritesError` at the tail (log-retention test commits). Test-infra hygiene. | Low (CI) |
| **#148** | CI "Frappe Linter" fails on repo-wide `ruff-format` drift (≈67 files). Deferred. | Low (CI) |

### Other tracked notes
- **Server CI check** fails at *Install MariaDB Client* (`mariadb-client-10.6`
  no longer in the runner's apt sources). One-line workflow fix; **deferred** as
  pure infra (not app code).
- **#113 re-opened in spirit:** the experimental Pick List "amend → reset Wave
  picker state" was **removed** (it was wiping picking details on Wave); an
  amended Pick List now stays COLLECTED on Wave. A *targeted* re-pick reset is a
  future, team-designed task.

---

## 6. Discrepancy to resolve

**Issue #131 was closed as "implemented via PR #132 (in-app iPay Payment Entry
auto-create)", but PR #132 was never merged to `main`.** The
`prepaid_pe_creator` service and the `ipay_auto_create_payment_entry` setting do
**not** exist on `main`. Today, prepaid **verification** is live (#130) but the
**Payment Entry itself is created by the external n8n automation**, not by this
app. Action: re-open #131 (or re-merge #132) if in-app PE creation is wanted.

---

## 7. Operational notes

- **Enabling flag-gated features in production** (e.g. picker lockdown, iPay
  verification) is a business decision; defaults are conservative.
- After merging a PR that changes Python, the dev/prod bench needs a
  **`bench restart`**; PRs that add a Wave Settings field also need
  **`bench migrate`**.
- **Live verification done:** the `paymentStatus` PATCH contract is confirmed
  against Wave; the picker-state reset was verified and then removed.
- **Dev config quick-wins** (see `WAVE_SETTINGS_GUIDE.md` §9): raise Log
  Retention from 1 day, add Fee Mappings, and set the Wave Payment Review
  Assignee.

---

*Companion document: `WAVE_SETTINGS_GUIDE.md` — every setting explained for a
manager, with the live dev values.*

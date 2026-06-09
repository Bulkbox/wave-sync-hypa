# Wave Settings — Manager's Guide

This guide explains every option on the **Wave Settings** page (the single
control panel for the Wave ⇄ ERPNext integration) in plain language, with an
example and the **value currently configured on the dev site**
(`dev.bulkbox.cloud`, captured 2026‑06‑08).

> Where to find it: search **"Wave Settings"** in ERPNext (Awesomebar). It is a
> single settings document — there is only ever one. Editing it takes effect
> immediately; no deploy needed.

---

## 1. The master switch (read this first)

**Integration Enabled** (`enabled`) — the single on/off switch for the whole
integration. When **off**, Wave's webhooks are still received, authenticated and
logged (and acknowledged so Wave doesn't keep retrying), **but nothing is
created in ERP and nothing is pushed back to Wave.** Use it to pause everything
during a cutover without losing data.

> **Dev: ON.**

Most other switches below are *channel-level* toggles — they let you mute one
part of the integration (e.g. stock sync) without turning off the whole thing.

---

## 2. Integration section

| Option | What it means | Example | Dev value |
|---|---|---|---|
| **Customer Email Fallback Lookup Enabled** | When Wave's customer can't be matched by its Wave ID, also try matching an existing ERP customer by email (prevents duplicate customers). Skips ambiguous matches safely. | A customer added manually in ERP last year places their first Wave order → matched by email instead of creating a duplicate. | **ON** |
| **Pick List Inbound Submit Enabled** | When Wave reports an order was picked ("COLLECTED"), auto-submit the matching draft Pick List in ERP. Already-submitted lists are only commented on, never changed. | Picker finishes in the Wave app → the ERP Pick List submits itself. | **ON** |
| **Shipping Item Tax Template** | Tax template applied to the shipping-fee line so the line + tax equals exactly what Wave charged. Leave blank to use Wave's amount as-is. | Set to "KES VAT 16%" so a 116 shipping fee is stored as 100 + 16 tax. | *(blank)* |
| **Intake Review ToDo Enabled** | When an order needs manual review (e.g. an unmapped fee), also raise a ToDo task so the team is pinged in "My Tasks". | An order with an unknown shipping fee → a ToDo appears for the assignee. | **ON** |
| **Intake Review Assignee** | The single person who receives those review ToDos. | `rashid@bulkbox.co.ke` | **rashid@bulkbox.co.ke** |
| **Intake Review Role** | If no single assignee is set, fan the ToDos out to everyone in this role instead. | "Accounts Manager" | *(blank)* |
| **Price Scale Divisor** | Wave sends money in cents; this divides by it to get shillings. Leave at 100. | Wave sends `22000` → stored as `220.00`. | **100** |
| **Log Retention (days)** | Audit-log rows older than this are deleted daily. | `14` keeps two weeks of logs. | **1** ⚠️ *(see "Things to review")* |

---

## 3. Inbound Authentication

| Option | What it means | Dev value |
|---|---|---|
| **Inbound API Key (32 URL-safe chars)** | The shared secret Wave must send on every webhook; requests without it are rejected. Stored encrypted. Must be exactly 32 characters (letters/numbers/`_`/`-`). | **set** (hidden) |

---

## 4. Outbound (the Wave API connection + what we push back)

These are the credentials and switches for **ERP → Wave** traffic.

| Option | What it means | Dev value |
|---|---|---|
| **Wave API Base URL** | The Wave server we call. | `https://dev.hypaafrica.api.wavegrocery.com` |
| **Wave App ID** | App identifier Wave issued us; sent on every call. | `hypaafrica-integration` |
| **Wave Store ID** | The human-readable store number (usually `1`). | `1` |
| **Wave Shop ID (mongo _id)** | Wave's internal shop id, used only when ERP *creates* an order on Wave. | `698ef51f728782a10adcef6d` |
| **Wave API Key** | Secret key sent on every outbound call. Stored encrypted. | **set** (hidden) |
| **Outbound Stock Sync Enabled** | Push ERP stock changes to Wave so the storefront shows correct availability. | **ON** |
| **Outbound Stock Caps Max Quantity Enabled** | Also mirror each item's max-orderable quantity to Wave. | **ON** |
| **Outbound Order Status Sync Enabled** | Push order **status** changes (ACCEPTED → INVOICING → UNDER_DELIVERY → COMPLETED / CANCELLED) to Wave as the order moves through ERP. | **ON** |
| **Pick List Batch IDs Push Enabled** | When a Pick List is created, send the picked items + identifiers to Wave's picker app. | **ON** |
| **Picker Identifier Source** | What the picker app scans per line: blank = batch numbers, or "Item Code" / "Item Barcode". | **Item Barcode** |
| **Pick List ERP Submit Lockdown Enabled** | Once on, submitting/cancelling a Pick List directly in ERP needs the "Pick List Wave Override" role — making Wave's picker app the source of truth. | **ON** |
| **Wave Pickup Driver** | The driver/employee stamped on the Delivery Note for **pickup** orders (so the DN has a driver). | `HR-DRI-2022-00002` |

---

## 5. ERP → Wave Push (creating Wave orders from ERP)

For orders raised **in ERP** (offline / walk-in) that you want mirrored to Wave.

| Option | What it means | Dev value |
|---|---|---|
| **ERP to Wave Push Enabled** | Enables the "Push to Wave" button on Sales Orders. | **ON** |
| **Wave Common Offline Customer ID** | The Wave customer used as the buyer for ERP-originated orders. | `69b9a8171dad235b5e857d1d` |
| **Wave Default Offline Payment Type** | The payment type stamped on those pushed orders. | `cash` |
| **Wave Push Failure ToDo Enabled** | If a push fails, raise a ToDo so someone fixes and retries. | **ON** |

---

## 6. ERP Defaults (what new Wave orders are built with)

Every Wave order that comes in becomes an ERPNext Sales Order built with these
defaults. **If any of these is wrong, orders fail to create** — which is why a
save-time check on them is being added (PR #150).

| Option | What it means | Dev value |
|---|---|---|
| **Default Company** | The company the Sales Order belongs to. | `Three Spears Limited` |
| **Default Warehouse** | Stock warehouse on each order line. | `Storage - TSL` |
| **Default Price List** | The selling price list used to price items. | `Standard Selling` |
| **Default Currency** | Order currency. | `KES` |
| **Default Unresolved Items Placeholder** | A stand-in Item used when an order's product isn't found in ERP, so the order is still captured (and flagged) rather than lost. | `TEST01` |
| **Default Customer Group** | Group assigned to newly created customers. | `Consumer` |
| **Default Territory** | Territory assigned to newly created customers. | `Kenya` |
| **Walk-in Customer** | The customer used for guest checkouts (and the safety fallback if a real customer can't be created). | `Bulkbox Office` |

---

## 7. Rules tables (how statuses, fees, taxes and payments are mapped)

These tables let you change behaviour without code. Below is what's configured
on dev.

### Outbound Status Rules — *5 rows configured*
"When this happens in ERP, set the Wave order to this status."

| ERP document | ERP event | → Wave status |
|---|---|---|
| Sales Order | cancel | CANCELLED |
| Delivery Note | submit | INVOICING |
| Sales Invoice | submit (non-return) | UNDER_DELIVERY |
| Pick List | created | ACCEPTED |
| Pick List | cancel | PENDING |

> COMPLETED is **not** in this table — it's pushed when Shipday reports the
> delivery as *Delivered* (and, with PR #151, optionally on payment or via a
> manual button for non-Shipday orders).

### Route Rules — *4 rows* · Inbound Status Rules — *1 row*
Internal routing of which Wave webhook types are processed; rarely changed by an
operator.

### Fee Mappings — *0 rows* ⚠️
Maps a Wave fee (e.g. `SHIPPING_COST`) to the ERP Item used for that line.
**None configured on dev**, so orders carrying a shipping/bag fee are created
but **flagged for manual review** (the fee line can't be added automatically).

### Tax Rules — *0 rows*
Optional auto-application of a tax template to new orders. **None on dev.**

### Payment Method Mappings — *8 rows configured*
Classifies each Wave payment type as **prepaid** (paid online) or **cod** (cash
on delivery). This drives how payment is handled downstream.

| Wave payment type | Classification | Mode of Payment |
|---|---|---|
| card, klarna, mobile, bankTransfer, thirdPartyReference | **prepaid** | *(not linked)* |
| cardOnDelivery, irisOnDelivery, cash | **cod** | *(not linked)* |

> "Mode of Payment" is intentionally optional — classification alone drives the
> logic; linking a Mode of Payment only powers an advisory check.

---

## 8. iPay (prepaid payment verification)

| Option | What it means | Dev value |
|---|---|---|
| **iPay Verification Enabled** | For prepaid orders, look the payment up on iPay (by the Wave friendly id), stamp the confirmed payment details on the Sales Order, and flag it for accounting if it can't be verified. Also adds a **"Verify iPay Payment"** button on the order. | **ON** |
| **Wave Payment Review Assignee** | Who receives the accounting flag/ToDo when a prepaid payment can't be verified. | *(blank)* ⚠️ |

> Note: the integration **verifies** prepaid payments but does **not** create the
> Payment Entry itself — that is done by the separate n8n automation. (An
> in-app PE-creation feature was prototyped but is **not deployed**.)

---

## 9. Things worth reviewing on dev

These are configuration observations, not bugs — flagged so a manager can decide:

1. **Log Retention = 1 day.** Audit logs are purged daily, so troubleshooting an
   issue older than a day loses its trail. Production typically uses **14**.
2. **No Fee Mappings.** Any Wave order with a shipping/plastic-bag fee will be
   created **flagged for review** because the fee line can't be mapped to an ERP
   item. Add a row per fee type to automate it.
3. **No Tax Rules.** New orders get no automatic tax template (tax then relies on
   the customer/item masters). Fine if intended.
4. **Wave Payment Review Assignee is blank.** When a prepaid payment fails
   verification, the flag/ToDo has **no recipient** — nobody is notified. Set a
   person here.
5. **Payment Method Mappings have no Mode of Payment linked.** Harmless today
   (only an advisory check uses it), but link them if you want the cross-check.

---

## 10. Current dev configuration — at a glance

| Switch | State |
|---|---|
| Integration Enabled | **ON** |
| Customer Email Fallback | ON |
| Pick List Inbound Submit | ON |
| Outbound Stock Sync | ON · Max-Qty cap ON |
| Outbound Order Status Sync | ON |
| Pick List Batch IDs Push | ON (Identifier = Item Barcode) |
| Pick List ERP Submit Lockdown | ON |
| ERP → Wave Push | ON |
| iPay Verification | ON |
| Intake Review ToDo | ON → rashid@bulkbox.co.ke |
| Price Scale Divisor / Log Retention | 100 / **1 day** |

*Generated from the dev site on 2026‑06‑08. Re-run the snapshot after any
settings change.*

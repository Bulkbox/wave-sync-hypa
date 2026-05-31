# Contributing to wave_sync_hypa

This app bridges the **Wave** storefront with **Hypa** (ERPNext). Every change here affects how orders flow end-to-end, so we hold a high bar on code quality, traceability, and reviewability.

Read this document before opening your first PR.

---

## Ground rules

1. **One responsibility per function.** If a function does two things, split it. Name it after the one thing it does. Start each function with a one-line comment describing its intent.
2. **Rules live in data, not code.** Status mappings, routing, and fee mappings are stored in Wave Settings child tables and are editable from the Frappe Desk without a deployment. Do not hard-code new status strings or route decisions inside handler modules.
3. **The ERP is the source of truth** for inventory, pricing, and customer master data. We resolve Wave entities to ERP entities by stable keys (`sku`, Wave `_id`); we never overwrite ERP data with Wave data except through the explicit fields owned by this app (`wave_*` custom fields).
4. **Ack first, process later.** The inbound webhook authenticates, logs, enqueues, and returns `200` immediately. All business logic runs in a background worker.
5. **Every step is logged.** Every inbound webhook and every processing step writes a `Wave Sync Log` row keyed by a `correlation_id`. If it is not logged, it did not happen.
6. **Skills-aligned.** Follow the procedures in `github.com/GaturaN/frappe-agent-skills` (app, DocType, API, enterprise patterns, router, testing). Load the relevant SKILL.md at the start of a phase and respect its verification and failure-mode checklist.

---

## Branching

- `main` is always deployable.
- Feature work lives on branches named `feat/<slug>`; chores on `chore/<slug>`; fixes on `fix/<slug>`.
- Cut branches from `main`, not from other feature branches.
- Delete your branch after the PR merges.

---

## Issues

Open an issue **before** you write code. Use the templates under `.github/ISSUE_TEMPLATE/`.

- Every phase has a tracking issue (`Phase N — …`).
- Non-trivial items inside a phase get their own issues, linked as checklist entries from the phase issue.
- Label with `phase-N`, `type:feature|chore|bug`, `area:<customer|order|picklist|delivery|invoice|payment|promotions|infra>`.

---

## Commits

We use **[Conventional Commits](https://www.conventionalcommits.org/)** with detailed bodies. Small, logical commits per concern.

**Header** (≤ 72 chars):
```
<type>(<scope>): <subject>
```
Types: `feat`, `fix`, `chore`, `refactor`, `docs`, `test`, `perf`, `build`, `ci`.
Scopes are the module or subsystem: `settings`, `webhook`, `dispatcher`, `customer`, `order`, `picklist`, `delivery`, `invoice`, `payment`, `connectors/wave`, `logging`, `infra`.

**Body** (required for anything beyond a one-line docs/lint fix):
- **Why** the change exists — the business reason or the bug.
- **What** it does at a high level — not a line-by-line diff narration.
- **Trade-offs / alternatives considered**, if any.
- Skill references: `Skill: frappe-api-development`, `Skill: frappe-doctype-development`, etc.

**Footer:**
- `Refs #N` for partial progress on an issue.
- `Closes #N` when this commit completes the issue.

### Example

```
feat(settings): add Wave Settings Single with rule child tables

Wave ↔ Hypa routing, status mapping, and fee mapping must be editable by
admins without a deploy. This introduces the Wave Settings Single DocType
and four child tables (Wave Route Rule, Wave Status Rule Inbound,
Wave Status Rule Outbound, Wave Fee Mapping). No behaviour reads the
tables yet — that wiring lands in Phase 2.

Password fields are used for inbound_api_key and wave_api_key so the
secrets are encrypted at rest per Frappe conventions.

Skill: frappe-doctype-development
Refs #2
```

---

## Pull requests

- PR title = issue title.
- PR body must include: **Summary** (1–3 bullets), **Test plan** (what you ran), **Rule rows to configure** (if any admin setup is needed), **Screenshots / JSON samples** where helpful, and `Closes #N`.
- Do not merge your own PR without review — even on solo phases, the GitHub UI review step is the final checkpoint.
- CI must be green.

---

## Tests

The suite is split into two subpackages:

- **`tests/unit/`** — pure-mock tests (frappe.db, wave_client, log_step etc. all patched). No DocType writes. Fast — ~1–2s for the full ~370-test sweep. **This is the dev-loop runner.**
- **`tests/integration/`** — handler-driven tests that create real DocTypes (Customer, Sales Order, Wave Sync Log, Wave Settings, ...). Slow — minutes. Run on CI / before push.

```bash
# Dev loop (run on every change)
bench --site dev.bulkbox.cloud run-tests --app wave_sync_hypa \
    --module wave_sync_hypa.wave_sync_hypa.tests.unit

# Pre-push (full sweep)
bench --site dev.bulkbox.cloud run-tests --app wave_sync_hypa
```

A new test file goes into `unit/` unless it (a) calls a real handler entry point like `handle()` / `process_webhook()`, (b) calls `.insert()` / `.save()` / `frappe.delete_doc()`, or (c) calls `frappe.db.commit()`. Those go into `integration/`. After adding a file, add the matching `from .test_name import *  # noqa: F401, F403` line to the subpackage's `__init__.py` so `--module` discovery finds it.

## Verification checklist (run before pushing)

- [ ] `bench migrate` completes cleanly on `dev.bulkbox.cloud`.
- [ ] `bench --site dev.bulkbox.cloud run-tests --app wave_sync_hypa --module wave_sync_hypa.wave_sync_hypa.tests.unit` passes (fast).
- [ ] `bench --site dev.bulkbox.cloud run-tests --app wave_sync_hypa` passes (full sweep, slow — known `test_log_retention` `TooManyWritesError` at tail is pre-existing).
- [ ] `pre-commit run --all-files` passes (ruff, eslint, prettier, pyupgrade).
- [ ] No secrets, tokens, or `.env` contents committed.
- [ ] Any new rule keys are documented in the PR body so the admin knows what to configure.

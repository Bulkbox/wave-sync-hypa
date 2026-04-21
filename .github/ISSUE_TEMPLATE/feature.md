---
name: Feature
about: A new capability or integration point for wave_sync_hypa.
title: "feat(<scope>): <short subject>"
labels: ["type:feature"]
---

## Context / Why

<!-- The business reason. What triggers this work? What gap does it close? -->

## Scope

<!-- Bullet the concrete things in scope. Be specific about files/modules. -->

- [ ]
- [ ]
- [ ]

## Out of scope

<!-- Anything explicitly deferred. Link to follow-up issues if they exist. -->

## Skills to load

<!-- From github.com/GaturaN/frappe-agent-skills. Load the SKILL.md at start. -->

- `frappe-...`
- `frappe-...`

## Acceptance criteria

- [ ] Behaviour: …
- [ ] Admin-facing rule rows (if any) documented in the PR body.
- [ ] Tests added/updated under `wave_sync_hypa/tests/`.
- [ ] `bench migrate` clean.
- [ ] `bench --site dev.bulkbox.cloud run-tests --app wave_sync_hypa` green.
- [ ] Every processing step produces a Wave Sync Log entry with the correlation_id.

## Verification

<!-- Concrete commands/curl payloads a reviewer can run to verify. -->

## Open questions

<!-- Anything unresolved that may block or reshape the work. -->

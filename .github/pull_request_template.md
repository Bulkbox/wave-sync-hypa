<!-- PR title should match the issue title. -->

## Summary

<!-- 1–3 bullets describing what this PR delivers and the business outcome. -->

-
-

## Implementation notes

<!-- Any notable design decisions, trade-offs, or skill procedures followed. -->

- Skills applied:
  - `frappe-...`

## Admin rule rows to configure

<!-- List any Wave Settings child-table rows a deployer must add for this PR to take effect. Omit if none. -->

- None.

## Test plan

- [ ] `bench migrate` clean on `dev.bulkbox.cloud`.
- [ ] `bench --site dev.bulkbox.cloud run-tests --app wave_sync_hypa` green.
- [ ] Manual smoke test: <describe>
- [ ] Wave Sync Log rows inspected for correlation_id: <…>

## Screenshots / JSON samples

<!-- Attach any relevant payloads or screenshots. -->

## Linked issue

Closes #

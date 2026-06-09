"""Fresh-install seeding.

Frappe marks every patch in patches.txt as already-applied *without running
it* when an app is installed (installer.set_all_patches_as_completed, called
from install_app with set_as_patched=True). So our seed patches — both
override roles, the inbound route rules, the outbound status rules, the
payment-method mappings, the shipping-cost item — never execute on a new
site; they only ever ran on existing sites that predated the patch and later
hit `bench migrate`.

This hook closes that gap: on install it runs every post_model_sync patch's
execute() exactly once. All our seed patches are idempotent (existence-
guarded), so this is safe to run here AND to keep listed in patches.txt for
already-migrated sites. Enumerating via get_patches_from_app keeps this in
lock-step with patches.txt — any future seed patch is covered with no second
list to maintain. Each patch is isolated: one failing seed is logged and the
rest still run, so a compliance-app validator rejecting (say) the shipping
Item never aborts the whole install.
"""

from __future__ import annotations

import frappe
from frappe.modules.patch_handler import PatchType, get_patches_from_app

APP = "wave_sync_hypa"


def after_install() -> None:
	"""Run every post_model_sync seed patch once on a fresh install."""
	for patch in get_patches_from_app(APP, PatchType.post_model_sync):
		try:
			frappe.get_attr(f"{patch}.execute")()
		except Exception:
			frappe.log_error(
				title=f"wave_sync_hypa: install seed failed ({patch})",
				message=frappe.get_traceback(),
			)

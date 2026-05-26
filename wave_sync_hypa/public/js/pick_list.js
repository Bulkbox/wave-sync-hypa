// Wave Sync — Pick List form: manual batch-IDs push button + submit lockdown UX.
//
// On every refresh:
//   1. Add a "Send Batch IDs to Wave" button under a "Wave Sync" group.
//      Clicking it asks for confirmation and invokes the whitelisted endpoint,
//      which enqueues the PATCH worker for each linked Wave order (bypassing
//      the auto-fire kill-switch on Wave Settings).
//   2. When the ERP submit lockdown is on AND the user lacks the override
//      role, hide the primary Submit button and surface a dashboard hint.
//      Server-side guards in handlers/pick_list.py are the actual enforcement;
//      this is purely a UX nicety so unprivileged users don't see a button
//      they can't use.

const PICK_LIST_OVERRIDE_ROLE = "Pick List Wave Override";

frappe.ui.form.on("Pick List", {
	refresh(frm) {
		if (frm.is_new()) return;
		// Only on Draft — the picker work is over once the PL is submitted/cancelled.
		if (frm.doc.docstatus === 0) {
			frm.add_custom_button(
				__("Send Batch IDs to Wave"),
				() => _confirm_and_push(frm),
				__("Wave Sync")
			);
		}
		_maybe_hide_submit_when_locked_down(frm);
	},
});

function _maybe_hide_submit_when_locked_down(frm) {
	frappe.db
		.get_single_value("Wave Settings", "pick_list_erp_submit_lockdown_enabled")
		.then((lockdown) => {
			if (!parseInt(lockdown || 0)) return;
			const allowed =
				frappe.user.has_role(PICK_LIST_OVERRIDE_ROLE) ||
				frappe.user.has_role("System Manager");
			if (allowed) return;
			// Wait for Frappe to settle the primary button after refresh, then
			// hide it if it's the Submit action. Cancel lives in the menu and
			// is gated server-side; we accept its visibility for now.
			setTimeout(() => {
				const txt = frm.page.btn_primary?.text?.();
				if (txt === __("Submit")) {
					frm.page.btn_primary.hide();
					frm.dashboard.add_indicator(
						__("Submit handled by Wave"),
						"blue"
					);
				}
			}, 200);
		});
}

function _confirm_and_push(frm) {
	frappe.confirm(
		__(
			"Send batch numbers from {0} to Wave now? " +
			"One PATCH per linked Wave order. " +
			"Items without a batch number are excluded.",
			[frm.doc.name]
		),
		() => {
			frappe.call({
				method: "wave_sync_hypa.wave_sync_hypa.api.pick_list.push_batch_ids_now",
				args: { pick_list: frm.doc.name },
				freeze: true,
				freeze_message: __("Enqueueing batch-IDs push…"),
				callback({ message }) {
					if (message?.ok) {
						frappe.show_alert({
							message: __(
								"Batch-IDs push enqueued for {0} Wave order(s). " +
								"Correlation: {1}",
								[message.enqueued, message.correlation_id]
							),
							indicator: "green",
						});
					} else {
						frappe.show_alert({
							message: __("Push not enqueued: {0}", [
								message?.reason || __("unknown"),
							]),
							indicator: "orange",
						});
					}
				},
			});
		}
	);
}

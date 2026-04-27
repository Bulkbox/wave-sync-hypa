// Wave Settings: operator button to fan out a full default-warehouse stock resync.
//
// The label is deliberately "Wave"-prefixed so operators recognise the action
// belongs to the Wave integration. No business logic in JS — backend validates
// kill-switch / config / admin role and returns a batch_id that the operator
// can use to filter Wave Sync Log for this run.

frappe.ui.form.on("Wave Settings", {
	refresh(frm) {
		frm.add_custom_button(
			__("Sync ALL Items' Stock to Wave"),
			() => _confirm_full_resync(frm),
			__("Wave")
		);
	},
});

function _confirm_full_resync(frm) {
	const wh = frm.doc.default_warehouse || __("(not configured)");
	frappe.confirm(
		__(
			"Push current ERP stock to Wave for every active stock item in {0}? " +
			"In-flight per-item pushes will not be duplicated.",
			[wh]
		),
		() => _trigger_full_resync()
	);
}

function _trigger_full_resync() {
	frappe.call({
		method: "wave_sync_hypa.wave_sync_hypa.api.wave_settings.start_full_resync",
		freeze: true,
		freeze_message: __("Queueing full Wave stock resync..."),
		callback(r) {
			if (!r.message?.ok) return;
			frappe.show_alert({
				message: __("Wave stock resync queued. Batch: {0} ({1} item(s))", [
					r.message.batch_id,
					r.message.item_count_estimate,
				]),
				indicator: "green",
			});
		},
	});
}

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
	// Click-time gate, in the operator's order: integration on, then a warehouse
	// to sync from. The backend re-validates these (and the API config); this is
	// the earlier, clearer message instead of letting the server reject the call.
	if (!frm.doc.enabled) {
		frappe.msgprint({
			title: __("Wave integration is off"),
			message: __("Enable the Wave integration in Wave Settings before syncing stock."),
			indicator: "red",
		});
		return;
	}
	if (!frm.doc.default_warehouse) {
		frappe.msgprint({
			title: __("No default warehouse"),
			message: __("Set a Default Warehouse in Wave Settings. Stock is only ever synced from that warehouse."),
			indicator: "red",
		});
		return;
	}
	frappe.confirm(
		__(
			"Push current ERP stock to Wave for every active stock item in {0}? " +
			"In-flight per-item pushes will not be duplicated.",
			[frm.doc.default_warehouse]
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

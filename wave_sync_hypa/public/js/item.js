// Item form: per-SKU "Sync to Wave" button under the "Wave" group.
//
// The label is deliberately "Wave"-prefixed so operators recognise the action
// belongs to the Wave integration rather than ERPNext core. Clicking calls the
// same backend endpoint as the Wave Settings full-resync, with item_codes set
// to this single SKU. The backend rejects non-stock / disabled items, so we
// don't gate that client-side and risk drift.

frappe.ui.form.on("Item", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(
			__("Sync This Item's Stock to Wave"),
			() => _trigger_wave_sync_for([frm.doc.name]),
			__("Wave")
		);
	},
});

function _trigger_wave_sync_for(item_codes) {
	frappe.call({
		method: "wave_sync_hypa.wave_sync_hypa.api.wave_settings.start_full_resync",
		args: { item_codes: item_codes },
		freeze: true,
		freeze_message: __("Queueing Wave stock sync..."),
		callback(r) {
			if (!r.message?.ok) return;
			frappe.show_alert({
				message: __("Wave stock sync queued. Batch: {0} ({1} item(s))", [
					r.message.batch_id,
					r.message.item_count_estimate,
				]),
				indicator: "green",
			});
		},
	});
}

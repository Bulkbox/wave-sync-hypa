// Item list view: two bulk Wave actions in the Actions menu.
//
//   - "Sync Selected Items' Stock to Wave" — pushes only checked rows.
//   - "Sync ALL Items' Stock to Wave"      — pushes every active stock item
//                                            in the default warehouse, the
//                                            same fan-out as the Wave Settings
//                                            button. Convenience entry point.
//
// Both call api.wave_settings.start_full_resync. Selected mode passes the
// checked names; All mode passes no item_codes so the backend resyncs the
// entire eligible universe.

frappe.listview_settings["Item"] = {
	onload(listview) {
		listview.page.add_action_item(
			__("Sync Selected Items' Stock to Wave"),
			() => _wave_sync_selected(listview)
		);
		listview.page.add_action_item(
			__("Sync ALL Items' Stock to Wave"),
			() => _wave_sync_all()
		);
	},
};

function _wave_sync_selected(listview) {
	const rows = listview.get_checked_items() || [];
	if (!rows.length) {
		frappe.show_alert({
			message: __("Select at least one item, or use 'Sync ALL Items' Stock to Wave'."),
			indicator: "orange",
		});
		return;
	}
	const codes = rows.map((r) => r.name);
	frappe.confirm(
		__("Push current ERP stock to Wave for {0} selected item(s)?", [codes.length]),
		() => _call_wave_sync({ item_codes: codes })
	);
}

function _wave_sync_all() {
	frappe.confirm(
		__(
			"Push current ERP stock to Wave for EVERY active stock item in the default warehouse? " +
			"This may queue many background jobs."
		),
		() => _call_wave_sync({})
	);
}

function _call_wave_sync(args) {
	frappe.call({
		method: "wave_sync_hypa.wave_sync_hypa.api.wave_settings.start_full_resync",
		args: args,
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

// Wave Sync — Pick List form: manual batch-IDs push button.
//
// On every refresh, add a "Send Batch IDs to Wave" button under a
// "Wave Sync" group. Clicking it asks for confirmation and invokes the
// whitelisted endpoint, which enqueues the PATCH worker for each linked
// Wave order (bypassing the auto-fire kill-switch on Wave Settings).

frappe.ui.form.on("Pick List", {
	refresh(frm) {
		if (frm.is_new()) return;
		frm.add_custom_button(
			__("Send Batch IDs to Wave"),
			() => _confirm_and_push(frm),
			__("Wave Sync")
		);
	},
});

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

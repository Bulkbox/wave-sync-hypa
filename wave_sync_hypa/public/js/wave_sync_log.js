// Operator-initiated replay: re-process a Failed Wave webhook after the root
// cause is fixed. No auto-retry — this button is the only trigger.
frappe.ui.form.on("Wave Sync Log", {
	refresh(frm) {
		if (frm.doc.step !== "Failed") return;
		frm.add_custom_button(__("Replay Order"), () => {
			frappe.confirm(
				__(
					"Re-process this Wave webhook? Fix the root cause first. The updatedAt " +
						"duplicate check is bypassed; an existing Sales Order is still not duplicated."
				),
				() => {
					frappe.call({
						method: "wave_sync_hypa.wave_sync_hypa.api.replay.replay_order",
						args: { correlation_id: frm.doc.correlation_id },
						freeze: true,
						freeze_message: __("Replaying…"),
						callback: (r) => {
							if (r.message && r.message.ok) {
								frappe.show_alert({
									message: __("Replayed under correlation {0}. Check the new log rows.", [
										r.message.correlation_id,
									]),
									indicator: "green",
								});
							}
						},
					});
				}
			);
		});
	},
});

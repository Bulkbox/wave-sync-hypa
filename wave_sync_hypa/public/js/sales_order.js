// Manual-review banner for Sales Orders drafted by the Wave Sync integration.
// Shows a prominent orange intro whenever wave_manual_review_required is set,
// and exposes a Clear Review Flag button that calls the backend acknowledge endpoint.

frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		if (!frm.doc.wave_manual_review_required) {
			return;
		}
		_render_manual_review_banner(frm);
		_add_clear_review_button(frm);
	},
});

// Render the orange intro at the top of the Sales Order form.
function _render_manual_review_banner(frm) {
	const correlation = frm.doc.wave_correlation_id || "—";
	frm.set_intro(
		__(
			"Wave Sync — manual review required. One or more fees, tax templates, or other inputs could not be resolved when this order was drafted. See the Comments section below for the specific items and fix steps. Correlation: {0}",
			[correlation]
		),
		"orange"
	);
}

// Attach the Clear Review Flag button under a Wave Sync group in the form header.
function _add_clear_review_button(frm) {
	frm.add_custom_button(
		__("Clear Review Flag"),
		() => _confirm_clear_review(frm),
		__("Wave Sync")
	);
}

// Ask the operator to confirm, then call the acknowledge endpoint.
function _confirm_clear_review(frm) {
	frappe.confirm(
		__(
			"Clear the Wave manual-review flag on {0}? Only do this after addressing the issues listed in the Comments.",
			[frm.doc.name]
		),
		() => _call_clear_endpoint(frm)
	);
}

// Invoke the whitelisted backend method and refresh the form on success.
function _call_clear_endpoint(frm) {
	frappe.call({
		method: "wave_sync_hypa.wave_sync_hypa.api.sales_order.clear_manual_review_flag",
		args: { sales_order: frm.doc.name },
		callback() {
			frappe.show_alert({
				message: __("Review flag cleared."),
				indicator: "green",
			});
			frm.reload_doc();
		},
	});
}

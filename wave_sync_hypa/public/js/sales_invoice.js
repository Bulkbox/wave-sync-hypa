// Payment-review banner for Sales Invoices. When a prepaid invoice's iPay
// Payment Entry could not be created/attached automatically (no verified
// payment, an already-submitted PE that can't be modified, or a validator
// block), the server sets wave_payment_review_required + a reason. This
// surfaces it at the top of the form so the accounting team follows up.
// The flag clears automatically once the Payment Entry submits.

frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (frm.doc.wave_payment_review_required) {
			const reason =
				frm.doc.wave_payment_review_reason || __("the iPay Payment Entry could not be created");
			frm.set_intro(
				__("Wave Sync — payment review required. {0} Reconcile the iPay Payment Entry manually.", [
					frappe.utils.escape_html(reason),
				]),
				"red"
			);
		}
	},
});

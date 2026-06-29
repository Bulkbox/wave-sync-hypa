// "Wave Payment Entry" action for prepaid Wave Sales Invoices.
//
// For a prepaid order we own the iPay Payment Entry, so the iPay app's own
// buttons are removed and replaced by a single Wave action. The button shows
// only while the invoice has no submitted Payment Entry yet (wave_payment_entry
// unset) — once the PE is submitted the invoice is Paid and the action is no
// longer needed. Clicking it runs the same idempotent engine as the SI-submit
// worker, so it creates the PE on error or confirms an existing one.

frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (frm.doc.wave_payment_classification !== "prepaid") return;
		if (frm.doc.wave_payment_review_required) {
			_render_payment_review_banner(frm);
		}
		// Only change the form when the integration is live AND the feature is on.
		// While either is off, leave iPay's own buttons intact — removing them and
		// showing a Wave button that only reports "disabled" would be a regression.
		Promise.all([
			frappe.db.get_single_value("Wave Settings", "enabled"),
			frappe.db.get_single_value("Wave Settings", "ipay_auto_create_payment_entry"),
		]).then(([enabled, auto]) => {
			if (!parseInt(enabled || 0) || !parseInt(auto || 0)) return;
			_suppress_ipay_buttons(frm);
			if (frm.doc.docstatus === 1 && !frm.doc.wave_payment_entry) {
				_add_wave_payment_entry_button(frm);
			}
		});
	},
});

// iPay's sales_invoice.js (loaded at desk boot) adds these on a submitted,
// unpaid invoice. Our script loads lazily via doctype_js, so our refresh runs
// after iPay's and the buttons exist to remove. A deferred second pass survives
// any late re-add.
function _suppress_ipay_buttons(frm) {
	const drop = () => {
		frm.remove_custom_button(__("iPay Request"));
		frm.remove_custom_button(__("Copy Payment Link"));
	};
	drop();
	setTimeout(drop, 0);
}

// Red banner when the prepaid invoice's Payment Entry could not be auto-created
// / submitted (amount mismatch, unverified payment, or a conflict). The reason
// is carried on the doc; the operator resolves it via the Wave Payment Entry
// button (or by reconciling manually).
function _render_payment_review_banner(frm) {
	const reason = frm.doc.wave_payment_review_reason || __("the Payment Entry could not be created");
	frm.set_intro(
		__(
			"Wave Sync — payment review required. {0} Use 'Wave Payment Entry' above to create / confirm it, then reconcile.",
			[frappe.utils.escape_html(reason)]
		),
		"red"
	);
}

function _add_wave_payment_entry_button(frm) {
	frm.add_custom_button(
		__("Wave Payment Entry"),
		() => _call_ensure_pe_endpoint(frm),
		__("Wave")
	);
}

function _call_ensure_pe_endpoint(frm) {
	frappe.call({
		method: "wave_sync_hypa.wave_sync_hypa.api.sales_invoice.ensure_payment_entry",
		args: { sales_invoice: frm.doc.name },
		freeze: true,
		freeze_message: __("Creating / confirming the Wave Payment Entry..."),
		callback(r) {
			const result = r.message || {};
			frm.reload_doc();
			if (result.ok) {
				const pe = result.payment_entry || "—";
				const message = result.created
					? __("Payment Entry {0} created and submitted.", [pe])
					: __("Existing Payment Entry {0} confirmed.", [pe]);
				frappe.msgprint({
					title: __("Wave Payment Entry"),
					message,
					indicator: "green",
				});
				return;
			}
			frappe.msgprint({
				title: __("Wave Payment Entry not completed"),
				message: __("{0}<br><br>Correlation: <code>{1}</code>", [
					frappe.utils.escape_html(result.reason || __("Could not create the Payment Entry.")),
					result.correlation_id || "—",
				]),
				indicator: "orange",
			});
		},
	});
}

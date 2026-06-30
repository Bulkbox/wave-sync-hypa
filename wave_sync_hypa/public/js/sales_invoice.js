// "Wave Payment Entry" action for prepaid Wave Sales Invoices.
//
// For a prepaid order we own the iPay Payment Entry, so the iPay app's own
// buttons are removed and replaced by a single Wave action. Shown while the
// invoice is unpaid (outstanding > 0) — so it reappears if a payment is later
// unlinked/cancelled. The action runs the same idempotent engine as the
// SI-submit worker, creating the PE on error or confirming an existing one.

frappe.ui.form.on("Sales Invoice", {
	refresh(frm) {
		if (frm.doc.wave_payment_classification !== "prepaid") return;
		if (frm.doc.wave_payment_review_required) {
			_render_payment_review_banner(frm);
		}
		// Gate on bootinfo (server-computed for every desk session) — synchronous,
		// so it runs deterministically after iPay's boot-loaded refresh, and needs
		// no read permission on the restricted Wave Settings Single. While the
		// integration or the feature is off, leave iPay's own buttons intact.
		if (!frappe.boot.wave_integration_enabled || !frappe.boot.wave_ipay_auto_create_payment_entry) {
			return;
		}
		_suppress_ipay_buttons(frm);
		if (frm.doc.docstatus === 1 && frm.doc.outstanding_amount > 0) {
			_add_wave_payment_entry_button(frm);
		}
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
	// Our doctype_js refresh is guaranteed to run after iPay's app_include_js
	// refresh, so drop now; and re-drop on render_complete (fires once after every
	// full re-render — save, reload_doc — when Frappe rebuilds the toolbar and
	// iPay re-adds its buttons). Namespaced + .off() first so it never stacks.
	drop();
	frm.$wrapper.off("render_complete.wave_ipay").on("render_complete.wave_ipay", drop);
}

// Red intro banner carrying the doc's payment-review reason.
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

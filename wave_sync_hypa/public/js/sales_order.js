// Manual-review banner for Sales Orders drafted by the Wave Sync integration.
// The banner + Clear Review Flag button render ONLY while the SO is in Draft
// (docstatus === 0). Once the operator submits or cancels, they have made an
// explicit decision about the order and the banner becomes noise — submitted
// orders surface their state through the standard ERPNext lifecycle, not via
// a leftover review flag from intake time.

frappe.ui.form.on("Sales Order", {
	refresh(frm) {
		const is_draft = frm.doc.docstatus === 0;
		const is_submitted = frm.doc.docstatus === 1;
		if (is_draft && frm.doc.wave_manual_review_required) {
			_render_manual_review_banner(frm);
			_add_clear_review_button(frm);
		}
		if (frm.doc.wave_push_failure_required_review) {
			_render_push_failure_banner(frm);
		}
		if (frm.doc.wave_payment_review_required) {
			_render_payment_review_banner(frm);
		}
		// Verify iPay Payment shows on a prepaid Wave order — regardless of
		// docstatus while unpaid, so an accountant can re-check before or after
		// submitting — until iPay confirms the payment (wave_ipay_paid). Once
		// paid there is nothing left to verify, so it hides.
		if (frm.doc.wave_payment_classification === "prepaid" && !frm.doc.wave_ipay_paid) {
			_add_verify_ipay_button(frm);
		}
		// Manual status re-push is hidden once the SO is submitted: by then the
		// automatic doc-event sync (DN/SI/Pick List/cancel) owns status
		// transitions and the order is final, so the escape hatch is just risk.
		if (!frm.is_new() && is_draft && frm.doc.wave_order_id) {
			_add_resync_status_button(frm);
		}
		// "Mark Delivered on Wave" — operator override to push COMPLETED for
		// non-Shipday orders (pickup / walk-in / manual delivery). Submitted,
		// Wave-linked orders only.
		if (is_submitted && frm.doc.wave_order_id) {
			_add_mark_delivered_button(frm);
		}
		// "Push to Wave" / "Send Order to Wave" surfaces only for ERP-side
		// orders that haven't been pushed yet. Submitted-only so an operator
		// can't push a draft they might still be editing. Wave-webhook-originated
		// SOs are excluded regardless of wave_order_id state (they're Wave's
		// order already). The button label flips to "Send Order to Wave" when
		// a prior push failed (wave_push_failure_required_review=1) so the
		// operator clearly understands they're retrying after a fix.
		if (
			is_submitted
			&& !frm.doc.wave_order_id
			&& frm.doc.wave_origin !== "Wave Webhook"
		) {
			_add_push_to_wave_button(frm, frm.doc.wave_push_failure_required_review);
		}
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

// Attach the manual "Sync Order Status to Wave" button under the Wave group.
// Visible only on saved DRAFT Wave-sourced orders; once submitted, the automatic
// doc-event sync owns status transitions. Backend re-derives the rule mapping
// for the SO's current docstatus and enqueues a PUT.
function _add_resync_status_button(frm) {
	frm.add_custom_button(
		__("Sync Order Status to Wave"),
		() => _confirm_resync_status(frm),
		__("Wave")
	);
}

function _confirm_resync_status(frm) {
	frappe.confirm(
		__(
			"Push the current Wave status mapping for order {0} to Wave? Only do this if you suspect Wave is out of sync.",
			[frm.doc.name]
		),
		() => _call_resync_status_endpoint(frm)
	);
}

function _call_resync_status_endpoint(frm) {
	frappe.call({
		method: "wave_sync_hypa.wave_sync_hypa.api.sales_order_status.resync_order_status",
		args: { sales_order: frm.doc.name },
		freeze: true,
		freeze_message: __("Queueing Wave status push..."),
		callback(r) {
			if (!r.message?.ok) return;
			frappe.show_alert({
				message: __("Wave status push queued ({0}). Correlation: {1}", [
					r.message.event,
					r.message.correlation_id,
				]),
				indicator: "green",
			});
		},
	});
}

// "Mark Delivered on Wave" — operator-triggered COMPLETED push for non-Shipday
// orders (pickup, walk-in, manual delivery). Visible on any submitted Wave-linked
// SO. The server reuses the standard outbound dispatch and is idempotent on
// terminal orders.
function _add_mark_delivered_button(frm) {
	frm.add_custom_button(
		__("Mark Delivered on Wave"),
		() => _confirm_mark_delivered(frm),
		__("Wave")
	);
}

function _confirm_mark_delivered(frm) {
	frappe.confirm(
		__(
			"Push Wave status COMPLETED for order {0}? Use this for pickup / walk-in / manually-delivered orders that don't go through Shipday.",
			[frm.doc.name]
		),
		() => _call_mark_delivered_endpoint(frm)
	);
}

function _call_mark_delivered_endpoint(frm) {
	frappe.call({
		method: "wave_sync_hypa.wave_sync_hypa.api.sales_order.mark_completed_on_wave",
		args: { sales_order: frm.doc.name },
		freeze: true,
		freeze_message: __("Pushing COMPLETED to Wave..."),
		callback(r) {
			const result = r.message || {};
			if (result.ok) {
				frappe.show_alert({
					message: __("Wave COMPLETED queued for order {0}. Correlation: {1}", [
						result.wave_order_id,
						result.correlation_id,
					]),
					indicator: "green",
				});
				return;
			}
			frappe.msgprint({
				title: __("Could not mark delivered"),
				message: frappe.utils.escape_html(result.reason || __("Unknown error.")),
				indicator: "orange",
			});
		},
	});
}

// Red banner surfaced when the last ERP -> Wave push failed and the issue
// hasn't been resolved (wave_push_failure_required_review = 1). The
// Comments section carries the exact reason + remediation hints; this is
// just the visible "something needs attention" signal at the top of the form.
function _render_push_failure_banner(frm) {
	frm.set_intro(
		__(
			"Wave Sync — ERP → Wave push failed. See the Comments section below for the specific reason and remediation. After fixing, click 'Send Order to Wave' above to retry."
		),
		"red"
	);
}

// Red banner when a prepaid order's iPay payment could not be verified and
// has been flagged for the accounting team. The reason is carried on the doc
// (wave_payment_review_reason); accounting follows up via the iPay record.
function _render_payment_review_banner(frm) {
	const reason = frm.doc.wave_payment_review_reason || __("payment could not be verified");
	frm.set_intro(
		__(
			"Wave Sync — payment review required. {0} Use 'Verify iPay Payment' above to re-check, then reconcile the Payment Entry.",
			[frappe.utils.escape_html(reason)]
		),
		"red"
	);
}

// "Verify iPay Payment" — operator-triggered, synchronous lookup of the iPay
// payment for a prepaid order. Stamps the iPay fields, clears/sets the review
// flag, and shows the details in a message. Never a hard JS error: the server
// degrades gracefully when iPay is absent or unreachable.
function _add_verify_ipay_button(frm) {
	frm.add_custom_button(
		__("Verify iPay Payment"),
		() => _call_verify_ipay_endpoint(frm),
		__("Wave")
	);
}

function _call_verify_ipay_endpoint(frm) {
	frappe.call({
		method: "wave_sync_hypa.wave_sync_hypa.api.sales_order.verify_ipay_payment",
		args: { sales_order: frm.doc.name },
		freeze: true,
		freeze_message: __("Verifying payment with iPay..."),
		callback(r) {
			const result = r.message || {};
			frm.reload_doc();
			if (result.ok && result.paid) {
				frappe.msgprint({
					title: __("iPay payment confirmed"),
					message: _format_ipay_details(result.data || {}),
					indicator: "green",
				});
				return;
			}
			frappe.msgprint({
				title: __("iPay payment not confirmed"),
				message: __("{0}<br><br>Correlation: <code>{1}</code>", [
					frappe.utils.escape_html(result.reason || __("Could not verify the payment.")),
					result.correlation_id || "—",
				]),
				indicator: "orange",
			});
		},
	});
}

// Render the confirmed iPay record as a compact key/value table.
function _format_ipay_details(data) {
	const rows = [
		[__("Amount"), data.transaction_amount],
		[__("Transaction Code"), data.transaction_code],
		[__("Payment Mode"), data.payment_mode],
		[__("Paid At"), data.paid_at],
		[__("Payer"), [data.firstname, data.lastname].filter(Boolean).join(" ")],
		[__("Phone"), data.telephone],
	];
	const body = rows
		.filter(([, value]) => value)
		.map(
			([label, value]) =>
				`<tr><td><b>${frappe.utils.escape_html(label)}</b></td>` +
				`<td>${frappe.utils.escape_html(String(value))}</td></tr>`
		)
		.join("");
	return `<table class="table table-bordered"><tbody>${body}</tbody></table>`;
}

// "Push to Wave" / "Send Order to Wave" — operator-triggered offline-order
// push. Visible only on submitted, ERP-side SOs that haven't been pushed yet.
// Server-side checks the kill-switch + customer + product mappings; failures
// surface via banner + Comment + ToDo (never a hard JS error). Label flips
// to "Send Order to Wave" on retry so operators know they're acting after
// a prior failure.
function _add_push_to_wave_button(frm, is_retry) {
	const label = is_retry ? __("Send Order to Wave") : __("Push to Wave");
	frm.add_custom_button(
		label,
		() => _confirm_push_to_wave(frm, is_retry),
		__("Wave")
	);
}

function _confirm_push_to_wave(frm, is_retry) {
	const prompt = is_retry
		? __(
				"Retry pushing this Sales Order to Wave? Make sure the issue listed in the Comments has been fixed. Idempotent — safe to keep retrying."
			)
		: __(
				"Push this Sales Order to Wave? This creates a corresponding order in Wave's catalog so the picker app can fulfil it. Idempotent — safe to retry on failure."
			);
	frappe.confirm(prompt, () => _call_push_to_wave_endpoint(frm));
}

function _call_push_to_wave_endpoint(frm) {
	frappe.call({
		method: "wave_sync_hypa.wave_sync_hypa.api.sales_order.push_to_wave",
		args: { sales_order: frm.doc.name },
		freeze: true,
		freeze_message: __("Pushing to Wave..."),
		callback(r) {
			const result = r.message || {};
			if (result.ok) {
				frappe.show_alert({
					message: __("Pushed to Wave: wave_order_id {0} (friendlyId {1}).", [
						result.wave_order_id,
						result.wave_friendly_id || "—",
					]),
					indicator: "green",
				});
				frm.reload_doc();
				return;
			}
			// Failure: server already set the banner + Comment + ToDo;
			// reload so the operator sees the banner, then show the reason.
			frm.reload_doc();
			frappe.msgprint({
				title: __("Wave push failed"),
				message: __("{0}<br><br>Correlation: <code>{1}</code>", [
					frappe.utils.escape_html(result.reason || "Unknown error."),
					result.correlation_id || "—",
				]),
				indicator: "red",
			});
		},
	});
}

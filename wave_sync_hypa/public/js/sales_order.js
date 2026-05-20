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
		if (!frm.is_new() && frm.doc.wave_order_id) {
			_add_resync_status_button(frm);
		}
		// "Push to Wave" surfaces only for ERP-side orders that haven't been
		// pushed yet. Submitted-only so an operator can't push a draft they
		// might still be editing. Wave-webhook-originated SOs are excluded
		// regardless of wave_order_id state (they're Wave's order already).
		if (
			is_submitted
			&& !frm.doc.wave_order_id
			&& frm.doc.wave_origin !== "Wave Webhook"
		) {
			_add_push_to_wave_button(frm);
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
// Visible only on saved Wave-sourced orders; backend re-derives the rule mapping
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

// Red banner surfaced when the last ERP -> Wave push failed and the issue
// hasn't been resolved (wave_push_failure_required_review = 1). The
// Comments section carries the exact reason + remediation hints; this is
// just the visible "something needs attention" signal at the top of the form.
function _render_push_failure_banner(frm) {
	frm.set_intro(
		__(
			"Wave Sync — ERP → Wave push failed. See the Comments section below for the specific reason and remediation. After fixing, click 'Push to Wave' again to retry."
		),
		"red"
	);
}

// "Push to Wave" — operator-triggered offline-order push. Visible only on
// submitted, ERP-side SOs that haven't been pushed yet. Server-side checks
// the kill-switch + customer + product mappings; failures surface via banner
// + Comment + ToDo (never a hard JS error).
function _add_push_to_wave_button(frm) {
	frm.add_custom_button(
		__("Push to Wave"),
		() => _confirm_push_to_wave(frm),
		__("Wave")
	);
}

function _confirm_push_to_wave(frm) {
	frappe.confirm(
		__(
			"Push this Sales Order to Wave? This creates a corresponding order in Wave's catalog so the picker app can fulfil it. Idempotent — safe to retry on failure."
		),
		() => _call_push_to_wave_endpoint(frm)
	);
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

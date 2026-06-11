"""Inbound handler for Wave's ORDER.UPDATE webhook.

Wave fires ORDER.UPDATE for many reasons; this handler reacts only when
`pickerStatus == "COLLECTED"` — the moment the picker app marks the order
as picked. Every other ORDER.UPDATE is logged as Info and ignored, so
this module is safe to wire even though it only handles one transition.

Behaviour by Pick List docstatus:

  0 (Draft)      Update picked_qty + batch_no on matching rows, add picker
                 audit + anomaly Comments, propagate the customer comment,
                 then submit through the existing wave_inbound_pick_list_submit
                 flag seam. Submit is suppressed when any item is REPLACED —
                 operator review required.

  1 (Submitted)  Add a single Wave-pick summary Comment; do NOT touch state.
  2 (Cancelled)  Add a single Wave-pick summary Comment; do NOT touch state.

For all three branches the customer comment, if present in the payload, is
mirrored as a Frappe Comment on both the Pick List AND the linked Sales
Order with a "Customer now asks:" prefix. The intake-time SO.wave_comments
field is left untouched so the original-vs-updated history is preserved.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field

import frappe

from wave_sync_hypa.wave_sync_hypa.services import picker_identifier
from wave_sync_hypa.wave_sync_hypa.services.dispatcher import HANDLER_REGISTRY
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step

STEP_NOT_COLLECTED = "pick_list_inbound_picker_not_collected"
STEP_DISABLED = "pick_list_inbound_submit_disabled"
STEP_NO_PICK_LIST = "pick_list_inbound_no_pick_list"
STEP_DRAFT_SUBMITTED = "pick_list_inbound_pick_list_submitted"
STEP_SUBMIT_FAILED = "pick_list_inbound_submit_failed"
STEP_SAVE_FAILED = "pick_list_inbound_save_failed"
STEP_REPLACEMENT_PRESENT = "pick_list_inbound_replacement_present"
STEP_DISPARITY_PRESENT = "pick_list_inbound_disparity_present"
STEP_ANNOTATED_SUBMITTED = "pick_list_inbound_annotated_submitted_pl"
STEP_ANNOTATED_CANCELLED = "pick_list_inbound_annotated_cancelled_pl"

# Tolerance for float comparisons between Wave's reported picked qty and
# ERPNext's total row qty. Wave sends integers today but the field is float
# upstream and we want sub-unit disparities to surface, not round away.
QTY_TOLERANCE = 0.0001


@dataclass
class SkuVerdict:
	"""One SKU's reconciliation outcome: greedy allocations across rows + verdict label."""
	allocations: list[float] = field(default_factory=list)
	message: str | None = None
	is_disparity: bool = False
	# True when nothing was picked (REMOVED, or step-adjusted picked qty 0): the
	# caller zeroes the row qty as well as picked_qty, not just picked_qty.
	zero_qty: bool = False


@dataclass
class ReconciliationOutcome:
	"""Aggregate verdict across every SKU in the Wave picking index."""
	anomalies: list[str] = field(default_factory=list)
	has_disparity: bool = False


def handle(payload: dict, correlation_id: str) -> None:
	"""Entry point for ORDER.UPDATE webhooks; filter, fan out across linked Pick Lists."""
	if not _is_picking_complete_signal(payload):
		_log_ignored_not_collected(payload, correlation_id)
		return
	settings = frappe.get_cached_doc("Wave Settings")
	if not _inbound_submit_enabled(settings):
		_log_ignored_disabled(payload, correlation_id)
		return
	wave_order_id = (payload.get("_id") or "").strip()
	pick_list_names = _find_pick_lists_for_wave_order(wave_order_id)
	if not pick_list_names:
		_log_no_matching_pick_list(payload, correlation_id, wave_order_id)
		return
	index = _build_wave_picking_index(payload)
	for name in pick_list_names:
		_process_pick_list(name, payload, correlation_id, index, settings)


def _is_picking_complete_signal(payload: dict) -> bool:
	"""Return True only when Wave's pickerStatus reads COLLECTED."""
	return (payload.get("pickerStatus") or "").strip().upper() == "COLLECTED"


def _inbound_submit_enabled(settings) -> bool:
	"""Return True when the master kill-switch is on in Wave Settings."""
	return bool(settings.get("pick_list_inbound_submit_enabled"))


def _find_pick_lists_for_wave_order(wave_order_id: str) -> list[str]:
	"""Return Pick List names linked to this Wave order regardless of docstatus."""
	if not wave_order_id:
		return []
	return frappe.get_all(
		"Pick List",
		filters={"wave_order_id": wave_order_id},
		pluck="name",
	)


def _build_wave_picking_index(payload: dict) -> dict[str, dict]:
	"""Cross-reference products[] with picking.items[]; key by sku for locations[] matching."""
	products_by_id = {
		(p.get("productId") or ""): p
		for p in payload.get("products") or []
	}
	picking_items = (payload.get("picking") or {}).get("items") or []
	index: dict[str, dict] = {}
	for item in picking_items:
		product_id = item.get("productId") or ""
		product = products_by_id.get(product_id) or {}
		sku = (product.get("sku") or product.get("integratorId") or "").strip()
		if not sku:
			continue
		# Wave reports the picked qty in purchase *steps*; ERP rows are in actual
		# units (qty x stepToUom, since #156). Multiply so both sides compare in
		# the same units — otherwise a stepped pick reads as a false shortfall.
		step = float(product.get("stepToUom") or 1)
		index[sku] = {
			"quantity": float(item.get("quantity") or 0) * step,
			"batch_ids": list(product.get("batchIds") or []),
			"status": (item.get("status") or "").strip().upper(),
			"replacements": list(item.get("replacements") or []),
			"wave_product_id": product_id,
		}
	return index


def _process_pick_list(name: str, payload: dict, correlation_id: str, index: dict[str, dict], settings) -> None:
	"""Branch on docstatus; submitted/cancelled get annotations only."""
	doc = frappe.get_doc("Pick List", name)
	if doc.docstatus == 1:
		_annotate_terminal_pick_list(doc, payload, correlation_id, index, "submitted", STEP_ANNOTATED_SUBMITTED)
		return
	if doc.docstatus == 2:
		_annotate_terminal_pick_list(doc, payload, correlation_id, index, "cancelled", STEP_ANNOTATED_CANCELLED)
		return
	_process_draft_pick_list(doc, payload, correlation_id, index, settings)


def _annotate_terminal_pick_list(
	doc, payload: dict, correlation_id: str, index: dict[str, dict],
	state_label: str, step: str,
) -> None:
	"""Add a summary Comment on a submitted/cancelled Pick List without touching state."""
	doc.add_comment("Comment", _build_terminal_summary_html(payload, index, state_label))
	_maybe_propagate_customer_comment_to_pick_list(doc, payload)
	_maybe_propagate_customer_comment_to_sales_order(doc, payload)
	log_step(
		correlation_id, step, "Info",
		doc_type="ORDER", action="UPDATE",
		wave_id=payload.get("_id"),
		friendly_id=payload.get("friendlyId"),
		linked_doctype="Pick List",
		linked_docname=doc.name,
		response_body={"state": state_label, "items_reported": len(index)},
	)


def _process_draft_pick_list(doc, payload: dict, correlation_id: str, index: dict[str, dict], settings) -> None:
	"""Update + comment + submit a Draft Pick List; skip submit when any REPLACED item or qty/identifier disparity is present."""
	outcome = _apply_wave_picking_to_locations(doc, index, settings)
	_add_picker_audit_comment(doc, payload)
	for anomaly in outcome.anomalies:
		doc.add_comment("Comment", anomaly)
	_maybe_propagate_customer_comment_to_pick_list(doc, payload)
	_maybe_propagate_customer_comment_to_sales_order(doc, payload)

	if _has_replacements(index):
		_add_replacement_review_comments(doc, index)
		_save_without_submit(doc, payload, correlation_id)
		_log_replacement_present(doc, payload, correlation_id, index)
		return

	if outcome.has_disparity:
		_save_without_submit(doc, payload, correlation_id)
		_log_disparity_present(doc, payload, correlation_id, outcome.anomalies)
		return

	_submit_pick_list_with_inbound_flag(doc, payload, correlation_id)


def _apply_wave_picking_to_locations(doc, index: dict[str, dict], settings) -> ReconciliationOutcome:
	"""Allocate Wave's picked qty across batch rows per SKU; never rewrite row.batch_no.

	ERPNext already assigned each row a batch_no through its FEFO/FIFO
	allocator in set_item_locations — that is the authoritative source.
	Wave's identifier is treated only as a verification check, not as a
	value to write onto the rows. Per SKU:

	  * group rows by item_code, preserving order (= earliest batch first);
	  * fill row[0].picked_qty up to row[0].qty, carry remainder to row[1], ...;
	  * compute a verdict (clean / shortfall / overpick / identifier mismatch /
	    removed / missing) and add a Comment when the verdict isn't clean.

	Any non-clean SKU flips has_disparity, which suppresses auto-submit and
	leaves the doc in Draft for operator review.
	"""
	rows_by_sku: dict[str, list] = {}
	for row in doc.locations or []:
		sku = (row.item_code or "").strip()
		if not sku:
			continue
		rows_by_sku.setdefault(sku, []).append(row)

	outcome = ReconciliationOutcome()
	for sku, rows in rows_by_sku.items():
		wave = index.get(sku)
		if not wave:
			continue
		verdict = _reconcile_sku(rows, wave, settings)
		for row, picked in zip(rows, verdict.allocations):
			row.picked_qty = picked
			if verdict.zero_qty:
				row.qty = 0
		if verdict.message:
			outcome.anomalies.append(verdict.message)
		if verdict.is_disparity:
			outcome.has_disparity = True

	for sku in index:
		if sku not in rows_by_sku:
			outcome.anomalies.append(
				f"Wave reported a pick for SKU {sku} but this Pick List has no matching line."
			)
			outcome.has_disparity = True

	# Persist a one-field summary so the Pick List form shows a banner; the
	# per-anomaly Comments remain the detailed audit trail. Cleared when clean.
	doc.wave_picking_discrepancy = "\n".join(outcome.anomalies) if outcome.has_disparity else ""
	return outcome


def _reconcile_sku(rows: list, wave: dict, settings) -> SkuVerdict:
	"""Pure: one SKU's rows + Wave entry + settings -> SkuVerdict.

	Split out so it can be tested in isolation without a Pick List doc. The
	five branches map one-to-one to the five disparity kinds plus the clean
	case. REMOVED takes precedence over qty/identifier checks because a
	REMOVED item is unambiguously an operator concern regardless of the
	other dimensions.
	"""
	sku = (rows[0].item_code or "").strip()
	zero_allocations = [0.0] * len(rows)

	if wave["status"] == "REMOVED":
		return SkuVerdict(
			allocations=zero_allocations,
			message=(
				f"Wave reported SKU {sku} as REMOVED; qty and picked_qty set to 0 across "
				f"{len(rows)} batch row(s). Operator review required."
			),
			is_disparity=True,
			zero_qty=True,
		)

	wave_qty = float(wave["quantity"])
	total_expected = sum(float(getattr(r, "qty", 0) or 0) for r in rows)

	if wave_qty <= QTY_TOLERANCE:
		# Nothing picked — item not found / not picked. Zero the qty as well as
		# picked_qty so the line reflects that it isn't being fulfilled.
		return SkuVerdict(
			allocations=zero_allocations,
			message=(
				f"NOT PICKED: SKU {sku} — Wave reported picked qty 0 (not found / not picked); "
				f"qty and picked_qty set to 0 across {len(rows)} row(s). Operator review required."
			),
			is_disparity=True,
			zero_qty=True,
		)

	cap = min(wave_qty, total_expected)
	allocations = _greedy_fill(rows, cap)

	if wave_qty < total_expected - QTY_TOLERANCE:
		return SkuVerdict(
			allocations=allocations,
			message=(
				f"DISPARITY (shortfall): SKU {sku} — Wave reported picked_qty "
				f"{wave_qty}, ERP allocated {total_expected} across {len(rows)} "
				"batch row(s). Greedy-filled earliest first; remaining rows left "
				"at 0. Operator review required."
			),
			is_disparity=True,
		)

	if wave_qty > total_expected + QTY_TOLERANCE:
		return SkuVerdict(
			allocations=allocations,
			message=(
				f"DISPARITY (overpick): SKU {sku} — Wave reported picked_qty "
				f"{wave_qty}, ERP allocated only {total_expected} across "
				f"{len(rows)} batch row(s). Capped allocation to ERP qty. "
				"Operator review required."
			),
			is_disparity=True,
		)

	wave_id = _first_batch_identifier(wave)
	if wave_id and not picker_identifier.identifier_matches_inbound(wave_id, rows, settings):
		return SkuVerdict(
			allocations=allocations,
			message=(
				f"DISPARITY (identifier mismatch): SKU {sku} — Wave reported "
				f"identifier '{wave_id}' which does not match what was sent "
				"outbound under the current Picker Identifier Source. Quantities "
				"allocated greedily; operator review required."
			),
			is_disparity=True,
		)

	return SkuVerdict(allocations=allocations, message=None, is_disparity=False)


def _greedy_fill(rows: list, total: float) -> list[float]:
	"""Allocate `total` across rows: fill row[0].qty first, then row[1], ..."""
	remaining = total
	out: list[float] = []
	for row in rows:
		available = float(getattr(row, "qty", 0) or 0)
		allocate = min(available, max(remaining, 0.0))
		out.append(allocate)
		remaining -= allocate
	return out


def _first_batch_identifier(wave: dict) -> str:
	"""Return Wave's first batch identifier (or empty string when none reported)."""
	batches = wave.get("batch_ids") or []
	return (batches[0] or "").strip() if batches else ""


def _has_replacements(index: dict[str, dict]) -> bool:
	"""Return True when any item in the Wave picking index has a non-empty replacements[]."""
	return any(item["replacements"] for item in index.values())


def _add_replacement_review_comments(doc, index: dict[str, dict]) -> None:
	"""Add one Comment per replacement so the operator sees exactly what to reconcile."""
	for sku, wave in index.items():
		for replacement in wave["replacements"]:
			with_id = (replacement.get("withProductId") or "(unknown)").strip()
			qty = replacement.get("quantity") or 0
			doc.add_comment(
				"Comment",
				f"Wave picker substituted SKU {sku} with productId {with_id} "
				f"(qty {qty}). Auto-submit suppressed — operator review required.",
			)


def _add_picker_audit_comment(doc, payload: dict) -> None:
	"""Append a Comment recording who picked + when + Wave's correlation id."""
	picking = payload.get("picking") or {}
	user = picking.get("assignedToUser") or {}
	picker = " ".join(p for p in (user.get("firstName"), user.get("lastName")) if p) or "(unknown picker)"
	email = user.get("email") or ""
	completed_at = picking.get("completedAt") or ""
	doc.add_comment(
		"Comment",
		f"Picked by {picker} ({email}) on {completed_at} per Wave order "
		f"{payload.get('friendlyId') or payload.get('_id')}.",
	)


def _maybe_propagate_customer_comment_to_pick_list(doc, payload: dict) -> None:
	"""Mirror the customer note onto the Pick List with a 'now asks' prefix; no-op when empty."""
	text = (payload.get("comments") or "").strip()
	if not text:
		return
	doc.add_comment("Comment", f"Customer now asks: {text}")


def _maybe_propagate_customer_comment_to_sales_order(pick_list_doc, payload: dict) -> None:
	"""Mirror the customer note onto every Sales Order linked through Pick List.locations[]."""
	text = (payload.get("comments") or "").strip()
	if not text:
		return
	so_names = {
		(row.sales_order or "").strip()
		for row in pick_list_doc.locations or []
		if (row.sales_order or "").strip()
	}
	for so_name in so_names:
		so = frappe.get_doc("Sales Order", so_name)
		so.add_comment("Comment", f"Customer now asks: {text}")


@contextmanager
def _as_administrator_with_ignore_permissions():
	"""Webhook permission bypass: impersonate Administrator + set ignore_permissions.

	The inbound webhook session is Guest (allow_guest=True). ERPNext's
	PickList.before_save calls get_descendants_of("Warehouse", ...), which routes
	through frappe.get_list and consults the session user directly — the
	ignore_permissions flags do not reach it. Switching the session user is the
	only reliable way to let the Warehouse read pass without granting the
	webhook user a real role.
	"""
	previous_user = frappe.session.user
	previous_ignore = frappe.flags.get("ignore_permissions")
	frappe.flags.ignore_permissions = True
	try:
		frappe.set_user("Administrator")
		yield
	finally:
		frappe.set_user(previous_user)
		frappe.flags.ignore_permissions = previous_ignore


def _save_without_submit(doc, payload: dict, correlation_id: str) -> None:
	"""Persist line + comment edits on a Draft Pick List under the Administrator bypass.

	Save-only path covers the REPLACED-SKU and disparity branches; the PL stays
	Draft for operator review. Save failure is caught and logged so the inbound
	webhook is still acknowledged — operator can retry via the manual resync.
	"""
	try:
		with _as_administrator_with_ignore_permissions():
			doc.flags.ignore_permissions = True
			# Removed/not-found rows are zeroed (qty=0); qty is reqd, so bypass the
			# mandatory check on this review-only save path.
			doc.flags.ignore_mandatory = True
			doc.save()
	except Exception as exc:
		frappe.db.rollback()
		_log_save_failed(doc, payload, correlation_id, exc)


def _submit_pick_list_with_inbound_flag(doc, payload: dict, correlation_id: str) -> None:
	"""Save + submit a Draft Pick List under the Administrator bypass + inbound flag.

	The inbound flag short-circuits the human-only submit gate in
	handlers.pick_list.block_unprivileged_pick_list_submit. Administrator
	impersonation gets the nested Warehouse read past the Guest session.
	"""
	previous_inbound = frappe.flags.get("wave_inbound_pick_list_submit")
	frappe.flags.wave_inbound_pick_list_submit = True
	try:
		with _as_administrator_with_ignore_permissions():
			doc.flags.ignore_permissions = True
			doc.save()
			doc.submit()
			_log_submitted(doc, payload, correlation_id)
	except Exception as exc:
		frappe.db.rollback()
		_log_submit_failed(doc, payload, correlation_id, exc)
	finally:
		frappe.flags.wave_inbound_pick_list_submit = previous_inbound


def _build_terminal_summary_html(payload: dict, index: dict[str, dict], state_label: str) -> str:
	"""Render the single Comment body that documents Wave's pick state for an already-terminal PL."""
	rows = "".join(
		"<li><b>SKU {sku}</b>: qty {qty} status {status} batches [{batches}]</li>".format(
			sku=sku,
			qty=wave["quantity"],
			status=wave["status"] or "—",
			batches=", ".join(wave["batch_ids"]) or "—",
		)
		for sku, wave in index.items()
	)
	return (
		f"<div><b>Wave reported picking-complete</b> after this Pick List "
		f"was {state_label} in ERP. Pick List state is unchanged; Wave-side pick:</div>"
		f"<ul>{rows}</ul>"
		f"<div>Wave order: {_payload_id_summary(payload)}</div>"
	)


def _payload_id_summary(payload: dict) -> str:
	"""Short label combining friendlyId + Wave _id for Comment footers."""
	return f"{payload.get('friendlyId') or '—'} (ID: {payload.get('_id') or '—'})"


def _log_ignored_not_collected(payload: dict, correlation_id: str) -> None:
	"""Audit row for ORDER.UPDATE webhooks that aren't picking-complete signals."""
	log_step(
		correlation_id, STEP_NOT_COLLECTED, "Info",
		doc_type="ORDER", action="UPDATE",
		wave_id=payload.get("_id"),
		friendly_id=payload.get("friendlyId"),
		response_body={"pickerStatus": payload.get("pickerStatus")},
	)


def _log_ignored_disabled(payload: dict, correlation_id: str) -> None:
	"""Audit row for the kill-switch-off short circuit."""
	log_step(
		correlation_id, STEP_DISABLED, "Info",
		doc_type="ORDER", action="UPDATE",
		wave_id=payload.get("_id"),
		friendly_id=payload.get("friendlyId"),
		error_message="pick_list_inbound_submit_enabled is off; skipping.",
	)


def _log_no_matching_pick_list(payload: dict, correlation_id: str, wave_order_id: str) -> None:
	"""Audit row when no ERP Pick List references this Wave order."""
	log_step(
		correlation_id, STEP_NO_PICK_LIST, "Warning",
		doc_type="ORDER", action="UPDATE",
		wave_id=payload.get("_id"),
		friendly_id=payload.get("friendlyId"),
		error_message=(
			f"No ERP Pick List linked to Wave order {wave_order_id}. The Pick List may not "
			"have been created yet, or the wave_order_id link is missing."
		),
	)


def _log_submitted(doc, payload: dict, correlation_id: str) -> None:
	"""Success audit row for the Draft -> Submitted transition."""
	log_step(
		correlation_id, STEP_DRAFT_SUBMITTED, "Success",
		doc_type="ORDER", action="UPDATE",
		wave_id=payload.get("_id"),
		friendly_id=payload.get("friendlyId"),
		linked_doctype="Pick List",
		linked_docname=doc.name,
	)


def _log_submit_failed(doc, payload: dict, correlation_id: str, exc: Exception) -> None:
	"""Error audit row when Pick List submit raises."""
	log_step(
		correlation_id, STEP_SUBMIT_FAILED, "Error",
		doc_type="ORDER", action="UPDATE",
		wave_id=payload.get("_id"),
		friendly_id=payload.get("friendlyId"),
		linked_doctype="Pick List",
		linked_docname=doc.name,
		error_message=str(exc),
		stack_trace=frappe.get_traceback(),
	)


def _log_save_failed(doc, payload: dict, correlation_id: str, exc: Exception) -> None:
	"""Error audit row when the save-only path (REPLACED / disparity) raises."""
	log_step(
		correlation_id, STEP_SAVE_FAILED, "Error",
		doc_type="ORDER", action="UPDATE",
		wave_id=payload.get("_id"),
		friendly_id=payload.get("friendlyId"),
		linked_doctype="Pick List",
		linked_docname=doc.name,
		error_message=str(exc),
		stack_trace=frappe.get_traceback(),
	)


def _log_replacement_present(doc, payload: dict, correlation_id: str, index: dict[str, dict]) -> None:
	"""Warning audit row when REPLACEMENT suppresses auto-submit."""
	replacement_skus = [sku for sku, wave in index.items() if wave["replacements"]]
	log_step(
		correlation_id, STEP_REPLACEMENT_PRESENT, "Warning",
		doc_type="ORDER", action="UPDATE",
		wave_id=payload.get("_id"),
		friendly_id=payload.get("friendlyId"),
		linked_doctype="Pick List",
		linked_docname=doc.name,
		response_body={"replacement_skus": replacement_skus},
		error_message=(
			"Wave reported replacements; non-replaced lines updated + commented, "
			"but auto-submit was suppressed. Operator review required."
		),
	)


def _log_disparity_present(doc, payload: dict, correlation_id: str, anomalies: list[str]) -> None:
	"""Warning audit row when a qty / identifier / REMOVED disparity suppresses auto-submit."""
	log_step(
		correlation_id, STEP_DISPARITY_PRESENT, "Warning",
		doc_type="ORDER", action="UPDATE",
		wave_id=payload.get("_id"),
		friendly_id=payload.get("friendlyId"),
		linked_doctype="Pick List",
		linked_docname=doc.name,
		response_body={"anomalies": anomalies},
		error_message=(
			"Wave-vs-ERP reconciliation surfaced disparities; lines updated + "
			"commented, but auto-submit was suppressed. Operator review required."
		),
	)


HANDLER_REGISTRY["order_update"] = handle

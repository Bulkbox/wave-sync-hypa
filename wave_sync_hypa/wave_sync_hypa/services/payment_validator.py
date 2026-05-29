"""Payment Entry submit-time validator for Wave-sourced orders.

Wired in hooks.py:

  Payment Entry.before_submit -> handlers.payment_entry.validate_payment_before_submit
                                  -> payment_validator.validate_pe_before_submit

Behaviour, applied to every PE submission:

  * No Wave-sourced references at all -> pass through. Existing manual non-Wave
    PEs and the n8n unallocated-state "Ipay Unallocated" PE (which has
    references = []) are unaffected.
  * Mixed prepaid + COD references in one PE -> hard block. Reconciliation
    becomes ambiguous; the user has confirmed split-into-two is the answer.
  * Prepaid PE without a Sales Invoice reference -> hard block. Accounting
    invariant: a prepaid PE must settle a real Sales Invoice (not float as
    an unallocated draft). The accountant must add the SI to references[]
    before submitting.
  * Prepaid PE with amount divergence (>= FULL_PAYMENT_TOLERANCE) -> hard block.
  * Prepaid PE with MOP differing from the mapping table -> Warning, not block.
    The current n8n flow hardcodes `MPESA` on every iPay PE; we don't want to
    break it. Tightening to a hard block is a separate ticket once n8n is
    updated to consult the mapping.
  * COD PE whose mode_of_payment is classified `prepaid` (or unknown) -> block.

Override role `Wave Payment Validator Override` (or System Manager) bypasses
all hard-block branches with a Warning audit row recording user + which check
was overridden. Same pattern as Pick List Wave Override.

Pure-ish: walks pe.references[] in memory, dereferences via frappe.db.get_value
for the linked SO/SI Wave fields. Calls log_step for audit rows. Calls
frappe.throw on hard-block branches.
"""

from __future__ import annotations

import frappe

from wave_sync_hypa.wave_sync_hypa.services.correlation import new_correlation_id
from wave_sync_hypa.wave_sync_hypa.services.logger import log_step
from wave_sync_hypa.wave_sync_hypa.services.payment_status_resolver import FULL_PAYMENT_TOLERANCE
from wave_sync_hypa.wave_sync_hypa.services.pe_references import ref_field

OVERRIDE_ROLE = "Wave Payment Validator Override"

STEP_PASS_THROUGH_NO_WAVE = "payment_validator_passthrough_no_wave_refs"
STEP_BLOCKED_MIXED_CLASS = "payment_validator_blocked_mixed_classification"
STEP_BLOCKED_PREPAID_NO_SI = "payment_validator_blocked_prepaid_no_sales_invoice"
STEP_BLOCKED_PREPAID_AMOUNT = "payment_validator_blocked_prepaid_amount_mismatch"
STEP_BLOCKED_COD_MOP = "payment_validator_blocked_cod_mop_mismatch"
STEP_BLOCKED_COD_AMOUNT = "payment_validator_blocked_cod_zero_amount"
STEP_WARN_MOP_MISMATCH = "payment_validator_mop_mismatch"
STEP_OVERRIDDEN = "payment_validator_overridden"
STEP_VALIDATED = "payment_validator_passed"


def validate_pe_before_submit(pe_doc) -> None:
	"""Validate a PE about to be submitted; raise frappe.ValidationError on hard-block branches."""
	wave_refs = _collect_wave_references(pe_doc)
	if not wave_refs:
		# Non-Wave PE or unallocated iPay PE with no references at all.
		return

	correlation_id = pe_doc.get("wave_correlation_id") or new_correlation_id()

	classes = {r["classification"] for r in wave_refs if r["classification"]}
	if "prepaid" in classes and "cod" in classes:
		_throw_or_override(
			pe_doc, correlation_id,
			step=STEP_BLOCKED_MIXED_CLASS,
			message=(
				"Split this Payment Entry — it references both a prepaid Wave order "
				"and a cash-on-delivery Wave order. Reconciliation requires one PE per class."
			),
		)

	if "prepaid" in classes:
		_validate_prepaid(pe_doc, correlation_id, wave_refs)
	elif "cod" in classes:
		_validate_cod(pe_doc, correlation_id, wave_refs)
	# Mixed-but-overridden falls through to here; we still warn on MOP mismatch
	# but cannot meaningfully run amount equality without picking one class.

	log_step(
		correlation_id=correlation_id,
		step=STEP_VALIDATED,
		level="Info",
		doc_type=pe_doc.doctype,
		linked_doctype=pe_doc.doctype,
		linked_docname=pe_doc.name or "<new>",
		response_body={
			"classes": sorted(classes),
			"wave_orders": [r["wave_order_id"] for r in wave_refs],
		},
	)


def _validate_prepaid(pe_doc, correlation_id: str, wave_refs: list[dict]) -> None:
	"""Hard-block branches for a prepaid-class PE: missing-SI, amount divergence."""
	prepaid_refs = [r for r in wave_refs if r["classification"] == "prepaid"]
	wave_orders = {r["wave_order_id"] for r in prepaid_refs}

	# Each prepaid Wave order id must have at least one Sales Invoice in
	# references[] for this PE. The accountant cannot submit COMPLETED to Wave
	# until the order has been invoiced.
	missing_si: list[str] = []
	for wave_order_id in wave_orders:
		has_si = any(
			r["wave_order_id"] == wave_order_id and r["reference_doctype"] == "Sales Invoice"
			for r in prepaid_refs
		)
		if not has_si:
			friendly = next(
				(r["friendly_id"] for r in prepaid_refs if r["wave_order_id"] == wave_order_id),
				wave_order_id,
			)
			missing_si.append(friendly)
	if missing_si:
		_throw_or_override(
			pe_doc, correlation_id,
			step=STEP_BLOCKED_PREPAID_NO_SI,
			message=(
				"Prepaid Wave Payment Entries must reference a Sales Invoice. "
				f"Add the Sales Invoice for Wave order(s) {', '.join(missing_si)} "
				"to the references table before submitting."
			),
		)

	# Amount equality: pe.paid_amount must equal sum(wave_payment_hold +
	# wave_additional_payment_hold) over distinct prepaid Wave SOs.
	expected = 0.0
	seen_orders: set[str] = set()
	for r in prepaid_refs:
		if r["wave_order_id"] in seen_orders:
			continue
		seen_orders.add(r["wave_order_id"])
		expected += float(r["wave_payment_hold"] or 0.0)
		expected += float(r["wave_additional_payment_hold"] or 0.0)
	paid = float(pe_doc.get("paid_amount") or 0.0)
	if abs(paid - expected) >= FULL_PAYMENT_TOLERANCE:
		_throw_or_override(
			pe_doc, correlation_id,
			step=STEP_BLOCKED_PREPAID_AMOUNT,
			message=(
				f"Payment Entry paid_amount={paid:.2f} does not match the Wave-stamped "
				f"hold total {expected:.2f} (diff {paid - expected:+.2f}). "
				"Adjust the PE amount to match Wave's gateway settlement, or split refs."
			),
		)

	# MOP mismatch is a Warning, not a block. We compare against the FIRST
	# prepaid ref's mapped MOP — multi-SO prepaid PEs in production are rare
	# and would converge on the same MOP via Wave's payment_method_mappings.
	first = prepaid_refs[0]
	if first["expected_mop"] and pe_doc.get("mode_of_payment") != first["expected_mop"]:
		log_step(
			correlation_id=correlation_id,
			step=STEP_WARN_MOP_MISMATCH,
			level="Warning",
			doc_type=pe_doc.doctype,
			linked_doctype=pe_doc.doctype,
			linked_docname=pe_doc.name or "<new>",
			error_message=(
				f"Prepaid PE mode_of_payment={pe_doc.get('mode_of_payment')!r} differs from "
				f"Wave Payment Method Mapping expected={first['expected_mop']!r} "
				f"for paymentType={first['wave_payment_type']!r}. Submitting anyway."
			),
		)


def _validate_cod(pe_doc, correlation_id: str, wave_refs: list[dict]) -> None:
	"""Hard-block branches for a COD-class PE: MOP class mismatch, zero amount."""
	pe_mop = (pe_doc.get("mode_of_payment") or "").strip()
	mop_class = _classify_mode_of_payment(pe_mop)
	if mop_class != "cod":
		_throw_or_override(
			pe_doc, correlation_id,
			step=STEP_BLOCKED_COD_MOP,
			message=(
				f"This Payment Entry settles a cash-on-delivery Wave order; "
				f"Mode of Payment '{pe_mop}' is not classified as cod in the "
				"Wave Payment Method Mappings. Use a COD-classified MOP."
			),
		)

	if float(pe_doc.get("paid_amount") or 0.0) <= 0:
		_throw_or_override(
			pe_doc, correlation_id,
			step=STEP_BLOCKED_COD_AMOUNT,
			message="Payment Entry paid_amount must be greater than 0.",
		)


def _collect_wave_references(pe_doc) -> list[dict]:
	"""Walk pe.references[] and project each Wave-sourced row to a flat dict.

	Returns a list of {reference_doctype, reference_name, wave_order_id,
	classification, expected_mop, wave_payment_type, wave_payment_hold,
	wave_additional_payment_hold, friendly_id}.

	Non-Wave references and references without wave_order_id are skipped.
	"""
	out: list[dict] = []
	for ref in pe_doc.get("references") or []:
		ref_doctype = ref_field(ref, "reference_doctype")
		ref_name = ref_field(ref, "reference_name")
		if ref_doctype not in ("Sales Invoice", "Sales Order") or not ref_name:
			continue
		wave_order_id = (frappe.db.get_value(ref_doctype, ref_name, "wave_order_id") or "").strip()
		if not wave_order_id:
			continue
		# Sales Invoices don't carry the payment classification fields. Walk
		# the SI's referenced SO to read the Wave-stamped fields. SOs read
		# straight from themselves.
		so_for_metadata = ref_name if ref_doctype == "Sales Order" else _so_for_si(ref_name)
		if not so_for_metadata:
			# SI not linked to a Wave-stamped SO — treat as non-Wave for safety.
			continue
		so_fields = frappe.db.get_value(
			"Sales Order",
			so_for_metadata,
			[
				"wave_payment_classification",
				"wave_payment_type",
				"wave_payment_hold",
				"wave_additional_payment_hold",
				"wave_friendly_id",
			],
			as_dict=True,
		) or {}
		classification = (so_fields.get("wave_payment_classification") or "").strip()
		out.append({
			"reference_doctype": ref_doctype,
			"reference_name": ref_name,
			"wave_order_id": wave_order_id,
			"classification": classification,
			"wave_payment_type": (so_fields.get("wave_payment_type") or "").strip(),
			"wave_payment_hold": so_fields.get("wave_payment_hold"),
			"wave_additional_payment_hold": so_fields.get("wave_additional_payment_hold"),
			"friendly_id": (so_fields.get("wave_friendly_id") or "").strip() or wave_order_id,
			"expected_mop": _expected_mop_for_payment_type(
				(so_fields.get("wave_payment_type") or "").strip()
			),
		})
	return out


def _so_for_si(si_name: str) -> str | None:
	"""Return the Sales Order linked to this SI's items[] (first non-empty)."""
	rows = frappe.db.get_all(
		"Sales Invoice Item",
		filters={"parent": si_name},
		fields=["sales_order"],
		limit=10,
	)
	for row in rows:
		if row.get("sales_order"):
			return row["sales_order"]
	return None


def _expected_mop_for_payment_type(payment_type: str) -> str | None:
	"""Look up the mapping row for paymentType and return its mode_of_payment, or None."""
	if not payment_type:
		return None
	settings = frappe.get_cached_doc("Wave Settings")
	for row in settings.get("payment_method_mappings") or []:
		if (row.get("wave_payment_type") or "").strip() == payment_type:
			return (row.get("mode_of_payment") or "").strip() or None
	return None


def _classify_mode_of_payment(mop: str) -> str | None:
	"""Reverse-lookup: given a MOP, return its classification (prepaid|cod) or None."""
	if not mop:
		return None
	settings = frappe.get_cached_doc("Wave Settings")
	for row in settings.get("payment_method_mappings") or []:
		if (row.get("mode_of_payment") or "").strip() == mop:
			cls = (row.get("classification") or "").strip()
			return cls or None
	return None


def _throw_or_override(pe_doc, correlation_id: str, *, step: str, message: str) -> None:
	"""Raise frappe.ValidationError unless the user holds an override role.

	When overridden, write a Warning audit row capturing user + step + message
	and let submission continue. System Manager always passes (never lockout).
	"""
	user = frappe.session.user
	roles = set(frappe.get_roles(user))
	if OVERRIDE_ROLE in roles or "System Manager" in roles:
		log_step(
			correlation_id=correlation_id,
			step=STEP_OVERRIDDEN,
			level="Warning",
			doc_type=pe_doc.doctype,
			linked_doctype=pe_doc.doctype,
			linked_docname=pe_doc.name or "<new>",
			error_message=f"User '{user}' overrode validator step '{step}': {message}",
		)
		return

	log_step(
		correlation_id=correlation_id,
		step=step,
		level="Error",
		doc_type=pe_doc.doctype,
		linked_doctype=pe_doc.doctype,
		linked_docname=pe_doc.name or "<new>",
		error_message=message,
	)
	frappe.throw(msg=message, title="Payment Entry validator")

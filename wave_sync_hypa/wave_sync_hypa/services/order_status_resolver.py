"""Pure-function rule matcher for outbound Wave status pushes.

Reads the `outbound_status_rules` child table on Wave Settings, filters the
rows whose (doctype, event, optional condition) matches the firing ERP event,
and merges the matched rows' wave_status / wave_delivery_status into one
dict shaped like the partial PUT body Wave expects.

Stateless: no I/O beyond reading the settings doc the caller already has.
This makes it trivially testable and keeps the matching decision auditable
in a single log row at enqueue time.
"""

from __future__ import annotations


def resolve_outbound_payload(doc, event: str, settings) -> dict | None:
	"""Match outbound rules against (doc.doctype, event, optional condition); return merged payload or None."""
	rules = settings.get("outbound_status_rules") or []
	matched = [rule for rule in rules if _rule_matches(rule, doc, event)]
	if not matched:
		return None

	payload: dict = {}
	for rule in matched:
		if rule.get("wave_status"):
			payload["status"] = rule.get("wave_status")
		if rule.get("wave_delivery_status"):
			payload["deliveryStatus"] = rule.get("wave_delivery_status")
	return payload or None


def _rule_matches(rule, doc, event: str) -> bool:
	"""Return True when the rule is active and its predicates all hold for this doc + event."""
	if not _rule_get(rule, "enabled"):
		return False
	if _rule_get(rule, "erp_doctype") != doc.doctype:
		return False
	if _rule_get(rule, "erp_event") != event:
		return False
	return _condition_satisfied(rule, doc)


def _condition_satisfied(rule, doc) -> bool:
	"""When the rule has an erp_condition_field, require doc.<field> == erp_condition_value."""
	field = (_rule_get(rule, "erp_condition_field") or "").strip()
	value = (_rule_get(rule, "erp_condition_value") or "").strip()
	if not field:
		return True
	doc_value = _doc_field(doc, field)
	return doc_value == value


def _rule_get(rule, key: str):
	"""Read a field off a rule row whether it's a Frappe doc, a _dict, or a plain dict."""
	if isinstance(rule, dict):
		return rule.get(key)
	return getattr(rule, key, None)


def _doc_field(doc, field: str) -> str:
	"""Read doc.<field> as a string for equality compare; missing fields read as empty string."""
	if hasattr(doc, "get"):
		raw = doc.get(field)
	else:
		raw = getattr(doc, field, None)
	return "" if raw is None else str(raw)

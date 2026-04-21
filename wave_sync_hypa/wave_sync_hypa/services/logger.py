"""Structured logging for the Wave <> Hypa pipeline.

One function, one concern: `log_step` writes exactly one `Wave Sync Log` row.
Every handler, resolver, and service is expected to call it at every stage
boundary so the correlation_id chain stays unbroken end to end.

The function is deliberately forgiving: if writing a log row fails we fall
back to `frappe.log_error` rather than letting a logging bug abort the
business operation.
"""

from typing import Any

import frappe

from wave_sync_hypa.wave_sync_hypa.utils.json_tools import safe_dumps


def log_step(
	correlation_id: str,
	step: str,
	level: str = "Info",
	doc_type: str | None = None,
	action: str | None = None,
	wave_id: str | None = None,
	friendly_id: str | None = None,
	linked_doctype: str | None = None,
	linked_docname: str | None = None,
	request_body: Any = None,
	response_body: Any = None,
	error_message: str | None = None,
	stack_trace: str | None = None,
	duration_ms: int | None = None,
) -> str | None:
	"""Append one Wave Sync Log row and return its name; never raise."""
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Wave Sync Log",
				"correlation_id": correlation_id,
				"step": step,
				"level": level,
				"doc_type": doc_type,
				"action": action,
				"wave_id": wave_id,
				"friendly_id": friendly_id,
				"linked_doctype": linked_doctype,
				"linked_docname": linked_docname,
				"request_body": safe_dumps(request_body) if request_body is not None else None,
				"response_body": safe_dumps(response_body) if response_body is not None else None,
				"error_message": error_message,
				"stack_trace": stack_trace,
				"duration_ms": duration_ms,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(
			title="wave_sync_hypa: failed to write Wave Sync Log",
			message=frappe.get_traceback(),
		)
		return None

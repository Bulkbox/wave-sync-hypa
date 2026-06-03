"""Single source for the Wave outbound HTTP config.

Every service that calls Wave needs the same triplet — base_url, app_id,
api_key — and aborts with a "config incomplete" audit row when any piece is
missing. The stock pusher additionally needs store_id for its storeId-scoped
calls. This collapses the seven byte-identical copies that used to live one
per service.
"""

from __future__ import annotations


def resolve_outbound_config(settings, *, include_store_id: bool = False) -> dict | None:
	"""Return the outbound HTTP config, or None when any required piece is missing.

	Always requires base_url + app_id + api_key. When include_store_id is set
	(the stock pusher's storeId-scoped calls) store_id is required and returned too.
	"""
	base_url = (settings.get("wave_api_base_url") or "").strip()
	app_id = (settings.get("wave_app_id") or "").strip()
	api_key = settings.get_password("wave_api_key", raise_exception=False) or ""
	if not (base_url and app_id and api_key):
		return None
	config = {"base_url": base_url, "app_id": app_id, "api_key": api_key}
	if include_store_id:
		store_id = (settings.get("wave_store_id") or "").strip()
		if not store_id:
			return None
		config["store_id"] = store_id
	return config

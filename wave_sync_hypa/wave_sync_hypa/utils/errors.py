"""Domain error hierarchy for wave_sync_hypa.

Every exception the integration raises should be one of these classes, so
callers (logger, webhook handler, tests) can match on category instead of
re-parsing messages. The hierarchy is deliberately shallow.
"""


class WaveSyncError(Exception):
	"""Base for every domain error raised inside wave_sync_hypa."""

	pass


class WaveAuthError(WaveSyncError):
	"""Raised when an inbound webhook fails authentication."""

	pass


class WaveValidationError(WaveSyncError):
	"""Raised when a request or payload is malformed."""

	pass


class WaveResolutionError(WaveSyncError):
	"""Raised when a Wave identifier cannot be mapped to an ERP entity."""

	pass


class WaveOutboundError(WaveSyncError):
	"""Raised when an outbound call to the Wave API fails.

	Carries enough structured context for callers to branch on Wave's
	application-level error code (e.g. PRODUCT0006 for a stale product id,
	ORDER0049 for a forbidden state transition) without having to grep
	error message strings. wave_code is set when the Wave response body
	parsed as JSON and contained a `code` field; otherwise None.
	"""

	def __init__(
		self,
		message: str,
		*,
		http_status: int | None = None,
		wave_code: str | None = None,
		response_text: str | None = None,
	) -> None:
		super().__init__(message)
		self.http_status = http_status
		self.wave_code = wave_code
		self.response_text = response_text

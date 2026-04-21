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
	"""Raised when an outbound call to the Wave API fails."""

	pass

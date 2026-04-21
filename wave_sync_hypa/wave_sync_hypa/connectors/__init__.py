"""Outbound integrations.

One sub-package per external system. Each connector owns its HTTP client,
authentication, and retry policy; callers only see semantic functions
(e.g. `push_status(wave_order_id, wave_status)`).
"""

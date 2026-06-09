"""Unit tests for the fresh-install seeding hook.

after_install enumerates the app's post_model_sync patches and runs each
one's execute(). Two behaviours matter: every patch runs, and one failing
patch is isolated (logged, not re-raised) so install never aborts.

get_patches_from_app and frappe.get_attr are patched at the module boundary
so the test asserts the orchestration without touching real patches.
"""

from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from wave_sync_hypa import install

PATCHES = ["app.patches.v1_0.seed_alpha", "app.patches.v1_0.seed_beta"]


class TestAfterInstall(FrappeTestCase):
	def test_runs_every_post_model_sync_patch_execute(self):
		with (
			patch.object(install, "get_patches_from_app", return_value=PATCHES),
			patch.object(frappe, "get_attr") as mock_get_attr,
		):
			install.after_install()
		self.assertEqual(
			mock_get_attr.call_args_list,
			[call(f"{PATCHES[0]}.execute"), call(f"{PATCHES[1]}.execute")],
		)
		# Each resolved execute() callable was actually invoked.
		self.assertEqual(mock_get_attr.return_value.call_count, len(PATCHES))

	def test_one_failing_patch_is_logged_and_does_not_abort_the_rest(self):
		ok = MagicMock()

		def _resolve(path: str):
			if "seed_alpha" in path:
				return MagicMock(side_effect=RuntimeError("boom"))
			return ok

		with (
			patch.object(install, "get_patches_from_app", return_value=PATCHES),
			patch.object(frappe, "get_attr", side_effect=_resolve),
			patch.object(frappe, "log_error") as mock_log,
		):
			install.after_install()  # must not raise

		mock_log.assert_called_once()
		self.assertIn("seed_alpha", mock_log.call_args.kwargs["title"])
		# The patch after the failing one still ran.
		ok.assert_called_once()

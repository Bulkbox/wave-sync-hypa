"""Integration tests — handler-driven, DocType-touching, slow.

These tests exercise real DocType creation, savepoints, and full handler
pipelines. They take minutes to run, mutate dev-site state, and include
the known `test_log_retention` cross-test pollution issue. Run them as
part of the full app sweep (CI), not the dev loop:

    bench --site <site> run-tests --app wave_sync_hypa

To run just this subset explicitly:

    bench --site <site> run-tests --app wave_sync_hypa \
        --module wave_sync_hypa.wave_sync_hypa.tests.integration

Adding a new integration test file requires one line here, just like
tests/unit/__init__.py.
"""

from .test_customer_handler import *  # noqa: F401, F403
from .test_customer_handler_common_offline_skip import *  # noqa: F401, F403
from .test_dispatcher import *  # noqa: F401, F403
from .test_fee_resolver import *  # noqa: F401, F403
from .test_idempotency import *  # noqa: F401, F403
from .test_log_retention import *  # noqa: F401, F403
from .test_logger import *  # noqa: F401, F403
from .test_master_switch import *  # noqa: F401, F403
from .test_order_create import *  # noqa: F401, F403
from .test_prepaid_pe_lifecycle import *  # noqa: F401, F403
from .test_order_update_pick_list_collected import *  # noqa: F401, F403
from .test_processor import *  # noqa: F401, F403
from .test_sales_order_api import *  # noqa: F401, F403
from .test_wave_action import *  # noqa: F401, F403
from .test_wave_settings import *  # noqa: F401, F403
from .test_wave_status import *  # noqa: F401, F403
from .test_webhook import *  # noqa: F401, F403

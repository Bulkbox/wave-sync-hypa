"""Unit tests — pure-mock, fast, the standard dev-loop subset.

These tests patch at module boundaries (`frappe.db.get_value`, `wave_client`,
`log_step`, `frappe.enqueue`, etc.) and never touch real DocTypes. Run via:

    bench --site <site> run-tests --app wave_sync_hypa \
        --module wave_sync_hypa.wave_sync_hypa.tests.unit

Integration tests (handler-driven, DB-touching) live in tests/integration/
and run as part of the full app sweep:

    bench --site <site> run-tests --app wave_sync_hypa

The wildcard re-exports below let `--module ...tests.unit` discover every
test class via the unit package namespace. Adding a new unit test file
requires one line here.
"""

from .test_contact_resolver import *  # noqa: F401, F403
from .test_correlation import *  # noqa: F401, F403
from .test_credit_note_classifier import *  # noqa: F401, F403
from .test_customer_business_classification import *  # noqa: F401, F403
from .test_customer_create_side_effects import *  # noqa: F401, F403
from .test_customer_email_lookup import *  # noqa: F401, F403
from .test_delivery_note_autopopulate import *  # noqa: F401, F403
from .test_delivery_note_status_push import *  # noqa: F401, F403
from .test_delivery_type_classifier import *  # noqa: F401, F403
from .test_install_seeds import *  # noqa: F401, F403
from .test_integration_user import *  # noqa: F401, F403
from .test_intake_customer_resilience import *  # noqa: F401, F403
from .test_intake_review_notifier import *  # noqa: F401, F403
from .test_intake_soft_fail_items import *  # noqa: F401, F403
from .test_ipay_gateway import *  # noqa: F401, F403
from .test_ipay_payment_sync import *  # noqa: F401, F403
from .test_item_resolver import *  # noqa: F401, F403
from .test_json_tools import *  # noqa: F401, F403
from .test_money import *  # noqa: F401, F403
from .test_order_create_quantity import *  # noqa: F401, F403
from .test_order_status_push import *  # noqa: F401, F403
from .test_payment_entry_status_push import *  # noqa: F401, F403
from .test_payment_metadata_intake import *  # noqa: F401, F403
from .test_payment_review_flag import *  # noqa: F401, F403
from .test_payment_status_pusher import *  # noqa: F401, F403
from .test_payment_status_resolver import *  # noqa: F401, F403
from .test_payment_validator import *  # noqa: F401, F403
from .test_pick_list_api import *  # noqa: F401, F403
from .test_pick_list_batch_pusher import *  # noqa: F401, F403
from .test_pick_list_status_push import *  # noqa: F401, F403
from .test_pick_list_submit_gate import *  # noqa: F401, F403
from .test_picker_identifier import *  # noqa: F401, F403
from .test_product_resolver import *  # noqa: F401, F403
from .test_sales_invoice_status_push import *  # noqa: F401, F403
from .test_sales_order_amend import *  # noqa: F401, F403
from .test_sales_order_auto_push import *  # noqa: F401, F403
from .test_sales_order_ipay_api import *  # noqa: F401, F403
from .test_sales_order_ipay_handler import *  # noqa: F401, F403
from .test_replay import *  # noqa: F401, F403
from .test_sales_order_validation import *  # noqa: F401, F403
from .test_shipday_intercept import *  # noqa: F401, F403
from .test_shipping_back_calc import *  # noqa: F401, F403
from .test_stock_resync import *  # noqa: F401, F403
from .test_stock_sync import *  # noqa: F401, F403
from .test_wave_client_admin_endpoints import *  # noqa: F401, F403
from .test_wave_comments_stamping import *  # noqa: F401, F403
from .test_wave_customer_resolver import *  # noqa: F401, F403
from .test_wave_order_builder import *  # noqa: F401, F403
from .test_wave_order_creator import *  # noqa: F401, F403
from .test_wave_settings_preflight import *  # noqa: F401, F403

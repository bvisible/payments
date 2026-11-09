# //// Neoffice — added file (no upstream equivalent). The payment records of the retired apps
# //// (the Stripe terminal of `neopay_integration`, the first till TWINT integration) sat in
# //// tables without a DocType that nothing read: no refund, no reconciliation, no customer
# //// history. They are copied into Payment Intent, the old tables stay as they are
# //// (neoffice-maintenance#2390). Idempotent; the work is in payments.utils.legacy_payment_records.
"""Patch v15.14 — copy the legacy payment records into Payment Intent (#2390)."""

from payments.utils.legacy_payment_records import copy_legacy_payment_records


def execute() -> None:
	copy_legacy_payment_records()

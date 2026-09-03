# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Create the two channels a payment taken on the operator's phone can use —
``stripe_tap_to_pay`` and ``twint_mobile`` — on the sites that already exist.

Their specifications live in ``payments.setup.payment_channels`` with every other
shipped channel, where ``after_install`` provisions them on a fresh site (a patch never
runs there: ``bench install-app`` marks it as completed). Idempotent — safe to re-run.
Creates no provider and no binding: those are the merchant's choice, made once in
Mobile Payment Settings.
"""

from __future__ import annotations

from payments.setup.payment_channels import provision_payment_channels


def execute() -> None:
	provision_payment_channels(codes=("stripe_tap_to_pay", "twint_mobile"))

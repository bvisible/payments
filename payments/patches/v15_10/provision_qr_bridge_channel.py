# //// Neoffice — added file (no upstream equivalent). Creates the POS TWINT channel
# //// `qr_bridge` on the sites that lack it. Existing instances received the record from
# //// the retired twint_integration app; a fresh site never did (tracker #221). The
# //// specification lives in `payments.setup.payment_channels`, which `after_install`
# //// runs on a fresh site, where a patch never runs (`bench install-app` marks them all
# //// completed). Idempotent — an existing channel is never touched.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Create the POS TWINT channel ``qr_bridge`` on the sites that lack it.

Its specification lives in ``payments.setup.payment_channels`` with every other
shipped channel, where ``after_install`` provisions it on a fresh site. Idempotent —
safe to re-run; a channel that already exists is left exactly as it is.
"""

from __future__ import annotations

from payments.setup.payment_channels import provision_payment_channels


def execute() -> None:
	provision_payment_channels(codes=("qr_bridge",))

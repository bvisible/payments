# //// Neoffice — added file (no upstream equivalent). Resolves a (provider_name,
# //// channel_code) couple into a concrete PaymentDriverBase instance, by reading
# //// `Payment Provider` + `Payment Channel` + `Provider Channel Settings` and
# //// importing the resolved driver class. Upstream dispatches straight onto a
# //// `<psp>_settings` DocType, so it has no couple to resolve and no registry.
# //// Deliberately uncached: Frappe forks workers and credentials may rotate.
# //// Commits: e32ecf5 2026-05-13 "feat(payments): Phase 1 — unified payment driver layer (Provider × Channel × Driver)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""Driver registry.

Resolves a ``(provider_name, channel_code)`` pair into a concrete
:class:`PaymentDriverBase` instance, by:

1. Loading the ``Payment Provider`` record (credentials + driver class).
2. Loading the ``Payment Channel`` record (capabilities).
3. Loading the ``Provider Channel Settings`` record (per-binding config, optional
   driver class override).
4. Importing the resolved driver class and instantiating it.

Drivers are NOT cached at module level — Frappe runs in a forked worker model,
and credentials may rotate during the lifetime of a process. The cost of
instantiation is negligible compared with the HTTP roundtrip that follows.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

import frappe
from frappe import _

if TYPE_CHECKING:
	from payments.drivers.base import PaymentChannelBase, PaymentDriverBase, PaymentProviderBase


class DriverResolutionError(Exception):
	"""Raised when a driver cannot be resolved for a given provider/channel pair."""


def _import_class(dotted_path: str):
	"""Import a class from a dotted Python path. Raises DriverResolutionError on failure."""
	if not dotted_path or "." not in dotted_path:
		raise DriverResolutionError(f"Invalid dotted path: {dotted_path!r}")
	module_path, _, class_name = dotted_path.rpartition(".")
	try:
		module = import_module(module_path)
	except ImportError as exc:
		raise DriverResolutionError(f"Cannot import module {module_path!r}: {exc}") from exc
	try:
		return getattr(module, class_name)
	except AttributeError as exc:
		raise DriverResolutionError(f"Class {class_name!r} not found in {module_path!r}") from exc


def resolve_driver(provider_name: str, channel_code: str) -> "PaymentDriverBase":
	"""Resolve and instantiate the driver for ``(provider_name, channel_code)``.

	Lookup order for the driver class:
	1. ``Provider Channel Settings.driver_class`` (override)
	2. ``Payment Provider.driver_class`` (provider default)
	"""
	# 1. Provider record (must exist and be enabled).
	provider_doc = frappe.get_doc("Payment Provider", provider_name)
	if not provider_doc.enabled:
		raise DriverResolutionError(
			_("Payment Provider {0} is not enabled").format(provider_name)
		)

	# 2. Channel record (must exist).
	channel_doc = frappe.get_doc("Payment Channel", channel_code)

	# 3. Provider Channel Settings (must exist, must be enabled).
	binding_name = frappe.db.get_value(
		"Provider Channel Settings",
		{"provider": provider_name, "channel": channel_code},
		"name",
	)
	if not binding_name:
		raise DriverResolutionError(
			_("No Provider Channel Settings found for {0} × {1}").format(provider_name, channel_code)
		)
	binding_doc = frappe.get_doc("Provider Channel Settings", binding_name)
	if not binding_doc.enabled:
		raise DriverResolutionError(
			_("Provider Channel Settings {0} is disabled").format(binding_name)
		)

	# 4. Resolve driver class: binding override first, then provider default.
	driver_dotted_path = (binding_doc.driver_class or "").strip() or (
		provider_doc.driver_class or ""
	).strip()
	if not driver_dotted_path:
		raise DriverResolutionError(
			_("No driver class configured for {0} × {1}").format(provider_name, channel_code)
		)

	# 5. Instantiate.
	driver_cls = _import_class(driver_dotted_path)
	# We also need a Provider class + a Channel class. Convention: the driver class
	# is expected to know how to wire its provider/channel companions, OR we infer
	# them from the Provider record's metadata. For now, the simplest path: the
	# driver is responsible for accepting bare docs, and constructs its own
	# provider/channel wrappers internally if needed.
	#
	# To keep the contract clear, drivers receive the three docs and decide what
	# to do.
	return driver_cls.from_docs(provider_doc, channel_doc, binding_doc)


# //// Neoffice — maintenance#2296: the provider a payment goes to when none is named was the one saved last
# //// (`order_by="modified desc"`): opening and saving a test provider sent the next TWINT or Wallee payments of
# //// the shop to it. The rule of the Payrexx resolver, which learnt it first (drivers/payrexx/_common.py), now
# //// stands for them too.
def pick_provider(driver_class_like: str, channel: str | None = None) -> str | None:
	"""The enabled provider of one PSP family that acts when none is named, by a rule that does not move.

	1. Enabled providers whose driver class matches.
	2. When a channel is named, those with an enabled binding for it, if any has one: a site with no binding
	   rows still resolves, as it did.
	3. ``live`` before ``test``: a site that holds both takes real money.
	4. Then by name. Several still left is a guess, said in the Error Log once an hour.
	"""
	candidates = frappe.get_all(
		"Payment Provider",
		filters={"driver_class": ["like", driver_class_like], "enabled": 1},
		fields=["name", "mode"],
		order_by="name asc",
	)
	if not candidates:
		return None
	if channel:
		bound = set(
			frappe.get_all(
				"Provider Channel Settings",
				filters={
					"provider": ["in", [c.name for c in candidates]],
					"channel": channel,
					"enabled": 1,
				},
				pluck="provider",
			)
		)
		if bound:
			candidates = [c for c in candidates if c.name in bound]
	live = [c for c in candidates if (c.mode or "").lower() == "live"]
	pool = live or candidates
	if len(pool) > 1:
		from payments.api.webhook_common import log_once_an_hour

		log_once_an_hour(
			f"Payment provider is ambiguous: {driver_class_like}",
			f"channel={channel or '-'} candidates={[c.name for c in pool]}: {pool[0].name} is used. Disable the "
			"ones this site must not use, or name the provider.",
		)
	return pool[0].name

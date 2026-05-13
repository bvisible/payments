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

# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import base64

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class TwintBridgeSettings(Document):
	"""Per-merchant TWINT configuration.

	One record per TWINT merchant on the calling site. The P12 certificate FILE
	is never stored here — it lives on neoservice in
	``/home/neoffice/twint-certs/<merchant_uuid>.p12``. Only the unlock password
	is kept here (encrypted Frappe Password field).

	The ``p12_certificate`` Attach field is a one-shot upload vehicle: on save the
	bytes are pushed to neoservice via
	``neoffice_devops.api.twint.upload_certificate`` and the local copy is
	deleted, so the certificate never persists on this instance. Deleting (or
	rotating) the record removes the certificate from neoservice too.
	"""

	def validate(self):
		merchant = (self.merchant_uuid or "").strip()
		if not merchant:
			frappe.throw(_("Merchant UUID is required"))
		if "/" in merchant or ".." in merchant or "\\" in merchant:
			frappe.throw(
				_("Merchant UUID must not contain path separators (got: {0})").format(merchant)
			)
		self.merchant_uuid = merchant

	def on_update(self):
		# A freshly attached certificate triggers a push to neoservice. After a
		# successful deploy the field is cleared, so this is a no-op on later saves.
		if self.p12_certificate:
			self._deploy_certificate()

	def on_trash(self):
		# Best-effort: remove the merchant's certificate from neoservice so the
		# file lifecycle follows the record.
		self._delete_certificate_remote()

	# ------------------------------------------------------------------ #
	# Helpers
	# ------------------------------------------------------------------ #

	def _twint_provider(self):
		"""Resolve the TWINT Payment Provider (service URL + bridge auth token)."""
		from payments.drivers.twint.provider import TwintProvider

		name = self.linked_payment_provider or frappe.db.get_value(
			"Payment Provider",
			{"driver_class": ["like", "payments.drivers.twint.%"], "enabled": 1},
			"name",
			order_by="modified desc",
		)
		if not name:
			frappe.throw(
				_(
					"No enabled TWINT Payment Provider found. Create the TWINT Payment "
					"Provider first so the certificate can be pushed to neoservice."
				)
			)
		return TwintProvider(frappe.get_doc("Payment Provider", name))

	def _deploy_certificate(self):
		provider = self._twint_provider()
		file_doc = frappe.get_doc("File", {"file_url": self.p12_certificate})
		with open(file_doc.get_full_path(), "rb") as fh:
			raw = fh.read()
		payload = {
			"merchant_uuid": self.merchant_uuid,
			"content_base64": base64.b64encode(raw).decode(),
		}
		url = f"{provider.service_url}/api/method/neoffice_devops.api.twint.upload_certificate"
		try:
			resp = requests.post(url, headers=provider._auth_headers(), json=payload, timeout=30)
		except requests.exceptions.RequestException as exc:
			frappe.throw(_("Could not reach the certificate service on neoservice: {0}").format(exc))

		data = {}
		try:
			data = (resp.json() or {}).get("message") or {}
		except ValueError:
			pass
		if not (resp.ok and data.get("success")):
			frappe.throw(
				_("Certificate upload failed: {0}").format(
					data.get("error") or f"HTTP {resp.status_code}: {resp.text[:200]}"
				)
			)

		# Success: purge the local copy (cert lives only on neoservice) + stamp status.
		try:
			file_doc.delete(ignore_permissions=True)
		except Exception:  # noqa: BLE001 — purge is best-effort, never block a good deploy
			frappe.log_error(
				"TWINT local certificate purge failed", f"{self.name}: {frappe.get_traceback()}"
			)
		self.db_set("p12_certificate", None, update_modified=False)
		self.db_set("certificate_deployed", 1, update_modified=False)
		self.db_set("certificate_deployed_on", now_datetime(), update_modified=False)
		frappe.msgprint(
			_("Certificate deployed to neoservice ✓"), alert=True, indicator="green"
		)

	def _delete_certificate_remote(self):
		try:
			provider = self._twint_provider()
		except Exception:  # noqa: BLE001 — no provider configured: nothing to clean up
			return
		url = f"{provider.service_url}/api/method/neoffice_devops.api.twint.delete_certificate"
		try:
			requests.post(
				url,
				headers=provider._auth_headers(),
				json={"merchant_uuid": self.merchant_uuid},
				timeout=30,
			)
		except requests.exceptions.RequestException as exc:
			frappe.log_error("TWINT remote certificate delete failed", f"{self.name}: {exc!r}")

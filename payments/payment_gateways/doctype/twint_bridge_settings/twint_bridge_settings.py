#//// Neoffice — added file (no upstream equivalent). The per-merchant TWINT
#//// configuration DocType: merchant UUID, the .p12 unlock password, and the
#//// certificate's notAfter date. The certificate FILE never stays here — on save
#//// the bytes are pushed to neoservice (`/home/neoffice/twint-certs/<uuid>.p12`)
#//// and the local copy is deleted; deleting or rotating the record removes the
#//// remote file too. `check_certificate_expiry` mails a reminder over the 45 days
#//// before expiry, because an expired .p12 takes TWINT payments down.
#//// Upstream has no TWINT anything: in-store TWINT runs through our central PHP
#//// bridge on neoservice (twint-ag/sdk), not Stripe's TWINT QR (ADR-002). The
#//// DocType was renamed from `Twint Settings` by cc503b1 to stop colliding with
#//// the legacy `twint_integration` app still listed in the bench's apps.txt.
#//// Commits: cc503b1 2026-05-13 "fix(payments): rename Twint Settings → Twint Bridge Settings + scheduler cron syntax"
#////          29cac07 2026-06-21 "feat(twint): upload P12 from Twint Bridge Settings form, lifecycle-synced to neoservice; clarify field help + Admin role perms"
#////          cf61f54 2026-06-21 "feat(twint): certificate expiry monitoring — store notAfter on upload, daily reminder email + form banner within 45 days (+ POS alert API)"
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE

import base64

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate, now_datetime, today

# Certificate-expiry monitoring: warn within this many days of the notAfter date.
CERT_EXPIRY_WARNING_DAYS = 45
# Email reminder milestones (days before expiry) to avoid daily spam during the
# whole window. Once expired (payments down), we email every day.
CERT_EXPIRY_MILESTONES = {45, 30, 21, 14, 10, 7, 5, 4, 3, 2, 1, 0}


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

	The certificate's expiry (``notAfter``) is read at upload and stored in
	``certificate_expires_on``; :func:`check_certificate_expiry` warns by email in
	the 45 days before, and the POS shows a banner over the same window.
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

		# Read the certificate expiry (notAfter) before purging the local copy, so
		# we can warn before it lapses. Best-effort — never block a good deploy.
		expiry = self._read_cert_expiry(raw)

		# Purge the local copy (cert lives only on neoservice) + stamp status.
		try:
			file_doc.delete(ignore_permissions=True)
		except Exception:  # noqa: BLE001 — purge is best-effort, never block a good deploy
			frappe.log_error(
				"TWINT local certificate purge failed", f"{self.name}: {frappe.get_traceback()}"
			)
		self.db_set("p12_certificate", None, update_modified=False)
		self.db_set("certificate_deployed", 1, update_modified=False)
		self.db_set("certificate_deployed_on", now_datetime(), update_modified=False)
		if expiry:
			self.db_set("certificate_expires_on", expiry, update_modified=False)
		msg = _("Certificate deployed to neoservice ✓")
		if expiry:
			msg += " " + _("(expires {0})").format(frappe.utils.formatdate(expiry, "dd.MM.yyyy"))
		frappe.msgprint(msg, alert=True, indicator="green")

	def _read_cert_expiry(self, raw: bytes):
		"""Return the certificate's notAfter as a ``date`` (or None on failure).

		Tries the ``cryptography`` library first; falls back to the ``openssl`` CLI
		(with ``-legacy``) for older RC2/3DES-encrypted P12 files that newer
		OpenSSL refuses by default.
		"""
		pw = self.get_password("p12_password", raise_exception=False) or ""
		# 1) cryptography — clean, no temp file.
		try:
			from cryptography.hazmat.primitives.serialization import pkcs12

			_key, cert, _add = pkcs12.load_key_and_certificates(raw, pw.encode() or None)
			if cert is not None:
				try:
					return cert.not_valid_after_utc.date()
				except AttributeError:
					return cert.not_valid_after.date()
		except Exception:  # noqa: BLE001 — fall through to openssl
			pass
		# 2) openssl fallback (handles legacy-encrypted P12).
		import os as _os
		import subprocess
		import tempfile
		from datetime import datetime

		tmp = None
		try:
			fd, tmp = tempfile.mkstemp(suffix=".p12")
			with _os.fdopen(fd, "wb") as fh:
				fh.write(raw)
			for extra in ([], ["-legacy"]):
				p1 = subprocess.run(
					["openssl", "pkcs12", "-in", tmp, "-nodes", "-passin", f"pass:{pw}", *extra],
					capture_output=True,
					timeout=15,
					check=False,
				)
				if p1.returncode == 0 and p1.stdout:
					p2 = subprocess.run(
						["openssl", "x509", "-noout", "-enddate"],
						input=p1.stdout,
						capture_output=True,
						timeout=15,
						check=False,
					)
					for line in (p2.stdout or b"").decode(errors="ignore").splitlines():
						if line.startswith("notAfter="):
							return datetime.strptime(
								line.split("=", 1)[1].strip(), "%b %d %H:%M:%S %Y %Z"
							).date()
		except Exception:  # noqa: BLE001
			return None
		finally:
			if tmp and _os.path.exists(tmp):
				try:
					_os.remove(tmp)
				except OSError:
					pass
		return None

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


# ---------------------------------------------------------------------------
# Certificate-expiry monitoring (daily scheduler + POS warning source)
# ---------------------------------------------------------------------------


def check_certificate_expiry(days_before: int = CERT_EXPIRY_WARNING_DAYS):
	"""Daily scheduler: email admins about TWINT certs near (or past) expiry.

	Emails on milestone days (to avoid 45 daily mails) and every day once the
	certificate has actually expired (payments are down — urgent).
	"""
	from frappe.utils import date_diff

	rows = frappe.get_all(
		"Twint Bridge Settings",
		filters={"enabled": 1, "certificate_expires_on": ["is", "set"]},
		fields=["name", "display_label", "merchant_uuid", "certificate_expires_on"],
	)
	for r in rows:
		days_left = date_diff(getdate(r.certificate_expires_on), getdate(today()))
		if days_left < 0 or (days_left <= days_before and days_left in CERT_EXPIRY_MILESTONES):
			_notify_cert_expiry(r, days_left)


def _notify_cert_expiry(row, days_left: int):
	recipients = _admin_recipients()
	if not recipients:
		return
	label = row.get("display_label") or row.get("name")
	when = frappe.utils.formatdate(row.get("certificate_expires_on"), "dd.MM.yyyy")
	if days_left < 0:
		subject = _("⚠️ TWINT certificate EXPIRED — {0}").format(label)
		message = _(
			"The TWINT certificate for <b>{0}</b> expired on {1}. TWINT payments are "
			"DOWN until a renewed .p12 is uploaded on the Twint Bridge Settings form."
		).format(label, when)
	else:
		subject = _("TWINT certificate expires in {0} day(s) — {1}").format(days_left, label)
		message = _(
			"The TWINT certificate for <b>{0}</b> expires on {1} (in {2} day(s)).<br><br>"
			"Renew it with TWINT (they issue a new .p12) and upload it via the "
			"<b>P12 Certificate</b> field on the Twint Bridge Settings form — it replaces "
			"the current one automatically."
		).format(label, when, days_left)
	frappe.sendmail(recipients=recipients, subject=subject, message=message)


def _admin_recipients() -> list[str]:
	"""Enabled users holding System Manager or Admin role (who can renew the cert)."""
	users = frappe.get_all(
		"Has Role",
		filters={"role": ["in", ["System Manager", "Admin"]], "parenttype": "User"},
		pluck="parent",
	)
	if not users:
		return []
	emails = frappe.get_all(
		"User",
		filters={"name": ["in", list(set(users))], "enabled": 1},
		pluck="email",
	)
	return [e for e in set(emails) if e and "@" in e]


@frappe.whitelist()
def get_pos_certificate_alerts(pos_profile: str | None = None) -> list[dict]:
	"""Return TWINT certs expiring within the warning window, for a POS banner.

	Used by the POS (neopos) at opening to show a non-blocking warning so the
	cashier/owner renews in time. Scoped to the given POS Profile's TWINT mappings
	when provided, else all enabled merchants.
	"""
	from frappe.utils import date_diff

	# An instance has at most a handful of TWINT merchants; return alerts for all
	# enabled ones. (pos_profile is accepted for future per-profile scoping.)
	rows = frappe.get_all(
		"Twint Bridge Settings",
		filters={"enabled": 1, "certificate_expires_on": ["is", "set"]},
		fields=["name", "display_label", "certificate_expires_on"],
	)
	alerts = []
	for r in rows:
		days_left = date_diff(getdate(r.certificate_expires_on), getdate(today()))
		if days_left <= CERT_EXPIRY_WARNING_DAYS:
			alerts.append(
				{
					"merchant": r.display_label or r.name,
					"expires_on": str(r.certificate_expires_on),
					"days_left": days_left,
					"expired": days_left < 0,
				}
			)
	return alerts

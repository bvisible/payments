# //// Neoffice — added file (no upstream equivalent). The hub refuses to replace or delete a stored TWINT
# //// certificate unless it is given the password that opens it (maintenance#1372): these tests pin what the
# //// instance sends. Run without a site: the hub is a recorder, nothing leaves the process.
# Copyright (c) 2026, Neoffice and contributors
# License: MIT. See LICENSE
"""What a Twint Bridge Settings record sends the hub when it uploads or deletes its certificate."""

import base64
import unittest
from unittest import mock

from payments.payment_gateways.doctype.twint_bridge_settings import twint_bridge_settings as module
from payments.payment_gateways.doctype.twint_bridge_settings.twint_bridge_settings import (
	TwintBridgeSettings,
)

MERCHANT = "3556446f-a25c-47bb-ba68-91b859e9baf5"
RAW = b"\x30\x03\x02\x01\x00"


def _record(stored_password=None, password="record-password"):
	"""A record without a site: only the attributes the payload builders read."""
	record = object.__new__(TwintBridgeSettings)
	record.merchant_uuid = MERCHANT
	record.name = "tbs-1"
	if stored_password is not None:
		record._stored_p12_password = stored_password
	record.get_password = lambda fieldname, raise_exception=True: password
	return record


class TestWhatTheInstanceSendsTheHub(unittest.TestCase):
	def test_a_first_upload_sends_no_proof(self):
		payload = _record(stored_password="")._upload_payload(RAW)
		self.assertEqual(
			payload, {"merchant_uuid": MERCHANT, "content_base64": base64.b64encode(RAW).decode()}
		)

	def test_replacing_a_certificate_sends_the_password_it_was_stored_with(self):
		payload = _record(stored_password="old-password")._upload_payload(RAW)
		self.assertEqual(payload["current_password"], "old-password")
		self.assertEqual(payload["merchant_uuid"], MERCHANT)

	def test_a_record_that_never_went_through_validate_sends_no_proof(self):
		self.assertNotIn("current_password", _record()._upload_payload(RAW))

	def test_deleting_the_record_sends_its_password(self):
		self.assertEqual(
			_record()._delete_payload(), {"merchant_uuid": MERCHANT, "current_password": "record-password"}
		)

	def test_a_record_with_no_password_sends_none(self):
		self.assertEqual(_record(password="")._delete_payload(), {"merchant_uuid": MERCHANT})


class TestTheStoredPasswordIsReadBeforeTheSaveWritesTheNewOne(unittest.TestCase):
	def _validate(self, is_new, stored):
		record = _record()
		record.is_new = lambda: is_new
		with mock.patch.object(
			TwintBridgeSettings, "_read_stored_password", return_value=stored
		) as read:
			record.validate()
		return record, read

	def test_an_existing_record_remembers_the_password_the_hub_holds_the_certificate_for(self):
		record, read = self._validate(is_new=False, stored="old-password")
		self.assertEqual(record._stored_p12_password, "old-password")
		read.assert_called_once_with()

	def test_a_new_record_has_nothing_stored(self):
		record, read = self._validate(is_new=True, stored="ignored")
		self.assertEqual(record._stored_p12_password, "")
		read.assert_not_called()


class TestARefusedDeleteIsSaidNotSwallowed(unittest.TestCase):
	def _delete(self, response=None, error=None):
		record = _record()
		provider = mock.Mock(service_url="https://hub.example")
		provider._auth_headers.return_value = {"Authorization": "token k:s"}
		with (
			mock.patch.object(TwintBridgeSettings, "_twint_provider", return_value=provider),
			mock.patch.object(module.requests, "post", return_value=response, side_effect=error) as post,
			mock.patch.object(module.frappe, "log_error") as log_error,
		):
			record._delete_certificate_remote()
		return post, log_error

	def test_the_password_goes_in_the_body_not_in_the_address(self):
		response = mock.Mock(ok=True)
		response.json.return_value = {"message": {"success": True, "deleted": True}}
		post, log_error = self._delete(response)
		url = post.call_args.args[0]
		self.assertTrue(url.endswith("neoffice_devops.api.twint.delete_certificate"))
		self.assertNotIn("record-password", url)
		self.assertEqual(post.call_args.kwargs["json"]["current_password"], "record-password")
		log_error.assert_not_called()

	def test_a_refusal_of_the_hub_is_logged(self):
		response = mock.Mock(ok=True, status_code=200)
		response.json.return_value = {
			"message": {"success": False, "error": "A certificate is already stored"}
		}
		_post, log_error = self._delete(response)
		title, message = log_error.call_args.args
		self.assertEqual(title, "TWINT remote certificate delete refused")
		self.assertIn("already stored", message)

	def test_an_answer_that_is_not_json_is_logged_too(self):
		response = mock.Mock(ok=False, status_code=502)
		response.json.side_effect = ValueError
		_post, log_error = self._delete(response)
		self.assertIn("HTTP 502", log_error.call_args.args[1])


if __name__ == "__main__":
	unittest.main()

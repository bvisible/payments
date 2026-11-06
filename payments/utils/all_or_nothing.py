# //// Neoffice — added file (no upstream equivalent). A booking that fails half-way is undone, never committed.
"""What a payment books is booked whole, or not at all.

The code that books a captured payment (a Payment Entry, an invoice row, a Payment Request set as paid) runs where a
failure must not reach the buyer: a gateway's success page, the doc_event of a Payment Intent. Those callers catch the
error and log it. Catching is not undoing: what the booking wrote before it failed (a Payment Entry submitted with no
GL entry, a request set as paid with no invoice) stayed in the transaction, and the request or the job committed it.

`all_or_nothing` runs the block in a savepoint: when the block raises, the database goes back to where it was, and the
error goes on up to the caller, which logs it as before. Frappe's own `savepoint` swallows the error, so the caller
could not log what failed.
"""

from contextlib import contextmanager

import frappe


@contextmanager
def all_or_nothing(label: str):
	"""Run the block in a savepoint; when it raises, undo what it wrote and raise again.

	A block that commits ends the savepoint (a commit releases them all), and so does a deadlock, which rolls the whole
	transaction back: the savepoint is then gone, and that is said in the Error Log, since nothing more can be undone.
	"""
	name = "payments_" + frappe.generate_hash(length=12)
	frappe.db.savepoint(name)
	try:
		yield
	except Exception:
		try:
			frappe.db.rollback(save_point=name)
		except Exception:  # noqa: BLE001 — the savepoint is gone: a commit or a deadlock ended it
			frappe.log_error(
				"Payment booking could not be undone",
				f"{label}: the savepoint was gone (a commit in the block, or a deadlock) when the block failed",
			)
		raise
	else:
		try:
			frappe.db.release_savepoint(name)
		except Exception:  # noqa: BLE001 — the block committed: nothing is left to release
			pass

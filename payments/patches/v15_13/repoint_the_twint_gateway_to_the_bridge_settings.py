# //// Neoffice — added file (no upstream equivalent). The Payment Gateway "Twint" of the image
# //// (built on a machine that once ran `twint_integration`) points at the legacy `Twint Settings`
# //// DocType, which `payments` replaced by `Twint Bridge Settings`. v15_04 repoints it only on an
# //// instance where `twint_integration` is installed, so a NEW client got a gateway whose
# //// settings could not be loaded (« Module import failed for Twint Settings »): the TWINT tile
# //// of its shop had no pay button. Idempotent; nothing is touched while the legacy DocType exists.
import frappe

GATEWAY = "Twint"
LEGACY = "Twint Settings"
BRIDGE = "Twint Bridge Settings"


def execute():
	"""Point the Twint Payment Gateway at the bridge settings once the legacy DocType it names is gone."""
	if not frappe.db.exists("Payment Gateway", GATEWAY) or not frappe.db.exists("DocType", BRIDGE):
		return
	if frappe.db.get_value("Payment Gateway", GATEWAY, "gateway_settings") != LEGACY:
		return
	if frappe.db.exists("DocType", LEGACY):
		return

	values = {"gateway_settings": BRIDGE}
	controller = frappe.db.get_value(BRIDGE, {}, "name", order_by="modified desc")
	if controller:
		values["gateway_controller"] = controller
	frappe.db.set_value("Payment Gateway", GATEWAY, values, update_modified=False)
	frappe.db.commit()
	print(f"Payment Gateway {GATEWAY}: {LEGACY} (gone) -> {BRIDGE}")

// Copyright (c) 2026, Neoffice and contributors
// License: MIT. See LICENSE

frappe.ui.form.on("Twint Bridge Settings", {
	refresh(frm) {
		frm.add_custom_button(__("TWINT merchant portal"), () => {
			window.open("https://portal.twint.ch/partner/gui/?login", "_blank", "noopener");
		});

		const exp = frm.doc.certificate_expires_on;
		if (!exp) return;
		const days = moment(exp).diff(moment().startOf("day"), "days");
		const when = moment(exp).format("DD.MM.YYYY");
		if (days < 0) {
			frm.set_intro(
				__(
					"⚠️ The TWINT certificate EXPIRED on {0} — TWINT payments are down until you upload a renewed .p12 in the field above.",
					[when]
				),
				"red"
			);
		} else if (days <= 45) {
			frm.set_intro(
				__(
					"⚠️ The TWINT certificate expires on {0} (in {1} days). Renew it with TWINT and upload the new .p12 in the field above — it replaces the current one automatically.",
					[when, days]
				),
				"orange"
			);
		}
	},

	p12_certificate(frm) {
		// Auto-fill Merchant UUID from the certificate file name — TWINT names the
		// .p12 after the merchant/tech-user UUID, so the operator doesn't type it.
		if (!frm.doc.p12_certificate || frm.doc.merchant_uuid) return;
		const fname = decodeURIComponent(frm.doc.p12_certificate.split("/").pop() || "");
		const base = fname.replace(/\.(p12|pfx)$/i, "");
		if (/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(base)) {
			frm.set_value("merchant_uuid", base);
			frappe.show_alert({
				message: __("Merchant UUID filled automatically from the certificate"),
				indicator: "green",
			});
		}
	},
});

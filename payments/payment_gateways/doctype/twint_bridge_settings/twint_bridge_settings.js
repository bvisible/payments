// Copyright (c) 2026, Neoffice and contributors
// License: MIT. See LICENSE

frappe.ui.form.on("Twint Bridge Settings", {
	refresh(frm) {
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
});

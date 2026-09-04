//// Neoffice — added file (no upstream equivalent). Form script for `Twint Bridge
//// Settings`. It turns `certificate_expires_on` into an intro banner (orange
//// inside the 45-day window, red once past it — an expired .p12 stops TWINT
//// payments), adds the TWINT merchant-portal button, and fills `merchant_uuid`
//// from the attached file name, which TWINT names after the merchant UUID.
//// Upstream has no TWINT anything: in-store TWINT goes through our central PHP
//// bridge on neoservice, not Stripe's TWINT QR (ADR-002).
//// Commits: cf61f54 2026-06-21 "feat(twint): certificate expiry monitoring — store notAfter on upload, daily reminder email + form banner within 45 days (+ POS alert API)"
////          d5eef5e 2026-06-21 "feat(twint): add TWINT merchant portal link + setup guide on the Twint Bridge Settings form"
////          e80ac15 2026-06-21 "feat(twint): auto-fill Merchant UUID from the certificate file name on attach"
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

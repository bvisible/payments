$(document).ready(function() {
	//// Neoffice — from the link, printed raw: see the same lines of stripe_checkout.js.
	var data = {{ frappe.form_dict | tojson }};
	var doctype = {{ reference_doctype | tojson }}
	var docname = {{ reference_docname | tojson }}

	frappe.call({
		method: "payments.templates.pages.gocardless_checkout.check_mandate",
		freeze: true,
		headers: {
			"X-Requested-With": "XMLHttpRequest"
		},
		args: {
			"data": JSON.stringify(data),
			"reference_doctype": doctype,
			"reference_docname": docname
		},
		callback: function(r) {
			if (r.message) {
				window.location.href = r.message.redirect_to
			}
		}
	})

})

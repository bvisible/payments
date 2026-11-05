$(document).ready(function() {
	//// Neoffice — from the link, printed raw, with no configuration needed to reach this page: see the same
	//// lines of stripe_checkout.js.
	var redirect_flow_id = {{ redirect_flow_id | tojson }};
	var doctype = {{ reference_doctype | tojson }};
	var docname = {{ reference_docname | tojson }};

	frappe.call({
		method: "payments.templates.pages.gocardless_confirmation.confirm_payment",
		freeze: true,
		headers: {
			"X-Requested-With": "XMLHttpRequest"
		},
		args: {
			"redirect_flow_id": redirect_flow_id,
			"reference_doctype": doctype,
			"reference_docname": docname
		},
		callback: function(r) {
			if (r.message) {
				window.location.href = r.message.redirect_to;
			}
		}
	});

});

$(document).ready(function() {

	var button = document.querySelector('#submit-button');
	var form = document.querySelector('#payment-form');
	//// Neoffice — from the link, printed raw: see the same lines of stripe_checkout.js.
	var data = {{ frappe.form_dict | tojson }};
	var doctype = {{ reference_doctype | tojson }}
	var docname = {{ reference_docname | tojson }}

	braintree.dropin.create({
		authorization: "{{ client_token }}",
		container: '#bt-dropin',
		paypal: {
			flow: 'vault'
		}
	}, function(createErr, instance) {
		form.addEventListener('submit', function(event) {
			event.preventDefault();
			instance.requestPaymentMethod(function(err, payload) {
				if (err) {
					console.log('Error', err);
					return;
				}
				frappe.call({
					method: "payments.templates.pages.braintree_checkout.make_payment",
					freeze: true,
					headers: {
						"X-Requested-With": "XMLHttpRequest"
					},
					args: {
						"payload_nonce": payload.nonce,
						"data": JSON.stringify(data),
						"reference_doctype": doctype,
						"reference_docname": docname
					},
					callback: function(r) {
						if (r.message && r.message.status == "Completed") {
							window.location.href = r.message.redirect_to
						} else if (r.message && r.message.status == "Error") {
							window.location.href = r.message.redirect_to
						}
					}
				})
			});
		});

		instance.on('paymentMethodRequestable', function (event) {
			button.removeAttribute('disabled');
		});

		instance.on('noPaymentMethodRequestable', function () {
			button.setAttribute('disabled', true);
		});
	});

})

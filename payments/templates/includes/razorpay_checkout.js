$(document).ready(function(){
	(function(e){
		//// Neoffice — these come from the link or from what a visitor stored in the Integration Request, printed
		//// raw: see the same lines of stripe_checkout.js.
		var options = {
			"key": "{{ api_key }}",
			"amount": cint({{ amount }} * 100), // 2000 paise = INR 20
			//// Neoffice — printed raw into a script: a value from the link could close the string.
			"currency": {{ currency | tojson }},
			"name": {{ title | tojson }},
			"description": {{ description | tojson }},
			"subscription_id": {{ subscription_id | tojson }},
			"handler": function (response){
				//// Neoffice — printed raw into a script: a value from the link could close the string.
				razorpay.make_payment_log(
					response, options, {{ reference_doctype | tojson }},
					{{ reference_docname | tojson }}, {{ token | tojson }}
				);
			},
			"prefill": {
				//// Neoffice — printed raw into a script: a value from the link could close the string.
				"name": {{ payer_name | tojson }},
				"email": {{ payer_email | tojson }},
				"order_id": {{ order_id | tojson }}
			},
			//// Neoffice — printed raw into a script: a value from the link could close the string.
			"notes": {{ frappe.form_dict | tojson }}
		};

		var rzp = new Razorpay(options);
		rzp.open();
		//	e.preventDefault();
	})();
})

frappe.provide('razorpay');

razorpay.make_payment_log = function(response, options, doctype, docname, token){
	$('.razorpay-loading').addClass('hidden');
	$('.razorpay-confirming').removeClass('hidden');

	frappe.call({
		method:"payments.templates.pages.razorpay_checkout.make_payment",
		freeze:true,
		headers: {"X-Requested-With": "XMLHttpRequest"},
		args: {
			"razorpay_payment_id": response.razorpay_payment_id,
			"options": options,
			"reference_doctype": doctype,
			"reference_docname": docname,
			"token": token
		},
		callback: function(r){
			if (r.message && r.message.status == 200) {
				window.location.href = r.message.redirect_to
			}
			else if (r.message && ([401,400,500].indexOf(r.message.status) > -1)) {
				window.location.href = r.message.redirect_to
			}
		}
	})
}

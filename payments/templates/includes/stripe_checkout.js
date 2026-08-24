var stripe = Stripe("{{ publishable_key }}");

var elements = stripe.elements();

//// Neoffice — les champs de carte sont dessinés PAR Stripe dans son iframe :
//// notre feuille de style ne peut pas les atteindre, seul cet objet le peut.
//// Sans lui, trois lignes en Helvetica bleu au milieu d'une page en Karla ink.
var style = {
	base: {
		color: '#141414',
		lineHeight: '20px',
		fontFamily: 'Karla, system-ui, -apple-system, "Segoe UI", sans-serif',
		fontSmoothing: 'antialiased',
		fontSize: '16px',
		'::placeholder': {
			color: '#8A8078'
		}
	},
	invalid: {
		color: '#B3261E',
		iconColor: '#B3261E'
	}
};

var card = elements.create('card', {
	hidePostalCode: true,
	style: style
});

card.mount('#card-element');

function setOutcome(result) {

	if (result.token) {
		$('#submit').prop('disabled', true)
		$('#submit').html(__('Processing...'))
		frappe.call({
			method:"payments.templates.pages.stripe_checkout.make_payment",
			freeze:true,
			headers: {"X-Requested-With": "XMLHttpRequest"},
			args: {
				"stripe_token_id": result.token.id,
				"data": JSON.stringify({{ frappe.form_dict|json }}),
				"reference_doctype": "{{ reference_doctype }}",
				"reference_docname": "{{ reference_docname }}",
				"payment_gateway": "{{ payment_gateway }}"
			},
			callback: function(r) {
				if (r.message.status == "Completed") {
					$('#submit').hide()
					$('.success').show()
					setTimeout(function() {
						window.location.href = r.message.redirect_to
					}, 2000);
				} else {
					$('#submit').hide()
					$('.error').show()
					setTimeout(function() {
						window.location.href = r.message.redirect_to
					}, 2000);
				}
			}
		});

	} else if (result.error) {
		$('.error').html(result.error.message);
		$('.error').show()
	}
}

card.on('change', function(event) {
	var displayError = document.getElementById('card-errors');
	if (event.error) {
		displayError.textContent = event.error.message;
	} else {
		displayError.textContent = '';
	}
});

frappe.ready(function() {
	$('#submit').off("click").on("click", function(e) {
		e.preventDefault();
		var extraDetails = {
			name: $('input[name=cardholder-name]').val(),
			email: $('input[name=cardholder-email]').val()
		}
		stripe.createToken(card, extraDetails).then(setOutcome);
	})
});

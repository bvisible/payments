//// Neoffice — added file (no upstream equivalent). The Wallee setup page, the twin of the Payrexx one
//// (Jérémy, 2026-11-10: the same page, the same elements). Paste the space, the application user and
//// its key, « Connect »: Neoffice saves Wallee Settings, binds the channels, lays down the ERP side and
//// sets the webhook up at Wallee itself. Layout: public/js/provider_setup_page.js.

frappe.pages["wallee-setup-wizard"].on_page_load = function (wrapper) {
	frappe.require("/assets/payments/js/provider_setup_page.js", () => {
		new payments.setup.Page(wrapper, wallee_setup_config());
	});
};

function wallee_setup_config() {
	const esc = (t) => payments.setup.esc(t);
	const ok_or = (row, yes, no) => (row.state === "ok" ? yes : no);

	return {
		title: "Wallee",
		subtitle: __("Card and TWINT, in the shop and online. Paste your access: Neoffice prepares the rest."),
		method: "payments.api.wallee_setup",
		fields: [
			{
				fieldname: "space_id",
				fieldtype: "Data",
				label: __("Space (Space ID)"),
				description: __("The number in the address of your space: app-wallee.com/s/12345/…"),
			},
			{
				fieldname: "user_id",
				fieldtype: "Data",
				label: __("Application user (ID)"),
				description: __("Account › Users › Application users: create « Neoffice », role « Account Admin »."),
			},
			{
				fieldname: "authentication_key",
				fieldtype: "Password",
				label: __("Authentication key"),
				description: __("Shown once only, when the application user is created."),
			},
		],

		fill_fields(status, page) {
			const p = status.provider;
			if (!p) return;
			if (!page.fields.space_id.get_value() && p.space_id) page.fields.space_id.set_value(String(p.space_id));
			if (!page.fields.user_id.get_value() && p.user_id) page.fields.user_id.set_value(String(p.user_id));
			if (p.has_key) {
				page.fields.authentication_key.df.description = __("Already stored — leave empty to keep it");
				page.fields.authentication_key.refresh();
			}
		},

		links() {
			return [["https://app-wallee.com/", __("Open my Wallee account")]];
		},

		rows: {
			account: {
				label: __("Account"),
				text: (row) => ({ pill: ok_or(row, __("Connected"), __("To connect")), detail: row.detail }),
			},
			accounting: {
				label: __("Accounting"),
				text: (row) => ({
					pill: ok_or(row, __("Ready"), __("To prepare")),
					detail: row.detail ? __("{0}, and the Wallee gateway in CHF", [row.detail]) : "",
				}),
			},
			till: {
				label: __("Till"),
				text: (row) => ({
					pill: ok_or(row, __("Ready"), __("To prepare")),
					detail: row.detail
						? __("« {0} » on {1}: the cashier types the amount on the terminal.", [row.till_mode, row.detail])
						: "",
				}),
			},
			shop: {
				label: __("Online shop"),
				text: (row) => {
					if (row.state === "ok")
						return {
							pill: __("Ready to choose"),
							html: `<a href="/app/payment-settings">${esc(__("Choose what it takes in Payment settings"))}</a>`,
						};
					if (row.state === "waiting")
						return {
							pill: __("In test"),
							detail: __("Prepared. Wallee can be chosen for the shop once it is in production."),
						};
					return { pill: __("To prepare"), detail: "" };
				},
			},
			webhook: {
				label: __("Instant confirmations"),
				text: (row) => {
					if (row.state === "ok")
						return { pill: __("In place"), detail: __("Set up by Neoffice at Wallee: nothing to copy.") };
					if (row.state === "error") return { pill: __("Not in place"), detail: row.detail };
					return { pill: __("To prepare"), detail: __("Set up by Neoffice at « Connect ».") };
				},
			},
		},

		mode_text(state) {
			return {
				live: __("Production — payments are real"),
				test: __("Test mode — no real payment"),
				unknown: __("Not connected yet"),
			}[state];
		},

		mode_help(status) {
			const live = status.mode === "live";
			return `
				<p>${esc(__("How Neoffice knows: it is the mode Neoffice uses Wallee in. Terminals are created in that environment, and Wallee can only be chosen for the shop in production. Online payments follow the setup of your Wallee space."))}</p>
				<p style="margin-top:12px"><b>${esc(__("Going to production: Neoffice does it, once Wallee has activated your account"))}</b></p>
				<ol>
					<li>${esc(__("At Wallee: your account is activated (acquiring contract signed, payment processor live)."))}</li>
					<li>${esc(__("Here: « Go to production ». The next payments are real."))}</li>
				</ol>
				<div class="ps-note">${esc(live ? __("Wallee is in production.") : __("Wallee is in test: no real payment."))}</div>`;
		},

		mode_action(status, page) {
			if (!status.provider) return { label: __("Close"), run: () => {} };
			const live = status.mode === "live";
			return {
				label: live ? __("Back to test") : __("Go to production"),
				run: () =>
					new Promise((resolve) =>
						frappe.confirm(
							live ? __("Back to test: the next payments are not real.") : __("The next payments will be real."),
							async () => resolve(await page.run("set_mode", { mode: live ? "test" : "live" })),
							() => resolve()
						)
					),
			};
		},

		panels(status) {
			const hook = (status.rows || []).find((r) => r.key === "webhook") || {};
			if (hook.state !== "error") return "";
			return `
				<div class="ps-card">
					<div class="ps-head"><span class="ps-num">↯</span><h3>${esc(__("Instant confirmations are not in place"))}</h3></div>
					<p class="ps-detail" style="margin:0">${esc(hook.detail || "")}</p>
					<p class="ps-detail" style="margin:0">${esc(__("Meanwhile Neoffice asks Wallee every minute: payments are still recorded."))}</p>
					<div class="ps-actions"><button class="btn btn-primary btn-sm" data-action="retry-webhook">${esc(__("Try again"))}</button></div>
				</div>`;
		},

		actions(status) {
			if (!status.provider) return "";
			return `
				<a class="btn btn-default btn-sm" href="/app/payment-terminal-wizard">${esc(__("Link a terminal to the till"))}</a>
				<a class="btn btn-primary btn-sm" href="/app/payment-settings">${esc(__("Who takes what"))}</a>`;
		},

		async on_action(action, page) {
			if (action === "retry-webhook") await page.run("setup_webhook");
		},
	};
}

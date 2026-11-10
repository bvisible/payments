//// Neoffice — added file (no upstream equivalent).
////
//// The Payrexx setup page. Since 2026-11-10 it is one gesture: paste the space and the API key,
//// « Connect », and Neoffice lays down the rest (clearing account, gateways, channels, the till mode)
//// and reads what the Payrexx account really offers off a test payment page. Nothing reaches the shop
//// on its own: the merchant chooses in Payment settings. The layout is shared with Wallee
//// (public/js/provider_setup_page.js), as Jérémy asked: every provider looks the same.

frappe.pages["payrexx-setup-wizard"].on_page_load = function (wrapper) {
	frappe.require("/assets/payments/js/provider_setup_page.js", () => {
		new payments.setup.Page(wrapper, payrexx_setup_config());
	});
};

function payrexx_setup_config() {
	const esc = (t) => payments.setup.esc(t);
	const space = (status, page) =>
		((page && page.fields.instance.get_value()) || (status.provider && status.provider.instance) || "").trim();
	const base = (name) => `https://${encodeURIComponent(name)}.payrexx.com/cadmin/index.php`;

	return {
		title: "Payrexx",
		subtitle: __("Card and TWINT, in the shop and online. Paste your key: Neoffice prepares the rest."),
		method: "payments.api.payrexx_setup",
		fields: [
			{
				fieldname: "instance",
				fieldtype: "Data",
				label: __("Your Payrexx space"),
				description: __("The name or the address of your back office: both are read."),
			},
			{
				fieldname: "api_secret",
				fieldtype: "Password",
				label: __("API key"),
				description: __("Back office › API & Plugins: the key of type « API ». The « HPOS Device API Key » is not needed."),
			},
		],

		secret_field: "api_secret",
		fill_fields(status, page) {
			const p = status.provider;
			if (!p) return;
			if (!page.fields.instance.get_value() && p.instance) page.fields.instance.set_value(p.instance);
			if (p.has_api_secret) {
				page.fields.api_secret.df.description = __("Already stored — leave empty to keep it");
				page.fields.api_secret.refresh();
			}
		},

		links(status) {
			const name = space(status);
			if (!name) return [];
			return [[`${base(name)}?cmd=checkout&act=integrations`, __("Open API & Plugins at Payrexx")]];
		},

		rows: {
			account: {
				label: __("Account"),
				text: (row) => ({ pill: row.state === "ok" ? __("Connected") : __("To connect"), detail: row.detail }),
			},
			accounting: {
				label: __("Accounting"),
				text: (row) => ({
					pill: row.state === "ok" ? __("Ready") : __("To prepare"),
					detail: row.detail ? __("{0}, and the Payrexx gateways in CHF", [row.detail]) : "",
				}),
			},
			till: {
				label: __("Till"),
				text: (row) => {
					const linked = [...new Set((row.linked || []).map((l) => `${l.pos_profile} (${l.default_device})`))];
					let detail = row.detail
						? __("« {0} » on {1}: the cashier types the amount on the terminal.", [row.till_mode, row.detail])
						: "";
					if (linked.length) detail += " " + __("Linked to the till: {0}.", [linked.join(", ")]);
					return { pill: row.state === "ok" ? __("Ready") : __("To prepare"), detail };
				},
			},
			shop: {
				label: __("Online shop"),
				text: (row) => {
					if (row.state === "ok")
						return {
							pill: __("Ready to choose"),
							html: `${esc(__("Payrexx offers {0} online.", [row.detail]))} <a href="/app/payment-settings">${esc(__("Choose what it takes in Payment settings"))}</a>`,
						};
					if (row.state === "waiting")
						return {
							pill: __("Waiting for Payrexx"),
							detail: __("Card and TWINT prepared, not shown. A test payment page shows that Payrexx does not take online payments yet: see below."),
						};
					return { pill: __("Not checked yet"), detail: "" };
				},
			},
			webhook: {
				label: __("Instant confirmations"),
				text: (row) => ({
					pill: row.state === "ok" ? __("In place") : __("Optional"),
					detail:
						row.state === "ok"
							? __("Payrexx tells Neoffice the moment a payment ends.")
							: __("The webhook: needed to link a terminal to the till. Without it the shop confirms when the shopper comes back."),
				}),
			},
		},

		mode_text(state) {
			return {
				live: __("Production — payments are real"),
				test: __("Test mode — no real payment"),
				mixed: __("Mixed — part still in test or not open"),
				unknown: __("Not checked yet"),
			}[state];
		},

		mode_help(status) {
			const name = space(status);
			const online = status.online || {};
			const methods = (online.methods || []).join(", ") || "—";
			return `
				<p>${esc(__("How Neoffice knows: it opens a test payment page (nothing is paid, it is deleted at once) and reads what it offers."))}</p>
				<div class="ps-note">${esc(__("Offered today: {0}", [methods]))}${online.test_banner ? "<br>" + esc(__("Payrexx shows « test mode » on that page: a provider of the account is still in test.")) : ""}</div>
				<p style="margin-top:12px"><b>${esc(__("Going to production happens at Payrexx; Neoffice cannot do it"))}</b></p>
				<ol>
					<li>${esc(__("Back office › Payment providers › the provider in test › Configure › Mode: Live › Save."))}</li>
					<li>${esc(__("Online sales missing from the contract: ask the support (the sentence is ready on the page)."))}</li>
					<li>${esc(__("Come back here: « Check now »."))}</li>
				</ol>
				${name ? `<a href="${esc(base(name))}?cmd=checkout&act=psps" target="_blank" rel="noopener">${esc(__("Open Payment providers"))} ↗</a>` : ""}`;
		},

		panels(status) {
			const out = [];
			const shop = (status.rows || []).find((r) => r.key === "shop") || {};
			const name = space(status);
			if (shop.state === "waiting") {
				const sentence = __(
					"Hello, please add online sales for our website {0} to our Payrexx Pay contract (cards and TWINT).",
					[window.location.host]
				);
				const when = shop.checked_at ? frappe.datetime.str_to_user(shop.checked_at) : "—";
				out.push(`
					<div class="ps-card">
						<div class="ps-head"><span class="ps-num">!</span><h3>${esc(__("Your shop is waiting for Payrexx"))}</h3></div>
						<p class="ps-detail" style="margin:0">${esc(__("Your Payrexx contract covers the shop on site, not your website yet. Ask Payrexx to open online sales: the « Support » button at the bottom right of your back office. One sentence is enough:"))}</p>
						<div class="ps-note">${esc(sentence)}</div>
						<div class="ps-actions">
							<button class="btn btn-default btn-sm" data-action="copy" data-text="${esc(sentence)}">${esc(__("Copy the sentence"))}</button>
							${name ? `<a href="https://${esc(encodeURIComponent(name))}.payrexx.com/cadmin/" target="_blank" rel="noopener" style="font-weight:700">${esc(__("Open my Payrexx back office"))} ↗</a>` : ""}
						</div>
						<p class="ps-detail" style="margin:0">${esc(__("Every morning Neoffice opens the test page again and e-mails you the day Payrexx has said yes. Nothing changes in your shop on its own."))}</p>
						<div class="ps-actions">
							<button class="btn btn-primary btn-sm" data-action="check">${esc(__("Check now"))}</button>
							<span class="ps-detail">${esc(__("Last check: {0}", [when]))}</span>
						</div>
					</div>`);
			}
			if (status.provider) {
				const ok = status.provider.has_webhook_key;
				out.push(`
					<div class="ps-card">
						<div class="ps-head"><span class="ps-num">↯</span><h3>${esc(__("Instant confirmations (optional)"))}</h3>
							<span class="ps-aside">${payments.setup.pill(ok ? "ok" : "optional", ok ? __("In place") : __("Optional"))}</span></div>
						<p class="ps-detail" style="margin:0">${esc(__("Payrexx does not let the webhook be created from outside: two gestures in its back office, under Webhooks. 1. Add this address, content type JSON, event Transaction, retries on. 2. « Generate Signing-Key », then paste the key here."))}</p>
						<code class="ps-url">${esc(status.webhook_url)}</code>
						<div class="ps-actions">
							<input type="password" class="form-control" data-zone="webhook-key" style="max-width:340px" placeholder="${esc(ok ? __("Already stored — leave empty to keep it") : __("Signing key"))}">
							<button class="btn btn-default btn-sm" data-action="save-webhook">${esc(__("Save the key"))}</button>
							${name ? `<a href="${esc(base(name))}?cmd=checkout&act=api" target="_blank" rel="noopener" style="font-weight:700">${esc(__("Open Webhooks at Payrexx"))} ↗</a>` : ""}
						</div>
					</div>`);
			}
			return out.join("");
		},

		actions(status) {
			if (!status.provider) return "";
			return `
				<button class="btn btn-default btn-sm" data-action="link-terminal">${esc(__("Link a terminal to the till"))}</button>
				<a class="btn btn-primary btn-sm" href="/app/payment-settings">${esc(__("Who takes what"))}</a>`;
		},

		async on_action(action, page) {
			if (action === "save-webhook") {
				const key = (page.$body.find('[data-zone="webhook-key"]').val() || "").trim();
				if (!key) return frappe.msgprint(__("Paste the signing key first"));
				await page.run("save_webhook_key", { webhook_signing_key: key });
				frappe.show_alert({ message: __("Signing key saved"), indicator: "green" });
			}
			if (action === "link-terminal") {
				const d = new frappe.ui.Dialog({
					title: __("Link a terminal to the till"),
					fields: [
						{
							fieldtype: "HTML",
							fieldname: "intro",
							options: `<p class="text-muted">${esc(__("Optional. Linked, the terminal receives the amount from the till: nothing to type. Without it, it takes payments all the same."))}</p>`,
						},
						{
							fieldname: "serial_number",
							fieldtype: "Data",
							label: __("Serial number"),
							reqd: 1,
							description: __("On the label at the back (N86… or N6…), or in the back office › Card readers."),
						},
						{ fieldname: "pos_profile", fieldtype: "Link", options: "POS Profile", label: __("Till"), reqd: 1 },
						{
							fieldname: "pairing_code",
							fieldtype: "Data",
							label: __("Pay-Connect code"),
							reqd: 1,
							description: __("On the terminal: menu ≡ at the top left › Pay-Connect. A 6-character code appears. Stay on that screen: the code changes if you leave it."),
						},
					],
					primary_action_label: __("Link"),
					primary_action: async (values) => {
						const { message } = await page.call("link_terminal", values);
						if (message && message.ok) {
							d.hide();
							frappe.show_alert({
								message: __("{0} linked to {1}: make a first payment of one franc from the till.", [
									values.serial_number,
									values.pos_profile,
								]),
								indicator: "green",
							});
							page.load();
						}
					},
				});
				d.show();
			}
		},
	};
}

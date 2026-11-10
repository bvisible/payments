//// Neoffice — added file (no upstream equivalent). « Payment settings »: who takes what, on the shop
//// and at the till, in a few clicks (Jérémy, 2026-11-10). Nothing switches on its own: Payrexx opening
//// online sales only makes it choosable here. Layout shared with the provider pages
//// (public/js/provider_setup_page.js).

frappe.pages["payment-settings"].on_page_load = function (wrapper) {
	frappe.require("/assets/payments/js/provider_setup_page.js", () => new PaymentSettings(wrapper));
};

class PaymentSettings {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({ parent: wrapper, title: __("Payment Settings"), single_column: true });
		payments.setup.style_once();
		this.$body = $(`<div class="ps-setup"></div>`).appendTo(this.page.main);
		this.$body.on("change", "select[data-method]", (e) => this.set_shop($(e.currentTarget)));
		this.$body.on("click", "[data-till-mode]", (e) => this.toggle_till($(e.currentTarget)));
		this.$body.on("change", "select[data-attach]", (e) => this.attach_terminal($(e.currentTarget)));
		this.$body.on("click", "[data-detach]", (e) => {
			e.preventDefault();
			this.detach_terminal($(e.currentTarget));
		});
		this.$body.on("click", "[data-remove]", (e) => this.remove_terminal($(e.currentTarget).data("remove")));
		this.$body.on("click", "[data-add]", (e) => this.add_terminal($(e.currentTarget).data("add")));
		this.load();
	}

	async load() {
		const { message } = await frappe.call({ method: "payments.api.payment_settings.get_overview" });
		this.render(message || {});
	}

	/** A refused change is shown as the server says it, and the page goes back to what is true. */
	async call(method, args) {
		try {
			const { message } = await frappe.call({
				method: method.includes(".") ? method : `payments.api.payment_settings.${method}`,
				args,
				freeze: true,
			});
			if (message && message.shop) this.render(message);
			return message;
		} catch (e) {
			this.load();
			return null;
		}
	}

	async set_shop($select) {
		const method = $select.data("method");
		const provider = $select.val();
		if (await this.call("set_shop_method", { method, provider }))
			frappe.show_alert({ message: __("Saved: the shop offers it now"), indicator: "green" });
	}

	async toggle_till($chip) {
		const mode = $chip.data("till-mode");
		const on = $chip.data("on") ? 0 : 1;
		const result = await this.call("set_till_mode", { mode, on });
		if (!result) return;
		if ((result.skipped || []).length)
			frappe.msgprint(__("Not added to {0}: no account for that till's company.", [result.skipped.join(", ")]));
		else frappe.show_alert({ message: __("Saved: the tills take it at their next load"), indicator: "green" });
	}

	async attach_terminal($select) {
		const device = $select.data("attach");
		const till = $select.val();
		if (!till) return;
		if (await this.call("payments.api.terminals.attach_terminal", { device, pos_profile: till }))
			frappe.show_alert({ message: __("Saved: the till takes it at its next load"), indicator: "green" });
	}

	async detach_terminal($chip) {
		const device = $chip.data("detach");
		const till = $chip.data("till");
		if (await this.call("payments.api.terminals.detach_terminal", { device, pos_profile: till }))
			frappe.show_alert({ message: __("Saved: the till takes it at its next load"), indicator: "green" });
	}

	remove_terminal(device) {
		frappe.confirm(
			__("Take this terminal off every till? It stays in your provider's account and can be added again."),
			async () => {
				if (await this.call("payments.api.terminals.remove_terminal", { device }))
					frappe.show_alert({ message: __("Terminal removed"), indicator: "green" });
			}
		);
	}

	till_field() {
		return {
			fieldname: "pos_profile",
			fieldtype: "Select",
			label: __("Till"),
			options: (this.data.terminals || {}).tills || [],
			reqd: 1,
		};
	}

	async add_terminal(provider) {
		if (provider === "payrexx") {
			const d = new frappe.ui.Dialog({
				title: __("Add a Payrexx terminal"),
				fields: [
					{
						fieldtype: "HTML",
						fieldname: "help",
						options: `<p class="text-muted">${payments.setup.esc(
							__("On the terminal: ≡ → Pay-Connect. Type the 6-character code it shows, while that screen stays open (the code changes when you leave it). The serial number is on the back.")
						)}</p>`,
					},
					{ fieldname: "serial_number", fieldtype: "Data", label: __("Serial number"), reqd: 1 },
					{ fieldname: "pairing_code", fieldtype: "Data", label: __("Pairing code"), reqd: 1 },
					this.till_field(),
				],
				primary_action_label: __("Add"),
				primary_action: async (values) => {
					d.hide();
					if (await this.call("payments.api.payrexx_setup.link_terminal", values)) {
						frappe.show_alert({ message: __("Terminal added"), indicator: "green" });
						this.load();
					}
				},
			});
			d.show();
			return;
		}
		const found = await this.call("payments.api.terminals.wallee_terminals", {});
		if (!found) return;
		if (!found.length) {
			frappe.msgprint(
				__("Every active terminal of your Wallee space is already at a till. A new terminal is made in the Wallee portal, or with the {0}.", [
					`<a href="/app/payment-terminal-wizard">${payments.setup.esc(__("terminal wizard"))}</a>`,
				])
			);
			return;
		}
		const d = new frappe.ui.Dialog({
			title: __("Add a Wallee terminal"),
			fields: [
				{
					fieldname: "terminal_id",
					fieldtype: "Select",
					label: __("Terminal of your Wallee space"),
					options: found.map((t) => ({ value: String(t.id), label: `${t.name} (${t.identifier || t.id})` })),
					reqd: 1,
				},
				this.till_field(),
			],
			primary_action_label: __("Add"),
			primary_action: async (values) => {
				d.hide();
				if (await this.call("payments.api.terminals.add_wallee_terminal", values))
					frappe.show_alert({ message: __("Terminal added"), indicator: "green" });
			},
		});
		d.show();
	}

	render_terminals(t) {
		const esc = payments.setup.esc;
		const status = {
			online: ["ok", __("Answers", null, "card terminal status")],
			offline: ["error", __("Not answering")],
			unknown: ["unknown", __("Not checked yet")],
		};
		const tills = t.tills || [];
		const rows = (t.devices || [])
			.map((d) => {
				const [state, text] = status[d.status] || status.unknown;
				const chips = d.tills
					.map(
						(x) => `<span class="ps-pill muted" style="margin:0 6px 6px 0">${esc(x)}
							<a href="#" data-detach="${esc(d.name)}" data-till="${esc(x)}" title="${esc(__("Take it off this till"))}"
								style="margin-left:4px;color:inherit;text-decoration:none">×</a></span>`
					)
					.join("");
				const free = tills.filter((x) => !d.tills.includes(x));
				const add = free.length
					? `<select class="form-control input-xs" data-attach="${esc(d.name)}" style="max-width:220px">
						<option value="">${esc(__("+ Add to a till"))}</option>
						${free.map((x) => `<option value="${esc(x)}">${esc(x)}</option>`).join("")}</select>`
					: "";
				return `<div class="ps-row" style="grid-template-columns:minmax(0,1.1fr) minmax(0,1.5fr) 150px 100px">
					<div><b>${esc(d.label)}</b><div class="ps-detail">${esc([d.provider_label || d.provider, d.serial].filter(Boolean).join(" · "))}</div></div>
					<div>${chips || `<span class="text-muted">${esc(__("No till"))}</span>`}${add}</div>
					<div>${payments.setup.pill(state, text)}</div>
					<div style="text-align:right"><button class="btn btn-xs btn-default" data-remove="${esc(d.name)}">${esc(__("Remove"))}</button></div></div>`;
			})
			.join("");
		const labels = { payrexx: __("Add a Payrexx terminal"), wallee: __("Add a Wallee terminal") };
		const add = (t.can_add || [])
			.map((p) => `<button class="btn btn-sm btn-default" data-add="${p}">+ ${esc(labels[p])}</button>`)
			.join(" ");
		return `<div class="ps-card">
			<div class="ps-head"><h3>${esc(__("Card terminals"))}</h3><span class="ps-aside">${esc(__("one list, every provider"))}</span></div>
			${rows || `<div class="ps-note">${esc(__("No card terminal at a till yet."))}</div>`}
			<div class="ps-actions">${add || `<span class="text-muted">${esc(__("Connect Payrexx or Wallee to add a terminal."))}</span>`}</div>
		</div>`;
	}

	render(data) {
		this.data = data;
		const esc = payments.setup.esc;
		const mode_text = {
			live: __("In production"),
			test: __("Test mode"),
			mixed: __("Mixed"),
			unknown: __("Not checked yet"),
		};
		const providers = (data.providers || [])
			.map(
				(p) => `<div class="ps-card" style="padding:16px 18px;gap:10px">
					<b style="font-size:18px">${esc(p.label)}</b>
					<span>${payments.setup.pill(p.mode || "unknown", mode_text[p.mode] || mode_text.unknown)}</span>
					<span class="ps-detail" style="color:var(--text-muted);font-size:13.5px">${esc(p.note)}</span>
					<a href="${esc(p.route)}" style="font-weight:700;margin-top:auto">${esc(__("Open its page"))}</a>
				</div>`
			)
			.join("");

		const labels = { card: __("Card (Visa, Mastercard)"), twint: "TWINT" };
		const rows = ["card", "twint"]
			.map((method) => {
				const shop = (data.shop || {})[method] || { options: [], current: "none" };
				const select = `<select class="form-control" data-method="${method}">${shop.options
					.map(
						(o) =>
							`<option value="${esc(o.key)}" ${o.key === shop.current ? "selected" : ""} ${
								o.available || o.key === shop.current ? "" : "disabled"
							} title="${esc(o.hint || "")}">${esc(o.label)}${o.reason ? " — " + esc(o.reason) : ""}</option>`
					)
					.join("")}</select>`;
				const chips = ((data.till || {})[method] || [])
					.map((m) => {
						const on = m.state !== "off";
						const label = m.state === "some" ? `${m.mode} (${m.tills.join(", ")})` : m.mode;
						return `<button class="btn btn-sm ${on ? "btn-primary" : "btn-default"}" style="border-radius:999px;margin:0 6px 6px 0"
							data-till-mode="${esc(m.mode)}" data-on="${on ? 1 : 0}">${on ? "✓ " : ""}${esc(label)}</button>`;
					})
					.join("") || `<span class="text-muted">—</span>`;
				return `<div class="ps-row" style="grid-template-columns:220px minmax(0,1fr) minmax(0,1fr)">
					<b>${esc(labels[method])}</b><div>${select}</div><div>${chips}</div></div>`;
			})
			.join("");

		this.$body.html(`
			<div class="ps-hero"><div><h2>${esc(__("Payments"))}</h2>
				<p>${esc(__("Who takes what, at the till and on your shop. A few clicks, and it is changed."))}</p></div></div>
			<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px">${providers ||
				`<div class="ps-note">${esc(__("No payment provider connected yet."))} <a href="/app/payrexx-setup-wizard">Payrexx</a> · <a href="/app/wallee-setup-wizard">Wallee</a></div>`}</div>
			<div class="ps-card">
				<div class="ps-head"><h3>${esc(__("Who takes what"))}</h3></div>
				<div class="ps-row" style="grid-template-columns:220px minmax(0,1fr) minmax(0,1fr);border-top:0;color:var(--text-muted);font-size:12px;text-transform:uppercase;letter-spacing:.08em">
					<span>${esc(__("Means"))}</span><span>${esc(__("Online shop"))}</span><span>${esc(__("Till"))}</span></div>
				${rows}
				<p class="text-muted" style="margin:6px 0 0;font-size:13.5px">${esc(
					__("A choice is live for the shop at once; the tills take it at their next load. A provider in test, or not yet open to online sales, cannot be chosen: the reason is written beside it.")
				)}</p>
			</div>
			${this.render_terminals(data.terminals || {})}`);
	}
}

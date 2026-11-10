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
		this.load();
	}

	async load() {
		const { message } = await frappe.call({ method: "payments.api.payment_settings.get_overview" });
		this.render(message || {});
	}

	async call(method, args) {
		const { message } = await frappe.call({
			method: `payments.api.payment_settings.${method}`,
			args,
			freeze: true,
		});
		if (message) this.render(message);
		return message;
	}

	async set_shop($select) {
		const method = $select.data("method");
		const provider = $select.val();
		await this.call("set_shop_method", { method, provider });
		frappe.show_alert({ message: __("Saved: the shop offers it now"), indicator: "green" });
	}

	async toggle_till($chip) {
		const mode = $chip.data("till-mode");
		const on = $chip.data("on") ? 0 : 1;
		const result = await this.call("set_till_mode", { mode, on });
		if (result && (result.skipped || []).length)
			frappe.msgprint(__("Not added to {0}: no account for that till's company.", [result.skipped.join(", ")]));
		else frappe.show_alert({ message: __("Saved: the tills take it at their next load"), indicator: "green" });
	}

	render(data) {
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
							}>${esc(o.label)}${o.reason ? " — " + esc(o.reason) : ""}</option>`
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
			</div>`);
	}
}

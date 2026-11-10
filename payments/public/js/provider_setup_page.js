//// Neoffice — added file (no upstream equivalent). The one layout every payment provider's setup
//// page shares (Payrexx, Wallee): a header with the test/production pill, « Your account » (the key
//// fields and Connect), « What Neoffice prepared » (one row per part, each with its state), then the
//// panels a state calls for. Jérémy validated it on 2026-11-10 (Claude Design canvas « Payrexx — mise
//// en service guidée ») and asked that every provider look the same, so it lives here once.

frappe.provide("payments.setup");

payments.setup.PILL = {
	ok: "success",
	live: "success",
	waiting: "warn",
	test: "warn",
	mixed: "danger",
	error: "danger",
	todo: "muted",
	optional: "muted",
	unknown: "muted",
};

payments.setup.style_once = function () {
	if (document.getElementById("ps-setup-style")) return;
	const css = `
	.ps-setup { max-width: 1120px; margin: 0 auto; padding: 8px 4px 40px; display: flex; flex-direction: column; gap: 20px; }
	.ps-setup .ps-hero { border: 1px solid var(--border-color); border-radius: 18px; padding: 24px 28px;
		background: linear-gradient(115deg, rgba(245,93,26,.10), rgba(251,246,238,.5) 55%, transparent);
		display: flex; justify-content: space-between; align-items: center; gap: 20px; flex-wrap: wrap; }
	.ps-setup .ps-hero h2 { margin: 0; font-family: var(--font-display, Forum, serif); font-weight: 400; font-size: 40px; }
	.ps-setup .ps-hero p { margin: 6px 0 0; color: var(--text-muted); font-size: 15px; }
	.ps-setup .ps-card { border: 1px solid var(--border-color); border-radius: 22px; padding: 22px 26px;
		background: var(--card-bg); display: flex; flex-direction: column; gap: 14px; }
	.ps-setup .ps-head { display: flex; align-items: center; gap: 12px; }
	.ps-setup .ps-num { width: 34px; height: 34px; border-radius: 50%; background: var(--text-color); color: var(--bg-color);
		display: inline-flex; align-items: center; justify-content: center; font-weight: 700; flex: none; }
	.ps-setup .ps-head h3 { margin: 0; font-size: 21px; font-weight: 700; }
	.ps-setup .ps-head .ps-aside { margin-left: auto; color: var(--text-muted); font-size: 13px; }
	.ps-setup .ps-fields { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
	.ps-setup .ps-actions { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
	.ps-setup .ps-row { display: grid; grid-template-columns: 200px minmax(0, 1fr) auto; gap: 16px; align-items: center;
		padding: 13px 0; border-top: 1px solid var(--border-color); }
	.ps-setup .ps-row b { font-size: 15.5px; }
	.ps-setup .ps-row .ps-detail { color: var(--text-muted); font-size: 14.5px; overflow-wrap: anywhere; }
	.ps-setup .ps-pill { display: inline-flex; align-items: center; gap: 8px; border-radius: 999px; padding: 5px 12px;
		font-size: 13px; font-weight: 500; white-space: nowrap; border: 0; cursor: default; }
	.ps-setup button.ps-pill { cursor: pointer; }
	.ps-setup .ps-pill::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
	.ps-setup .ps-pill.success { background: #E6F4EA; color: #15803D; }
	.ps-setup .ps-pill.warn { background: #FDF3DC; color: #A16207; }
	.ps-setup .ps-pill.danger { background: #FDEAE4; color: #C2410C; }
	.ps-setup .ps-pill.muted { background: var(--control-bg); color: var(--text-muted); }
	.ps-setup .ps-note { background: var(--control-bg); border-radius: 12px; padding: 12px 14px; font-size: 14.5px; }
	.ps-setup .ps-warn { background: #FDF3DC; color: #7a4b05; border-radius: 14px; padding: 16px 18px; }
	.ps-setup code.ps-url { display: block; background: var(--control-bg); border-radius: 9px; padding: 11px 13px; font-size: 13.5px; overflow-wrap: anywhere; }
	@media (max-width: 760px) { .ps-setup .ps-row { grid-template-columns: 1fr; gap: 6px; } }
	`;
	const tag = document.createElement("style");
	tag.id = "ps-setup-style";
	tag.textContent = css;
	document.head.appendChild(tag);
};

payments.setup.esc = (text) => frappe.utils.escape_html(text == null ? "" : String(text));

payments.setup.pill = function (state, text, opts) {
	const tone = payments.setup.PILL[state] || "muted";
	const tag = opts && opts.button ? "button" : "span";
	const extra = opts && opts.action ? ` data-action="${opts.action}"` : "";
	return `<${tag} class="ps-pill ${tone}"${extra}>${payments.setup.esc(text)}</${tag}>`;
};

/**
 * One provider's setup page.
 *
 * `config`:
 *   title, subtitle, method (python module holding connect/get_status), fields (make_control dfs),
 *   links(status) → [[url, label]], rows → {key: {label, text(row, status) → {pill, detail}}},
 *   mode_text(state), panels(status, page) → html, actions(status) → html, on_action(name, page).
 */
payments.setup.Page = class {
	constructor(wrapper, config) {
		this.config = config;
		this.page = frappe.ui.make_app_page({ parent: wrapper, title: config.title, single_column: true });
		payments.setup.style_once();
		this.$body = $(`<div class="ps-setup"></div>`).appendTo(this.page.main);
		this.status = null;
		this.render_shell();
		this.load();
	}

	call(name, args) {
		return frappe.call({
			method: `${this.config.method}.${name}`,
			args: args || {},
			freeze: true,
			freeze_message: __("Talking to {0}…", [this.config.title]),
		});
	}

	render_shell() {
		const esc = payments.setup.esc;
		this.$body.html(`
			<div class="ps-hero">
				<div><h2>${esc(this.config.title)}</h2><p>${esc(this.config.subtitle)}</p></div>
				<div data-zone="mode"></div>
			</div>
			<div class="ps-card">
				<div class="ps-head"><span class="ps-num">1</span><h3>${esc(__("Your {0} account", [this.config.title]))}</h3></div>
				<div class="ps-fields" data-zone="fields"></div>
				<div class="ps-actions">
					<button class="btn btn-primary btn-sm" data-action="connect">${esc(__("Connect"))}</button>
					<span data-zone="links"></span>
				</div>
				<div data-zone="connect-error"></div>
			</div>
			<div class="ps-card">
				<div class="ps-head"><span class="ps-num">2</span><h3>${esc(__("What Neoffice prepared"))}</h3>
					<span class="ps-aside">${esc(__("done on its own, a few seconds after « Connect »"))}</span></div>
				<div data-zone="rows"></div>
			</div>
			<div data-zone="panels" style="display:flex;flex-direction:column;gap:20px"></div>
			<div class="ps-actions" data-zone="actions" style="justify-content:flex-end"></div>
		`);
		this.fields = {};
		const $fields = this.$body.find('[data-zone="fields"]');
		for (const df of this.config.fields) {
			const $slot = $("<div></div>").appendTo($fields);
			this.fields[df.fieldname] = frappe.ui.form.make_control({ parent: $slot, df, render_input: true });
		}
		this.$body.on("click", "[data-action]", (e) => {
			e.preventDefault();
			const action = $(e.currentTarget).data("action");
			if (action === "connect") return this.connect();
			if (action === "check") return this.run("check_online");
			if (action === "mode") return this.show_mode();
			if (action === "copy") return frappe.utils.copy_to_clipboard($(e.currentTarget).data("text"));
			this.config.on_action && this.config.on_action(action, this, e);
		});
	}

	async load() {
		const { message } = await frappe.call({ method: `${this.config.method}.get_status` });
		this.show(message || {});
	}

	async run(name, args) {
		const { message } = await this.call(name, args);
		if (message) this.show(message);
		return message;
	}

	values() {
		const out = {};
		for (const [name, control] of Object.entries(this.fields)) out[name] = control.get_value();
		return out;
	}

	async connect() {
		const { message } = await this.call("connect", this.values());
		if (!message) return;
		this.$body.find('[data-zone="connect-error"]').html(
			message.ok === false
				? `<div class="ps-warn">✗ ${payments.setup.esc(message.error || __("Connection failed"))}</div>`
				: ""
		);
		this.show(message);
	}

	show(status) {
		this.status = status;
		const esc = payments.setup.esc;
		const cfg = this.config;
		this.config.fill_fields && this.config.fill_fields(status, this);
		const mode = status.mode || "unknown";
		this.$body.find('[data-zone="mode"]').html(
			payments.setup.pill(mode, cfg.mode_text(mode), { button: true, action: "mode" })
		);
		this.$body.find('[data-zone="links"]').html(
			(cfg.links(status) || [])
				.map(([url, label]) => `<a href="${esc(url)}" target="_blank" rel="noopener" style="font-weight:700">${esc(label)} ↗</a>`)
				.join(" &nbsp; ")
		);
		this.$body.find('[data-zone="rows"]').html(
			(status.rows || [])
				.map((row) => {
					const spec = cfg.rows[row.key];
					if (!spec) return "";
					const shown = spec.text(row, status);
					return `<div class="ps-row"><b>${esc(spec.label)}</b>
						<span class="ps-detail">${shown.html || esc(shown.detail || "")}</span>
						${payments.setup.pill(row.state, shown.pill)}</div>`;
				})
				.join("")
		);
		this.$body.find('[data-zone="panels"]').html(cfg.panels ? cfg.panels(status, this) : "");
		this.$body.find('[data-zone="actions"]').html(cfg.actions ? cfg.actions(status, this) : "");
	}

	show_mode() {
		const action = (this.config.mode_action && this.config.mode_action(this.status || {}, this)) || {
			label: __("Check now"),
			run: () => this.run("check_online"),
		};
		const d = new frappe.ui.Dialog({
			title: __("Test or production"),
			fields: [{ fieldtype: "HTML", fieldname: "body" }],
			primary_action_label: action.label,
			primary_action: async () => {
				d.hide();
				await action.run();
			},
		});
		d.fields_dict.body.$wrapper.html(this.config.mode_help(this.status || {}));
		d.show();
	}
};

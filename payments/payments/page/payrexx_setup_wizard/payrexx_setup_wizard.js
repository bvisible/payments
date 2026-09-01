//// Neoffice — added file (no upstream equivalent).
////
//// Standing a Payrexx account up by hand means touching six doctypes in the
//// right order, and every miss fails silently: a tile that never appears, or one
//// that appears and cannot take money. This walks through it once, and refuses
//// to build anything before the credentials have actually reached Payrexx.
////
//// Deliberately one page rather than a stepper. There are three decisions —
//// credentials, channels, tiles — and hiding two of them behind "Next" makes a
//// five-minute job feel like a form to endure. Sections unlock as they become
//// answerable.

frappe.pages["payrexx-setup-wizard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Payrexx Setup"),
		single_column: true,
	});
	new PayrexxSetup(page);
};

class PayrexxSetup {
	constructor(page) {
		this.page = page;
		this.$body = $(`<div class="payrexx-setup"></div>`).appendTo(page.main);
		this.connexion_ok = false;
		this.render();
		this.charger();
	}

	appel(methode, args) {
		return frappe.call({
			method: `payments.api.payrexx_setup.${methode}`,
			args: args || {},
			freeze: true,
			freeze_message: __("Talking to Payrexx…"),
		});
	}

	render() {
		this.$body.html(`
			<style>
				.payrexx-setup .card { margin-bottom: var(--margin-lg); }
				.payrexx-setup .etat { font-size: var(--text-sm); }
				.payrexx-setup .tuile { display:flex; align-items:flex-start; gap:8px;
					padding:8px 0; border-bottom:1px solid var(--border-color); }
				.payrexx-setup .tuile:last-child { border-bottom:0; }
				.payrexx-setup .tuile small { color: var(--text-muted); }
			</style>

			<div class="card">
				<div class="card-body">
					<h5>${__("1. Your Payrexx space")}</h5>
					<p class="text-muted etat">${__(
						"Everything else follows from this. It is the part before .payrexx.com in the address of your back office — sign in and read the address bar."
					)}</p>
					<div class="row">
						<div class="col-sm-5"><div class="frappe-control" data-champ="instance"></div></div>
						<div class="col-sm-7 pt-4">
							<a href="https://signup.payrexx.com" target="_blank" rel="noopener"
							   class="btn btn-default btn-xs">${__("No account yet — create one")}</a>
						</div>
					</div>
					<div class="mt-2 etat" data-zone="liens"></div>
				</div>
			</div>

			<div class="card">
				<div class="card-body">
					<h5>${__("2. Credentials")}</h5>
					<p class="text-muted etat">${__(
						"In your back office, under API & Plugins. The POS key is only needed for a card terminal."
					)}</p>
					<div class="row">
						<div class="col-sm-5"><div class="frappe-control" data-champ="api_secret"></div></div>
						<div class="col-sm-5"><div class="frappe-control" data-champ="pos_api_secret"></div></div>
						<div class="col-sm-2"><div class="frappe-control" data-champ="mode"></div></div>
					</div>
					<button class="btn btn-primary btn-sm" data-action="enregistrer">${__("Save and test")}</button>
					<div class="etat mt-3" data-zone="connexion"></div>
				</div>
			</div>

			<div class="card">
				<div class="card-body">
					<h5>${__("3. What Payrexx is used for")}</h5>
					<div class="frappe-control" data-champ="web"></div>
					<div class="frappe-control" data-champ="terminal"></div>
					<button class="btn btn-default btn-sm" data-action="canaux" disabled>${__("Apply")}</button>
					<div class="etat mt-3" data-zone="canaux"></div>
				</div>
			</div>

			<div class="card">
				<div class="card-body">
					<h5>${__("4. What the shop offers")}</h5>
					<p class="text-muted etat">${__(
						"One tile per payment method reads better than a single tile that asks the shopper to choose again on Payrexx's page."
					)}</p>
					<div data-zone="tuiles"></div>
					<button class="btn btn-default btn-sm mt-3" data-action="tuiles" disabled>${__("Create the tiles")}</button>
					<div class="etat mt-3" data-zone="resultat"></div>
				</div>
			</div>
		`);

		this.champs = {};
		const def = [
			["instance", "Data", __("Instance"), __("the part before .payrexx.com")],
			["api_secret", "Password", __("API secret"), ""],
			["pos_api_secret", "Password", __("POS API key"), __("terminal only")],
			["mode", "Select", __("Mode"), ""],
			["web", "Check", __("Online payments"), ""],
			["terminal", "Check", __("Card terminal"), ""],
		];
		for (const [nom, type, label, aide] of def) {
			this.champs[nom] = frappe.ui.form.make_control({
				parent: this.$body.find(`[data-champ="${nom}"]`),
				df: {
					fieldname: nom,
					fieldtype: type,
					label: label,
					description: aide,
					options: type === "Select" ? ["test", "live"] : undefined,
				},
				render_input: true,
			});
		}
		this.champs.mode.set_value("test");
		this.champs.web.set_value(1);

		//// Les liens du back-office ne sont adressables qu'avec le nom de l'espace :
		//// il fait partie du domaine. D'où l'instance en première question — sans
		//// elle on ne peut que dire « allez dans API & Plugins » et laisser le
		//// client chercher. Avec elle, chaque lien tombe sur SA page.
		this.champs.instance.$input.on("input", () => this.rendre_liens());
		this.rendre_liens();

		this.$body.on("click", "[data-action]", (e) => {
			const action = $(e.currentTarget).data("action");
			({
				enregistrer: () => this.enregistrer(),
				canaux: () => this.canaux(),
				tuiles: () => this.tuiles(),
			})[action]();
		});
	}

	//// `valeur` est passée explicitement à l'ouverture : `set_value` est
	//// asynchrone, donc relire le contrôle juste après rend encore le champ
	//// vide et les liens ne s'affichent jamais sur une configuration existante.
	rendre_liens(valeur) {
		const espace = (valeur !== undefined ? valeur : this.champs.instance.get_value() || "").trim();
		const zone = this.$body.find('[data-zone="liens"]');
		if (!espace) {
			zone.html(
				`<span class="text-muted">${__(
					"Enter your space above and the links to your own back office appear here."
				)}</span>`
			);
			return;
		}
		const base = `https://${encodeURIComponent(espace)}.payrexx.com/cadmin/index.php`;
		const liens = [
			[`${base}?cmd=checkout&act=api`, __("API & Plugins — the secrets")],
			[`${base}?cmd=checkout&act=softpos`, __("Tap to Pay")],
			[`${base}?cmd=checkout&act=api&tpl=webhookLogs`, __("Webhook logs")],
			[`https://${encodeURIComponent(espace)}.payrexx.com/cadmin/`, __("Back office")],
		];
		zone.html(
			liens
				.map(
					([url, texte]) =>
						`<a href="${frappe.utils.escape_html(url)}" target="_blank" rel="noopener"
						    class="btn btn-default btn-xs mr-2 mb-1">${frappe.utils.escape_html(texte)} ↗</a>`
				)
				.join("")
		);
	}

	async charger() {
		const { message } = await frappe.call({
			method: "payments.api.payrexx_setup.get_current_setup",
		});
		if (!message) return;
		const p = message.provider;
		if (p) {
			this.champs.instance.set_value(p.instance || "");
			this.rendre_liens(p.instance || "");
			this.champs.mode.set_value(p.mode || "test");
			//// The secrets are never sent back, so the fields stay empty and the
			//// description says they are already stored. Echoing a key into a form
			//// would put it in every browser cache and screenshot from then on.
			for (const [champ, present] of [
				["api_secret", p.has_api_secret],
				["pos_api_secret", p.has_pos_secret],
			]) {
				if (present) {
					this.champs[champ].df.description = __("Already stored — leave empty to keep it");
					this.champs[champ].refresh();
				}
			}
		}
		const canaux = (message.channels || []).map((c) => c.channel);
		this.champs.web.set_value(canaux.includes("payrexx_web") ? 1 : 0);
		this.champs.terminal.set_value(canaux.includes("terminal") ? 1 : 0);

		this.rendre_tuiles(message.tiles || []);
		if (p && p.enabled) this.enregistrer(true);
	}

	rendre_tuiles(existantes) {
		const par_cle = Object.fromEntries(existantes.map((t) => [t.key, t]));
		const def = [
			["card", __("Card"), __("Visa and Mastercard, entered inside the checkout")],
			["twint", "TWINT", __("goes straight to TWINT, no second choice")],
			["all", __("All methods"), __("one tile, the shopper chooses on Payrexx's page")],
		];
		this.$body.find('[data-zone="tuiles"]').html(
			def
				.map(([cle, titre, aide]) => {
					const t = par_cle[cle];
					const etat = t
						? `<small>${__("already there")} — ${frappe.utils.escape_html(t.account)}</small>`
						: `<small>${frappe.utils.escape_html(aide)}</small>`;
					return `<label class="tuile">
						<input type="checkbox" data-tuile="${cle}" ${t ? "checked" : ""}>
						<span><b>${frappe.utils.escape_html(titre)}</b><br>${etat}</span>
					</label>`;
				})
				.join("")
		);
	}

	async enregistrer(silencieux) {
		const instance = this.champs.instance.get_value();
		const secret = this.champs.api_secret.get_value();
		if (!silencieux) {
			if (!instance) return frappe.msgprint(__("The instance is required"));
			const r = await this.appel("save_credentials", {
				instance: instance,
				api_secret: secret,
				pos_api_secret: this.champs.pos_api_secret.get_value(),
				mode: this.champs.mode.get_value(),
			});
			if (!r.message) return;
		}
		const { message } = await this.appel("test_connection");
		this.connexion_ok = !!(message && message.ok);
		this.$body.find('[data-zone="connexion"]').html(
			this.connexion_ok
				? `<div class="text-success">✓ ${__("Connected to {0}", [
						frappe.utils.escape_html(message.instance || ""),
				  ])} — ${__("mode")} <b>${frappe.utils.escape_html(message.mode)}</b><br>
				   <span class="text-muted">${__("Methods on this account")}: ${(message.payment_methods || [])
						.map(frappe.utils.escape_html)
						.join(", ")}</span></div>`
				: `<div class="text-danger">✗ ${frappe.utils.escape_html(
						(message && message.error) || __("Connection failed")
				  )}</div>`
		);
		this.$body.find('[data-action="canaux"], [data-action="tuiles"]').prop("disabled", !this.connexion_ok);

		//// A live account is worth saying out loud: the same click that was
		//// harmless a moment ago now moves real money.
		if (this.connexion_ok && message.mode === "live") {
			this.$body.find('[data-zone="connexion"]').append(
				`<div class="text-warning mt-2">⚠ ${__(
					"This account is live. Payments taken from now on are real."
				)}</div>`
			);
		}
	}

	async canaux() {
		const r = await this.appel("setup_channels", {
			web: this.champs.web.get_value() ? 1 : 0,
			terminal: this.champs.terminal.get_value() ? 1 : 0,
		});
		if (r.message && r.message.ok) {
			this.$body
				.find('[data-zone="canaux"]')
				.html(`<div class="text-success">✓ ${r.message.channels.join(", ")}</div>`);
		}
	}

	async tuiles() {
		const choisies = this.$body
			.find("[data-tuile]:checked")
			.map((i, e) => $(e).data("tuile"))
			.get();
		if (!choisies.length) return frappe.msgprint(__("Pick at least one tile"));

		const r = await this.appel("setup_tiles", { tiles: choisies });
		if (!r.message || !r.message.ok) return;
		this.$body.find('[data-zone="resultat"]').html(
			`<div class="text-success">✓ ${r.message.tiles
				.map((t) => frappe.utils.escape_html(t.gateway))
				.join(" · ")}</div>
			 <div class="text-muted mt-1">${__("Open the shop's payment step to see them.")}</div>`
		);
		this.charger();
	}
}

// Copyright (c) 2026, Neoffice and contributors
// License: MIT. See LICENSE
//
// Unified Payment Terminal onboarding wizard. Four steps:
//   1. Provider              — pick Stripe / Wallee / future PSP
//   2. Location / Config     — provider-specific (Stripe Location vs Wallee Location+Configuration)
//   3. Pairing / Identification — Stripe pairing code OR Wallee terminal name + serial
//   4. Bind to POS Profile   — add row in custom_active_payment_devices
//
// Replaces the legacy `wallee_terminal_wizard` from the wallee_integration app
// and gives Stripe the same UX. Each provider's step components are kept
// separate so adding a new PSP later means writing a new step renderer, not
// editing existing ones.

frappe.pages["payment-terminal-wizard"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Payment Terminal Wizard"),
		single_column: true,
	});

	const wizard = new PaymentTerminalWizard(page);
	wizard.render();
};

class PaymentTerminalWizard {
	constructor(page) {
		this.page = page;
		this.$body = $(page.body);

		this.totalSteps = 4;
		this.currentStep = 1;

		// State carried across steps. Reset on each wizard run.
		this.state = {
			provider: null,        // {name, kind, display_label, mode}
			location: null,        // Stripe: {stripe_location_id, ...} | Wallee: {name (local), wallee_location_id, ...}
			configuration: null,   // Wallee only: {name, wallee_configuration_id, ...}
			pairing_code: null,    // Stripe only
			label: null,           // both: friendly device label
			payment_device: null,  // populated after Step 3 (the created Payment Device name)
			wallee_terminal_id: null,
			pos_profile: null,
			mode_of_payment: null,
		};
	}

	render() {
		this.$body.html(`
			<div class="ptw-wrap">
				<div class="ptw-progress">
					${[
						["1", __("Provider")],
						["2", __("Location")],
						["3", __("Pairing")],
						["4", __("POS Profile")],
					]
						.map(
							([n, label]) => `
						<div class="ptw-step" data-step="${n}">
							<div class="ptw-step-circle">${n}</div>
							<div class="ptw-step-label">${label}</div>
						</div>`
						)
						.join("")}
				</div>
				<div class="ptw-content"></div>
				<div class="ptw-nav">
					<button class="btn btn-default ptw-prev" disabled>${__("Previous")}</button>
					<button class="btn btn-primary ptw-next" disabled>${__("Next")}</button>
				</div>
			</div>
		`);

		this.$content = this.$body.find(".ptw-content");
		this.$prev = this.$body.find(".ptw-prev");
		this.$next = this.$body.find(".ptw-next");

		this.$prev.on("click", () => this.goToStep(this.currentStep - 1));
		this.$next.on("click", () => this.onNext());

		this.goToStep(1);
	}

	updateProgress() {
		this.$body.find(".ptw-step").each((_i, el) => {
			const $el = $(el);
			const step = parseInt($el.attr("data-step"), 10);
			$el.toggleClass("active", step === this.currentStep);
			$el.toggleClass("done", step < this.currentStep);
		});
		this.$prev.prop("disabled", this.currentStep === 1);
	}

	goToStep(step) {
		if (step < 1 || step > this.totalSteps) return;
		this.currentStep = step;
		this.updateProgress();
		this.renderStep();
	}

	renderStep() {
		this.$next.prop("disabled", true).text(__("Next"));
		this.$content.empty();
		if (this.currentStep === 1) this.renderStep1Provider();
		else if (this.currentStep === 2) this.renderStep2Location();
		else if (this.currentStep === 3) this.renderStep3Pairing();
		else if (this.currentStep === 4) this.renderStep4PosProfile();
	}

	// ------------------------------------------------------------------ //
	// Step 1 — Provider
	// ------------------------------------------------------------------ //

	renderStep1Provider() {
		this.$content.html(`
			<h3>${__("Choose a payment provider")}</h3>
			<p class="text-muted">${__("Pick the PSP whose terminal you want to enroll.")}</p>
			<div class="ptw-providers"></div>
		`);
		const $list = this.$content.find(".ptw-providers");

		frappe.call({ method: "payments.payments.page.payment_terminal_wizard.payment_terminal_wizard.list_terminal_providers" })
			.then((r) => {
				const providers = r.message || [];
				if (!providers.length) {
					$list.html(`<div class="alert alert-warning">${__("No enabled Payment Provider found. Create one in /app/payment-provider first.")}</div>`);
					return;
				}
				providers.forEach((p) => {
					const $card = $(`
						<div class="ptw-card ptw-provider-card" data-name="${frappe.utils.escape_html(p.name)}">
							<div class="ptw-card-title">${frappe.utils.escape_html(p.display_label || p.name)}</div>
							<div class="ptw-card-meta">
								<span class="ptw-badge ptw-kind-${p.kind}">${p.kind.toUpperCase()}</span>
								<span class="ptw-badge ptw-mode-${p.mode}">${p.mode}</span>
							</div>
						</div>
					`);
					$card.on("click", () => {
						$list.find(".ptw-provider-card").removeClass("selected");
						$card.addClass("selected");
						this.state.provider = p;
						this.$next.prop("disabled", false);
					});
					if (this.state.provider && this.state.provider.name === p.name) {
						$card.addClass("selected");
						this.$next.prop("disabled", false);
					}
					$list.append($card);
				});
			});
	}

	// ------------------------------------------------------------------ //
	// Step 2 — Location / Config
	// ------------------------------------------------------------------ //

	renderStep2Location() {
		if (!this.state.provider) return this.goToStep(1);

		if (this.state.provider.kind === "stripe") {
			this.renderStep2Stripe();
		} else if (this.state.provider.kind === "wallee") {
			this.renderStep2Wallee();
		} else {
			this.$content.html(`<div class="alert alert-warning">${__("Provider kind not supported yet.")}</div>`);
		}
	}

	renderStep2Stripe() {
		this.$content.html(`
			<h3>${__("Stripe Location")}</h3>
			<p class="text-muted">${__("Pick an existing Stripe Terminal Location or create a new one.")}</p>
			<div class="ptw-locations"></div>
			<button class="btn btn-sm btn-default ptw-new-loc">${__("+ New location")}</button>
		`);
		const $list = this.$content.find(".ptw-locations");

		const load = () => {
			$list.html(`<div class="text-muted">${__("Loading…")}</div>`);
			frappe.call({
				method: "payments.api.terminal.list_stripe_locations",
				args: { provider: this.state.provider.name },
			}).then((r) => {
				const locs = r.message || [];
				$list.empty();
				if (!locs.length) {
					$list.html(`<div class="text-muted">${__("No Stripe Locations found — create one below.")}</div>`);
					return;
				}
				locs.forEach((loc) => {
					const $card = $(`
						<div class="ptw-card" data-id="${frappe.utils.escape_html(loc.stripe_location_id)}">
							<div class="ptw-card-title">${frappe.utils.escape_html(loc.display_name)}</div>
							<div class="ptw-card-meta">${frappe.utils.escape_html(JSON.stringify(loc.address))}</div>
						</div>
					`);
					$card.on("click", () => {
						$list.find(".ptw-card").removeClass("selected");
						$card.addClass("selected");
						this.state.location = loc;
						this.$next.prop("disabled", false);
					});
					if (this.state.location && this.state.location.stripe_location_id === loc.stripe_location_id) {
						$card.addClass("selected");
						this.$next.prop("disabled", false);
					}
					$list.append($card);
				});
			});
		};

		this.$content.find(".ptw-new-loc").on("click", () => this.openStripeLocationDialog(load));
		load();
	}

	openStripeLocationDialog(onCreated) {
		const d = new frappe.ui.Dialog({
			title: __("Create Stripe Location"),
			fields: [
				{ fieldname: "display_name", fieldtype: "Data", label: __("Name"), reqd: 1 },
				{ fieldname: "line1", fieldtype: "Data", label: __("Address line 1"), reqd: 1 },
				{ fieldname: "city", fieldtype: "Data", label: __("City"), reqd: 1 },
				{ fieldname: "postal_code", fieldtype: "Data", label: __("Postal code"), reqd: 1 },
				{ fieldname: "country", fieldtype: "Data", label: __("Country (ISO-2)"), default: "CH", reqd: 1 },
			],
			primary_action_label: __("Create"),
			primary_action: (values) => {
				frappe.call({
					method: "payments.api.terminal.create_stripe_location",
					args: { ...values, provider: this.state.provider.name },
				}).then((r) => {
					d.hide();
					frappe.show_alert({ message: __("Location created"), indicator: "green" });
					onCreated && onCreated();
				});
			},
		});
		d.show();
	}

	renderStep2Wallee() {
		this.$content.html(`
			<h3>${__("Wallee Location & Configuration")}</h3>
			<p class="text-muted">${__("Pick a Wallee Location and Terminal Configuration. Use 'Sync from Wallee' if your local list is empty.")}</p>
			<div class="ptw-two-col">
				<div>
					<div class="ptw-col-title">${__("Location")}</div>
					<div class="ptw-locations"></div>
					<button class="btn btn-sm btn-default ptw-sync-locs">${__("Sync from Wallee")}</button>
				</div>
				<div>
					<div class="ptw-col-title">${__("Configuration")}</div>
					<div class="ptw-configs"></div>
					<button class="btn btn-sm btn-default ptw-sync-cfgs">${__("Sync from Wallee")}</button>
				</div>
			</div>
		`);

		const loadLocs = () => {
			const $l = this.$content.find(".ptw-locations");
			$l.html(`<div class="text-muted">${__("Loading…")}</div>`);
			frappe.call({
				method: "payments.integrations.wallee.api.get_existing_locations",
				args: { provider: this.state.provider.name },
			}).then((r) => this.renderWalleeList($l, r.message || [], "location_name", (item) => {
				this.state.location = item;
				this.maybeEnableNext();
			}));
		};
		const loadCfgs = () => {
			const $c = this.$content.find(".ptw-configs");
			$c.html(`<div class="text-muted">${__("Loading…")}</div>`);
			frappe.call({
				method: "payments.integrations.wallee.api.get_existing_configurations",
				args: { provider: this.state.provider.name },
			}).then((r) => this.renderWalleeList($c, r.message || [], "configuration_name", (item) => {
				this.state.configuration = item;
				this.maybeEnableNext();
			}));
		};
		this.$content.find(".ptw-sync-locs").on("click", () => {
			frappe.call({ method: "payments.integrations.wallee.api.sync_locations_from_wallee", args: { provider: this.state.provider.name } })
				.then((r) => {
					frappe.show_alert({ message: __("Synced {0} locations", [r.message.total]), indicator: "green" });
					loadLocs();
				});
		});
		this.$content.find(".ptw-sync-cfgs").on("click", () => {
			frappe.call({ method: "payments.integrations.wallee.api.sync_configurations_from_wallee", args: { provider: this.state.provider.name } })
				.then((r) => {
					frappe.show_alert({ message: __("Synced {0} configurations", [r.message.total]), indicator: "green" });
					loadCfgs();
				});
		});
		loadLocs();
		loadCfgs();
	}

	renderWalleeList($container, items, label_field, onPick) {
		$container.empty();
		if (!items.length) {
			$container.html(`<div class="text-muted">${__("Nothing yet — sync first.")}</div>`);
			return;
		}
		items.forEach((it) => {
			const $card = $(`
				<div class="ptw-card" data-name="${frappe.utils.escape_html(it.name)}">
					<div class="ptw-card-title">${frappe.utils.escape_html(it[label_field] || it.name)}</div>
					<div class="ptw-card-meta">${frappe.utils.escape_html(it.wallee_location_id || it.wallee_configuration_id || "")}</div>
				</div>
			`);
			$card.on("click", () => {
				$container.find(".ptw-card").removeClass("selected");
				$card.addClass("selected");
				onPick(it);
			});
			$container.append($card);
		});
	}

	maybeEnableNext() {
		if (this.state.provider.kind === "wallee") {
			this.$next.prop("disabled", !(this.state.location && this.state.configuration));
		} else if (this.state.provider.kind === "stripe") {
			this.$next.prop("disabled", !this.state.location);
		}
	}

	// ------------------------------------------------------------------ //
	// Step 3 — Pairing / Identification
	// ------------------------------------------------------------------ //

	renderStep3Pairing() {
		if (this.state.provider.kind === "stripe") this.renderStep3Stripe();
		else if (this.state.provider.kind === "wallee") this.renderStep3Wallee();
		else this.$content.html(`<div class="alert alert-warning">${__("Provider kind not supported.")}</div>`);
	}

	renderStep3Stripe() {
		this.$content.html(`
			<h3>${__("Stripe Reader pairing")}</h3>
			<p class="text-muted">
				${__("On the BBPOS WisePOS E: swipe from the LEFT edge → Settings → admin code 07139 → 'Generate pairing code'. You'll get three dash-separated words (valid ~10 minutes).")}
			</p>
			<div class="ptw-form">
				<label>${__("Pairing code")}</label>
				<input type="text" class="form-control ptw-input-code" placeholder="cool-cyan-fox" />
				<label class="mt-3">${__("Friendly label")}</label>
				<input type="text" class="form-control ptw-input-label" placeholder="${__("Reader counter 1")}" />
				<button class="btn btn-primary mt-3 ptw-register-stripe">${__("Register reader")}</button>
				<div class="ptw-status mt-3"></div>
			</div>
		`);

		this.$content.find(".ptw-register-stripe").on("click", () => {
			const code = this.$content.find(".ptw-input-code").val().trim();
			const label = this.$content.find(".ptw-input-label").val().trim();
			if (!code) return;
			const $status = this.$content.find(".ptw-status");
			$status.html(`<div class="text-muted">${__("Calling Stripe…")}</div>`);
			frappe.call({
				method: "payments.api.terminal.register_stripe_reader",
				args: {
					registration_code: code,
					location: this.state.location.stripe_location_id,
					label: label || null,
					device_label: label || null,
					provider: this.state.provider.name,
				},
			}).then((r) => {
				if (!r.message) {
					$status.html(`<div class="alert alert-danger">${__("Registration failed (no response)")}</div>`);
					return;
				}
				this.state.payment_device = r.message.payment_device;
				this.state.label = label;
				$status.html(`<div class="alert alert-success">${__("Reader registered: {0}", [r.message.stripe_reader_id])}</div>`);
				this.$next.prop("disabled", false);
			}).catch(() => {
				$status.html(`<div class="alert alert-danger">${__("Registration failed — see error popup")}</div>`);
			});
		});
	}

	renderStep3Wallee() {
		this.$content.html(`
			<h3>${__("Wallee Terminal — create + link")}</h3>
			<p class="text-muted">${__("Create the terminal in Wallee with the chosen configuration and location, then pair the physical device by its serial number.")}</p>
			<div class="ptw-form">
				<label>${__("Terminal name (Wallee dashboard)")}</label>
				<input type="text" class="form-control ptw-input-name" placeholder="Terminal Shop 1" />
				<label class="mt-3">${__("Friendly label (Payment Device)")}</label>
				<input type="text" class="form-control ptw-input-label" placeholder="${__("Reader counter 1")}" />
				<label class="mt-3">${__("Device serial number")}</label>
				<input type="text" class="form-control ptw-input-serial" placeholder="WPE-XXXXXXXX" />
				<button class="btn btn-primary mt-3 ptw-create-wallee">${__("Create + link")}</button>
				<div class="ptw-status mt-3"></div>
			</div>
		`);

		this.$content.find(".ptw-create-wallee").on("click", () => {
			const name = this.$content.find(".ptw-input-name").val().trim();
			const label = this.$content.find(".ptw-input-label").val().trim();
			const serial = this.$content.find(".ptw-input-serial").val().trim();
			if (!name || !serial) return;
			const $status = this.$content.find(".ptw-status");
			$status.html(`<div class="text-muted">${__("Creating terminal in Wallee…")}</div>`);
			frappe.call({
				method: "payments.integrations.wallee.api.create_terminal",
				args: {
					provider: this.state.provider.name,
					terminal_name: name,
					configuration: this.state.configuration.name,
					location: this.state.location.name,
					device_label: label || name,
				},
			}).then((r) => {
				if (!r.message) {
					$status.html(`<div class="alert alert-danger">${__("Creation failed (no response)")}</div>`);
					return;
				}
				this.state.payment_device = r.message.payment_device;
				this.state.wallee_terminal_id = r.message.wallee_terminal_id;
				this.state.label = label;
				$status.html(`<div class="text-muted">${__("Terminal created. Linking serial {0}…", [serial])}</div>`);
				return frappe.call({
					method: "payments.integrations.wallee.api.link_terminal_device",
					args: {
						provider: this.state.provider.name,
						payment_device: r.message.payment_device,
						serial_number: serial,
					},
				});
			}).then((r2) => {
				if (r2 && r2.message && r2.message.linked) {
					$status.html(`<div class="alert alert-success">${__("Terminal {0} linked to device serial {1}", [this.state.wallee_terminal_id, serial])}</div>`);
					this.$next.prop("disabled", false);
				}
			}).catch(() => {
				$status.html(`<div class="alert alert-danger">${__("Creation or linking failed — see error popup")}</div>`);
			});
		});
	}

	// ------------------------------------------------------------------ //
	// Step 4 — Bind to POS Profile
	// ------------------------------------------------------------------ //

	renderStep4PosProfile() {
		this.$content.html(`
			<h3>${__("Bind to a POS Profile")}</h3>
			<p class="text-muted">${__("This adds your new Payment Device to the profile's Active Payment Methods & Terminals table. Optional — you can skip this step and configure it later from POS Profile directly.")}</p>
			<div class="ptw-form">
				<label>${__("POS Profile")}</label>
				<select class="form-control ptw-input-profile"><option value="">${__("— Choose a profile —")}</option></select>
				<label class="mt-3">${__("Mode of Payment")}</label>
				<select class="form-control ptw-input-mop" disabled><option value="">${__("— Pick a profile first —")}</option></select>
				<label class="mt-3"><input type="checkbox" class="ptw-input-default" /> ${__("Mark as default")}</label>
				<button class="btn btn-primary mt-3 ptw-bind">${__("Bind to profile")}</button>
				<button class="btn btn-default mt-3 ptw-skip">${__("Skip — finish wizard")}</button>
				<div class="ptw-status mt-3"></div>
			</div>
		`);

		const $profile = this.$content.find(".ptw-input-profile");
		const $mop = this.$content.find(".ptw-input-mop");

		frappe.call({ method: "payments.payments.page.payment_terminal_wizard.payment_terminal_wizard.list_pos_profiles" })
			.then((r) => {
				(r.message || []).forEach((p) => {
					$profile.append(`<option value="${frappe.utils.escape_html(p.name)}">${frappe.utils.escape_html(p.name)}</option>`);
				});
			});

		$profile.on("change", () => {
			const prof = $profile.val();
			$mop.empty().append(`<option value="">${__("— Choose a Mode of Payment —")}</option>`).prop("disabled", !prof);
			if (!prof) return;
			frappe.call({
				method: "payments.payments.page.payment_terminal_wizard.payment_terminal_wizard.list_modes_of_payment_for_profile",
				args: { pos_profile: prof },
			}).then((r) => {
				(r.message || []).forEach((m) => {
					$mop.append(`<option value="${frappe.utils.escape_html(m.name)}">${frappe.utils.escape_html(m.name)}</option>`);
				});
			});
		});

		this.$content.find(".ptw-bind").on("click", () => {
			const prof = $profile.val();
			const mop = $mop.val();
			if (!prof || !mop) return;
			const isDefault = this.$content.find(".ptw-input-default").is(":checked") ? 1 : 0;
			const $status = this.$content.find(".ptw-status");
			frappe.call({
				method: "payments.payments.page.payment_terminal_wizard.payment_terminal_wizard.link_device_to_pos_profile",
				args: {
					payment_device: this.state.payment_device,
					pos_profile: prof,
					mode_of_payment: mop,
					is_default: isDefault,
				},
			}).then((r) => {
				if (r.message && r.message.row) {
					$status.html(`<div class="alert alert-success">${__("Bound to {0}", [prof])}</div>`);
					this.$next.text(__("Done")).prop("disabled", false);
					this.$next.off("click").on("click", () => frappe.set_route("payment-device", this.state.payment_device));
				}
			});
		});

		this.$content.find(".ptw-skip").on("click", () => {
			frappe.set_route("payment-device", this.state.payment_device);
		});
	}

	// ------------------------------------------------------------------ //
	// Navigation
	// ------------------------------------------------------------------ //

	onNext() {
		if (this.currentStep < this.totalSteps) {
			this.goToStep(this.currentStep + 1);
		}
	}
}

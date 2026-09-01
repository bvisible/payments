//// Neoffice — added file (no upstream equivalent).
////
//// One list for every payment, whatever took it. Without this the list reads as
//// a wall of identical rows: `60195` where a reader expects `CHF 601.95`, and a
//// status column with no colour, so nothing tells you at a glance which payments
//// need attention and which are done.

frappe.listview_settings["Payment Intent"] = {
	add_fields: ["status", "amount", "currency", "provider", "channel"],

	//// The states that matter to whoever is looking, in the colours Frappe uses
	//// everywhere else — red is a problem, orange is waiting on someone, green is
	//// money in. `requires_action` and `processing` are deliberately not grey:
	//// a payment stuck in either is the one thing worth noticing.
	get_indicator(doc) {
		const couleurs = {
			succeeded: ["Encaissé", "green"],
			refunded: ["Remboursé", "blue"],
			processing: ["En cours", "orange"],
			requires_action: ["Attente client", "orange"],
			failed: ["Échoué", "red"],
			canceled: ["Annulé", "gray"],
			created: ["Créé", "gray"],
		};
		const [libelle, couleur] = couleurs[doc.status] || [doc.status, "gray"];
		return [__(libelle), couleur, "status,=," + doc.status];
	},

	formatters: {
		//// Amounts are stored in minor units everywhere in this app — a deliberate
		//// choice, since a float amount is the one arithmetic bug that costs real
		//// money. The list is where that choice stops being helpful, so it is
		//// undone here and nowhere else.
		amount(value, df, doc) {
			if (value === null || value === undefined) return "";
			return format_currency(flt(value) / 100, doc.currency || "CHF");
		},

		//// Which gateway, and how it was taken. Two facts a reader wants together
		//// and would otherwise have to open the record to get.
		provider(value, df, doc) {
			const canal = (doc.channel || "").replace(/_/g, " ");
			return `${value} <span class="text-muted small">${canal}</span>`;
		},
	},
};

// Overlay a red "Wave: Needs Review" indicator on Sales Order list rows flagged for review.
//
// Wraps any indicator ERPNext (or another app) already supplies so the standard
// To Deliver and Bill / Completed / Cancelled colouring is preserved for rows that
// aren't flagged. Clicking the chip filters the list to all flagged rows.

(function () {
	const existing = frappe.listview_settings['Sales Order'] || {};
	const previous_indicator = existing.get_indicator;
	const previous_add_fields = existing.add_fields || [];

	frappe.listview_settings['Sales Order'] = Object.assign({}, existing, {
		add_fields: [...previous_add_fields, 'wave_manual_review_required'],
		get_indicator: function (doc) {
			if (doc.wave_manual_review_required) {
				return [
					__("Wave: Needs Review"),
					"red",
					"wave_manual_review_required,=,1",
				];
			}
			return previous_indicator ? previous_indicator(doc) : null;
		},
	});
})();

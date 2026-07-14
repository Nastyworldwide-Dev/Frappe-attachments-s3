// Copyright (c) 2018, Frappe and contributors
// For license information, please see license.txt

frappe.ui.form.on('S3 File Attachment', {
	refresh: function (frm) {
		if (frm.__s3_realtime_bound) return;
		frm.__s3_realtime_bound = true;
		console.info('[S3Attachment] binding migration realtime listeners');
		frappe.realtime.on('s3_migration_progress', function (d) {
			frappe.show_progress(
				__('Migrating files to S3'),
				d.current,
				d.total,
				d.file_name
			);
		});
		frappe.realtime.on('s3_migration_complete', function (d) {
			console.info('[S3Attachment] migration complete', d);
			frappe.hide_progress();
			show_migration_summary(d);
		});
	},

	migrate_existing_files: function (frm) {
		console.info('[S3Attachment] migration requested');
		frappe.call({
			method: 'frappe_s3_attachment.controller.get_migration_summary',
			callback: function (r) {
				const s = r.message;
				if (!s) return;
				if (s.running) {
					frappe.msgprint(
						__('A migration job is already running — the button is locked to prevent duplicate jobs.')
					);
					return;
				}
				if (!s.total_local) {
					frappe.msgprint(__('No local files to migrate.'));
					return;
				}
				confirm_and_start(s);
			},
		});
	},
});

function confirm_and_start(s) {
	console.info('[S3Attachment] showing migration confirm dialog', s);
	const lines = [__('{0} file(s) will be uploaded to S3.', [s.will_upload])];
	if (s.skipped_ignored) {
		lines.push(
			__('{0} file(s) skipped — attached to an ignored doctype.', [
				s.skipped_ignored,
			])
		);
	}
	if (s.unattached) {
		lines.push(
			__('{0} unattached file(s) will be filed under /File/.', [s.unattached])
		);
	}
	if (s.shared_local) {
		lines.push(
			__('{0} file(s) share a local copy and will be handled safely.', [
				s.shared_local,
			])
		);
	}
	lines.push(
		`<b>${__('Local copies are deleted only after successful upload and commit.')}</b>`
	);

	const d = new frappe.ui.Dialog({
		title: __('Migrate existing files to S3?'),
		primary_action_label: __('Start Migration'),
		primary_action() {
			d.hide();
			start_migration();
		},
	});
	d.$body.html(lines.map((l) => `<p>${l}</p>`).join(''));
	d.show();
}

function start_migration() {
	frappe.call({
		method: 'frappe_s3_attachment.controller.migrate_existing_files',
		callback: function (r) {
			const queued = r.message && r.message.status === 'queued';
			console.info('[S3Attachment] migration enqueue result', r.message);
			if (queued) {
				frappe.show_alert({
					message: __('Migration started in the background.'),
					indicator: 'blue',
				});
			} else {
				frappe.msgprint(__('A migration job is already running.'));
			}
		},
	});
}

function show_migration_summary(d) {
	console.info('[S3Attachment] rendering migration summary', d);
	const esc = frappe.utils.escape_html;
	let html = `<p><b>${__('Migrated')}:</b> ${d.migrated} · <b>${__(
		'Skipped'
	)}:</b> ${d.skipped} · <b>${__('Failed')}:</b> ${d.failed}</p>`;
	if (d.failed && d.failures && d.failures.length) {
		const rows = d.failures
			.map(
				(f) =>
					`<tr><td>${esc(f.file || '')}</td><td>${esc(f.reason || '')}</td></tr>`
			)
			.join('');
		html += `<table class="table table-bordered"><thead><tr><th>${__(
			'File'
		)}</th><th>${__('Reason')}</th></tr></thead><tbody>${rows}</tbody></table>`;
	}
	frappe.msgprint({
		title: __('Migration complete'),
		message: html,
		indicator: d.failed ? 'orange' : 'green',
	});
}

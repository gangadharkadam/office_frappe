# Copyright (c) 2013, Web Notes Technologies Pvt. Ltd. and Contributors
# MIT License. See license.txt

from __future__ import unicode_literals

import frappe
import os, json, re

from frappe import _
from frappe.modules import scrub, get_module_path
from frappe.utils import flt, cint
import frappe.widgets.reportview

def get_report_doc(report_name):
	doc = frappe.get_doc("Report", report_name)
	if not doc.has_permission("read"):
		raise frappe.PermissionError("You don't have access to: {report}".format(report=report_name))

	if not frappe.has_permission(doc.ref_doctype, "report"):
		raise frappe.PermissionError("You don't have access to get a report on: {doctype}".format(
			doctype=doc.ref_doctype))

	return doc

@frappe.whitelist()
def get_script(report_name):
	report = get_report_doc(report_name)

	module = report.module or frappe.db.get_value("DocType", report.ref_doctype, "module")
	module_path = get_module_path(module)
	report_folder = os.path.join(module_path, "report", scrub(report.name))
	script_path = os.path.join(report_folder, scrub(report.name) + ".js")
	print_path = os.path.join(report_folder, scrub(report.name) + ".html")

	script = None
	if os.path.exists(script_path):
		with open(script_path, "r") as f:
			script = f.read()

	html_format = get_html_format(print_path, report, module)

	if not script and report.javascript:
		script = report.javascript

	if not script:
		script = "frappe.query_reports['%s']={}" % report_name

	# load translations
	if frappe.lang != "en":
		frappe.response["__messages"] = frappe.get_lang_dict("report", report_name)

	return {
		"script": script,
		"html_format": html_format
	}

def get_html_format(print_path, report, module):
	html_format = None
	if os.path.exists(print_path):
		with open(print_path, "r") as f:
			html_format = f.read()

		for include_directive, path in re.findall("""({% include ['"]([^'"]*)['"] %})""", html_format):
			for app_name in frappe.get_installed_apps():
				include_path = frappe.get_app_path(app_name, *path.split(os.path.sep))
				if os.path.exists(include_path):
					with open(include_path, "r") as f:
						html_format = html_format.replace(include_directive, f.read())
					break

	return html_format

@frappe.whitelist()
def run(report_name, filters=()):
	report = get_report_doc(report_name)

	if filters and isinstance(filters, basestring):
		filters = json.loads(filters)

	if not frappe.has_permission(report.ref_doctype, "report"):
		frappe.msgprint(_("Must have report permission to access this report."),
			raise_exception=True)

	if report.report_type=="Query Report":
		if not report.query:
			frappe.msgprint(_("Must specify a Query to run"), raise_exception=True)


		if not report.query.lower().startswith("select"):
			frappe.msgprint(_("Query must be a SELECT"), raise_exception=True)

		result = [list(t) for t in frappe.db.sql(report.query, filters)]
		columns = [c[0] for c in frappe.db.get_description()]
	else:
		module = report.module or frappe.db.get_value("DocType", report.ref_doctype, "module")
		if report.is_standard=="Yes":
			method_name = get_report_module_dotted_path(module, report.name) + ".execute"
			columns, result = frappe.get_attr(method_name)(frappe._dict(filters))

	if report.apply_user_permissions:
		result = get_filtered_data(report.ref_doctype, columns, result)

	if cint(report.add_total_row) and result:
		result = add_total_row(result, columns)

	return {
		"result": result,
		"columns": columns
	}

def get_report_module_dotted_path(module, report_name):
	return frappe.local.module_app[scrub(module)] + "." + scrub(module) \
		+ ".report." + scrub(report_name) + "." + scrub(report_name)

def add_total_row(result, columns):
	total_row = [""]*len(columns)
	has_percent = []
	for row in result:
		for i, col in enumerate(columns):
			fieldtype = None
			if isinstance(col, basestring):
				col = col.split(":")
				if len(col) > 1:
					fieldtype = col[1]
			else:
				fieldtype = col.get("fieldtype")

			if fieldtype in ["Currency", "Int", "Float", "Percent"] and flt(row[i]):
				total_row[i] = flt(total_row[i]) + flt(row[i])
			if fieldtype == "Percent" and i not in has_percent:
				has_percent.append(i)

	for i in has_percent:
		total_row[i] = total_row[i] / len(result)

	first_col_fieldtype = None
	if isinstance(columns[0], basestring):
		first_col = columns[0].split(":")
		if len(first_col) > 1:
			first_col_fieldtype = first_col[1]
	else:
		first_col_fieldtype = columns[0].get("fieldtype")

	if first_col_fieldtype not in ["Currency", "Int", "Float", "Percent"]:
		total_row[0] = "Total"

	result.append(total_row)
	return result

def get_filtered_data(ref_doctype, columns, data):
	result = []

	linked_doctypes = get_linked_doctypes(columns)
	match_filters = get_user_match_filters(linked_doctypes, ref_doctype)

	if match_filters:
		matched_columns = get_matched_columns(linked_doctypes, match_filters)
		for row in data:
			match = True

			if not ("owner" in match_filters and matched_columns.get("user", None)==match_filters["owner"]):
				for col, idx in matched_columns.items():
					if row[idx] not in match_filters[col]:
						match = False
						break

			if match:
				result.append(row)
	else:
		for row in data:
			result.append(row)

	return result

def get_linked_doctypes(columns):
	linked_doctypes = {}

	for idx, col in enumerate(columns):
		if isinstance(col, basestring):
			col = col.split(":")
			if len(col) > 1 and col[1].startswith("Link"):
				link_dt = col[1].split("/")[1]
				linked_doctypes[link_dt] = idx

		# dict
		elif col.get("fieldtype")=="Link" and col.get("options"):
			linked_doctypes[col["options"]] = idx

	return linked_doctypes

def get_user_match_filters(doctypes, ref_doctype):
	match_filters = {}

	for dt in doctypes:
		match_filters.update(frappe.widgets.reportview.build_match_conditions(dt, False))

	return match_filters

def get_matched_columns(linked_doctypes, match_filters):
	if "owner" in match_filters:
		match_filters["user"] = match_filters["owner"]

	col_idx_map = {}
	for dt, idx in linked_doctypes.items():
		link_field = dt.lower().replace(" ", "_")
		if link_field in match_filters:
			col_idx_map[link_field] = idx

	return col_idx_map

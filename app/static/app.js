/* AirLife Regulatory Affairs — one application.
 *
 * Ported from the reviewed prototype. The screens and the stylesheet carry
 * over; what changed is underneath:
 *
 *   - One navigation. The DMA / RIM split and its landing page are gone.
 *   - No approval workflow. A change applies when you make it.
 *   - Nothing is kept in localStorage. Every read and write goes to the API,
 *     and every write carries a reason that lands in the audit trail.
 *
 * Render pattern is the prototype's: build HTML from state, replace the DOM,
 * re-attach handlers. It is not efficient and does not need to be.
 */
(function () {
  "use strict";

  // ---------------------------------------------------------------- state

  var state = {
    route: "/",
    user: null,
    health: null,
    loading: true,
    error: null,
    registrations: [],
    gtins: [],
    holds: [],
    forms: [],
    openReg: null,
    openRegDetail: null,
    openRegHistory: null,
    openForm: null,
    editing: false,
    filters: { country: "", product_class: "", status_filter: "", search: "" },
    modal: null
  };

  var NAV = [
    ["/", "Home"],
    ["/registrations", "Registrations"],
    ["/gtins", "GTIN Lookup"],
    ["/holds", "Holds"],
    ["/intake", "Request Forms"]
  ];

  var STATUSES = ["Active", "Expiring soon", "Expired", "Pending", "In progress"];

  // ------------------------------------------------------------- helpers

  function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function dash(value) {
    if (value === null || value === undefined || value === "") {
      return '<span class="muted">&mdash;</span>';
    }
    return esc(value);
  }

  function fmtDate(iso) {
    if (!iso) return "";
    var d = new Date(String(iso).slice(0, 10) + "T00:00:00");
    if (isNaN(d.getTime())) return String(iso);
    var months = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];
    return ("0" + d.getDate()).slice(-2) + "-" + months[d.getMonth()] + "-" + d.getFullYear();
  }

  function fmtWhen(iso) {
    if (!iso) return "";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return String(iso);
    return fmtDate(d.toISOString()) + " " + d.toTimeString().slice(0, 5);
  }

  function statusPill(status) {
    var map = {
      "Active": "good", "Pending": "pending", "Expiring soon": "warn",
      "Expired": "bad", "In progress": "neutral", "Archived": "neutral"
    };
    return '<span class="pill ' + (map[status] || "neutral") +
      '"><span class="dot"></span>' + esc(status || "Unknown") + "</span>";
  }

  function canEdit() {
    return !!(state.user && state.user.can_edit);
  }

  // ----------------------------------------------------------------- api

  function api(path, options) {
    options = options || {};
    var init = { method: options.method || "GET", headers: {} };
    if (options.body !== undefined) {
      init.headers["Content-Type"] = "application/json";
      init.body = JSON.stringify(options.body);
    }
    if (options.form) {
      init.body = options.form;
      delete init.headers["Content-Type"];
    }
    return fetch(path, init).then(function (response) {
      return response.text().then(function (text) {
        var payload = null;
        try { payload = text ? JSON.parse(text) : null; } catch (e) { payload = { detail: text }; }
        if (!response.ok) {
          var detail = payload && payload.detail;
          if (Array.isArray(detail)) {
            // FastAPI validation errors
            detail = detail.map(function (d) { return d.msg || JSON.stringify(d); }).join("; ");
          }
          var err = new Error(detail || ("Request failed (" + response.status + ")"));
          err.status = response.status;
          throw err;
        }
        return payload;
      });
    });
  }

  function toast(kind, title, message) {
    var wrap = document.getElementById("toast");
    var el = document.createElement("div");
    el.className = "toast " + kind;
    el.innerHTML = "<b>" + esc(title) + "</b>" + (message ? esc(message) : "");
    wrap.appendChild(el);
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 6000);
  }

  function fail(err) {
    toast("bad", err.status === 403 ? "Not permitted" : "Something went wrong", err.message);
  }

  // ------------------------------------------------------ the reason gate
  //
  // Nothing approves a change here, so the reason is the only account of why
  // it happened. It is asked for in one place so it cannot be skipped by a
  // screen that forgot.

  function askReason(title, subtitle, onConfirm, extraHtml) {
    state.modal = {
      kind: "reason", title: title, subtitle: subtitle,
      onConfirm: onConfirm, extraHtml: extraHtml || ""
    };
    render();
  }

  function closeModal() {
    state.modal = null;
    render();
  }

  function modalHtml() {
    var m = state.modal;
    if (!m) return "";
    if (m.kind === "reason") {
      return '<div class="modal-scrim" id="modalScrim"><div class="modal">' +
        '<div class="modal-head"><h3>' + esc(m.title) + "</h3>" +
        (m.subtitle ? '<div class="sub">' + esc(m.subtitle) + "</div>" : "") +
        "</div>" +
        '<div class="modal-body">' + m.extraHtml +
          "<label><b>Reason for this change</b></label>" +
          '<textarea id="reasonText" placeholder="What changed and why, in a sentence."></textarea>' +
          '<div class="reason-note">Recorded against your name in the audit trail. ' +
          "Nothing approves this change, so this is the only record of why it was made.</div>" +
        "</div>" +
        '<div class="modal-foot">' +
          '<button class="btn" id="modalCancel">Cancel</button>' +
          '<button class="btn primary" id="modalConfirm">Save change</button>' +
        "</div></div></div>";
    }
    return "";
  }

  function wireModal() {
    var m = state.modal;
    if (!m) return;
    var scrim = document.getElementById("modalScrim");
    var cancel = document.getElementById("modalCancel");
    var confirm = document.getElementById("modalConfirm");
    if (scrim) scrim.addEventListener("click", function (ev) {
      if (ev.target === scrim) closeModal();
    });
    if (cancel) cancel.addEventListener("click", closeModal);
    if (confirm) confirm.addEventListener("click", function () {
      var box = document.getElementById("reasonText");
      var reason = box ? box.value.trim() : "";
      if (reason.length < 3) {
        toast("bad", "A reason is required", "Say in a few words why this is changing.");
        if (box) box.focus();
        return;
      }
      var fn = m.onConfirm;
      state.modal = null;
      fn(reason);
    });
    var text = document.getElementById("reasonText");
    if (text) text.focus();
  }

  // --------------------------------------------------------------- views

  function pageHome() {
    var regs = state.registrations;
    var counts = { Active: 0, "Expiring soon": 0, Expired: 0, Pending: 0, "In progress": 0 };
    regs.forEach(function (r) {
      if (counts[r.status] !== undefined) counts[r.status]++;
    });
    var pendingForms = state.forms.filter(function (f) { return f.state === "parsed"; }).length;
    var activeHolds = state.holds.length;

    function tile(label, value, note, bad) {
      return '<div><div class="stat-label">' + esc(label) + "</div>" +
        '<div class="stat-value"' + (bad ? ' style="color:var(--bad-fg)"' : "") + ">" +
        esc(value) + "</div>" +
        '<div class="proto-note">' + esc(note) + "</div></div>";
    }

    return '<div class="card home-hero">' +
      "<h1>Regulatory Affairs</h1>" +
      "<p class=\"muted\">Country registrations, GTIN authorization and shipping eligibility, " +
      "holds, and intake of completed Registration Request Forms.</p>" +
      '<p class="proto-note">Changes apply immediately &mdash; nothing here is waiting on an ' +
      "approval. Every change records who made it and why.</p>" +
      "</div>" +

      '<div class="card"><div class="stats">' +
        tile("Registrations", regs.length, "Not archived") +
        tile("Active", counts.Active, "In force today") +
        tile("Expiring ≤ 90 days", counts["Expiring soon"], "Renewal window", counts["Expiring soon"] > 0) +
        tile("Expired", counts.Expired, "No longer in force", counts.Expired > 0) +
        tile("Pending", counts.Pending + counts["In progress"], "Submitted or in progress") +
        tile("Active holds", activeHolds, "GTINs blocked", activeHolds > 0) +
        tile("Forms to review", pendingForms, "Uploaded, not yet accepted", pendingForms > 0) +
      "</div></div>" +

      '<div class="card"><h3 style="margin-bottom:8px">Not yet wired up</h3>' +
      '<p class="muted" style="margin-bottom:10px">These screens exist in the reviewed ' +
      "prototype but have no API behind them yet, so they are deliberately absent rather " +
      "than showing sample data that looks real:</p>" +
      '<div class="check-list"><label>World map dashboard</label><label>Customers / MAH profiles</label>' +
      "<label>Approval evidence</label><label>EUDAMED</label><label>Entities &amp; accounts</label>" +
      "<label>Data dictionary</label><label>Review timelines</label></div></div>";
  }

  // ---- registrations ----

  function pageRegistrations() {
    var rows = state.registrations;

    var countries = {};
    var classes = {};
    rows.forEach(function (r) {
      if (r.country_name || r.country_code) countries[r.country_code] = r.country_name || r.country_code;
      if (r.product_class_code) classes[r.product_class_code] = 1;
    });

    function opts(map, selected, labeller) {
      return Object.keys(map).sort().map(function (key) {
        return '<option value="' + esc(key) + '"' + (selected === key ? " selected" : "") +
          ">" + esc(labeller ? labeller(key) : key) + "</option>";
      }).join("");
    }

    var body;
    if (!rows.length) {
      body = '<div class="empty">No registrations match this view.' +
        (state.filters.search || state.filters.country || state.filters.product_class || state.filters.status_filter
          ? " Try clearing the filters." : " Upload a Registration Request Form to raise some.") +
        "</div>";
    } else {
      body = '<div class="table-wrap"><table class="rec-table" style="min-width:900px">' +
        '<colgroup><col style="width:150px"><col style="width:120px"><col style="width:240px">' +
        '<col style="width:150px"><col style="width:130px"><col style="width:120px"></colgroup>' +
        "<thead><tr><th>Country</th><th>SKU</th><th>Product</th>" +
        "<th>Certificate</th><th>Certificate Expiry</th><th>Status</th></tr></thead><tbody>" +
        rows.map(function (r) {
          return '<tr class="clickable" data-reg="' + esc(r.id) + '">' +
            '<td class="nowrap">' + dash(r.country_name || r.country_code) + "</td>" +
            '<td class="mono">' + dash(r.sku) + "</td>" +
            "<td>" + dash(r.product_description) + "</td>" +
            '<td class="mono">' + dash(r.registration_number) + "</td>" +
            '<td class="mono">' + (r.expiry_date ? esc(fmtDate(r.expiry_date)) : dash("")) + "</td>" +
            "<td>" + statusPill(r.status) + "</td></tr>";
        }).join("") +
        "</tbody></table></div>";
    }

    return '<div class="card">' +
      '<div class="rec-bar"><h3>Registrations</h3>' +
        '<div class="rec-search"><input type="text" id="regSearch" placeholder="Search&hellip;" ' +
        'value="' + esc(state.filters.search) + '"></div>' +
      "</div>" +
      '<div class="filter-row" style="margin-bottom:14px">' +
        '<select id="fCountry"><option value="">All countries</option>' +
          opts(countries, state.filters.country, function (k) { return countries[k]; }) + "</select>" +
        '<select id="fClass"><option value="">All product classes</option>' +
          opts(classes, state.filters.product_class) + "</select>" +
        '<select id="fStatus"><option value="">All statuses</option>' +
          STATUSES.map(function (s) {
            return '<option value="' + esc(s) + '"' +
              (state.filters.status_filter === s ? " selected" : "") + ">" + esc(s) + "</option>";
          }).join("") + "</select>" +
        '<button class="btn" id="fClear">Clear</button>' +
      "</div>" +
      '<div class="rec-actions"><span class="count">' + rows.length +
        " Registration" + (rows.length === 1 ? "" : "s") + "</span></div>" +
      body + "</div>" + regPanel();
  }

  var REG_FIELDS = [
    ["registration_number", "Certificate number", "text"],
    ["sku", "SKU", "text"],
    ["product_description", "Product", "text"],
    ["product_class_code", "Product class", "text"],
    ["device_class", "Device class", "text"],
    ["record_type", "Record type", "text"],
    ["legal_manufacturer", "Legal manufacturer", "text"],
    ["submitted_date", "Submission date", "date"],
    ["approved_date", "Approval date", "date"],
    ["expiry_date", "Certificate expiry", "date"],
    ["alert_months", "Alert timeframe (months)", "number"],
    ["ifu_version", "IFU version", "text"],
    ["issuing_body", "Issuing body", "text"],
    ["company_contact", "AirLife contact", "text"],
    ["local_rep", "Local representative", "text"],
    ["local_distributors", "Distributor", "text"],
    ["device_udi", "Device UDI", "text"],
    ["part_numbers", "Part numbers", "text"],
    ["configuration", "Configuration", "text"],
    ["medical_code", "Medical code", "text"],
    ["comments", "Comments", "text"],
    ["other_information", "Other information", "text"]
  ];

  function regPanel() {
    var reg = state.openRegDetail;
    if (!reg) return "";

    function kv(label, value) {
      return '<div class="rec-kv"><span class="k">' + esc(label) + "</span>" +
        '<span class="v">' + (value === null || value === undefined || value === ""
          ? '<span class="muted">&mdash;</span>' : esc(value)) + "</span></div>";
    }

    var body;
    if (state.editing) {
      body = '<div class="rec-group"><div class="section-h">Edit</div>' +
        REG_FIELDS.map(function (f) {
          var value = reg[f[0]];
          if (f[2] === "date" && value) value = String(value).slice(0, 10);
          return '<label style="font-size:12px; font-weight:600; color:var(--ink-soft); margin-top:6px">' +
            esc(f[1]) + "</label>" +
            '<input type="' + f[2] + '" data-field="' + esc(f[0]) + '" value="' +
            esc(value === null || value === undefined ? "" : value) + '" ' +
            'style="border:1px solid var(--line); border-radius:7px; padding:7px 9px; ' +
            'background:var(--card); color:var(--ink); width:100%">';
        }).join("") + "</div>";
    } else {
      body = '<div class="rec-group"><div class="section-h">Registration</div>' +
          kv("Status", reg.status) + kv("Country", reg.country_name || reg.country_code) +
          kv("Certificate number", reg.registration_number) +
          kv("SKU", reg.sku) + kv("Product", reg.product_description) +
          kv("Product class", reg.product_class_code) + kv("Device class", reg.device_class) +
          kv("Record type", reg.record_type) + kv("Legal manufacturer", reg.legal_manufacturer) +
          (reg.pending_type ? kv("Pending type", reg.pending_type) : "") +
        "</div>" +
        '<div class="rec-group"><div class="section-h">Dates</div>' +
          kv("Submission date", fmtDate(reg.submitted_date)) +
          kv("Approval date", fmtDate(reg.approved_date)) +
          kv("Certificate expiry", fmtDate(reg.expiry_date)) +
          kv("Alert timeframe", reg.alert_months ? reg.alert_months + " months before expiry" : "") +
          kv("Alert window opens", fmtDate(reg.alert_opens_on)) +
        "</div>" +
        '<div class="rec-group"><div class="section-h">Detail</div>' +
          kv("IFU version", reg.ifu_version) + kv("Issuing body", reg.issuing_body) +
          kv("AirLife contact", reg.company_contact) + kv("Local representative", reg.local_rep) +
          kv("Distributor", reg.local_distributors) + kv("Device UDI", reg.device_udi) +
          kv("Part numbers", reg.part_numbers) + kv("Configuration", reg.configuration) +
          kv("Medical code", reg.medical_code) + kv("Comments", reg.comments) +
          kv("Other information", reg.other_information) +
        "</div>" + historyBlock();
    }

    return '<div class="rec-scrim" id="regScrim"></div>' +
      '<div class="rec-panel" role="dialog" aria-label="Registration detail">' +
        '<div class="rec-panel-head"><div>' +
          '<span class="rec-badge" style="background:var(--brand); color:#fff">' +
          esc(reg.status || "Registration") + "</span>" +
          '<div class="ttl" style="margin-top:6px">' +
          esc(reg.product_description || reg.sku || "Registration") + "</div>" +
          '<div class="sub">' + esc(reg.country_name || reg.country_code || "") +
          (reg.registration_number ? " &middot; " + esc(reg.registration_number) : "") + "</div>" +
        '</div><button class="rec-panel-close" id="regClose" title="Close">&times;</button></div>' +
        '<div class="rec-panel-body">' + body + "</div>" +
        '<div class="rec-panel-foot">' +
          '<button class="btn tiny" id="regHistoryBtn">History</button>' +
          '<div class="btn-row">' +
            (canEdit()
              ? (state.editing
                  ? '<button class="btn tiny good" id="regSave">Save</button>' +
                    '<button class="btn tiny" id="regCancel">Cancel</button>'
                  : '<button class="btn tiny" id="regEdit">Edit</button>')
              : '<span class="readonly-note muted">Read only</span>') +
          "</div>" +
        "</div></div>";
  }

  function historyBlock() {
    var history = state.openRegHistory;
    if (!history) return "";
    if (!history.length) {
      return '<div class="rec-group"><div class="section-h">History</div>' +
        '<p class="muted">No changes recorded yet.</p></div>';
    }
    return '<div class="rec-group"><div class="section-h">History (' + history.length + ")</div>" +
      history.map(function (h) {
        var what;
        if (h.action === "insert") what = "<b>Created</b>";
        else if (h.action === "delete") what = "<b>Deleted</b>";
        else {
          what = "<b>" + esc(h.field) + "</b> " +
            (h.old_value ? esc(h.old_value) : '<span class="muted">empty</span>') +
            " &rarr; " + (h.new_value ? esc(h.new_value) : '<span class="muted">empty</span>');
        }
        return '<div class="hist-row"><div class="when">' + esc(fmtWhen(h.occurred_at)) +
          "<br>" + esc(h.actor) + '</div><div class="what">' + what +
          '<div class="why">' + esc(h.reason) + "</div></div></div>";
      }).join("") + "</div>";
  }

  // ---- gtin lookup ----

  function pageGtins() {
    var rows = state.gtins;
    var body;
    if (!rows.length) {
      body = '<div class="empty">No GTIN authorizations on file.</div>';
    } else {
      body = '<div class="table-wrap"><table class="rec-table" style="min-width:980px">' +
        "<thead><tr><th>GTIN</th><th>SKU</th><th>Country</th><th>Status for sale</th>" +
        "<th>Expiry</th><th>Shippable</th><th>Reason</th></tr></thead><tbody>" +
        rows.map(function (g) {
          var shippable = g.is_shippable
            ? '<span class="pill good"><span class="dot"></span>Yes</span>'
            : '<span class="pill bad"><span class="dot"></span>No</span>';
          return "<tr>" +
            '<td class="mono">' + dash(g.gtin) + "</td>" +
            '<td class="mono">' + dash(g.sku) + "</td>" +
            '<td class="nowrap">' + dash(g.country_code) + "</td>" +
            "<td>" + dash(g.status_for_sale) + "</td>" +
            '<td class="mono">' + (g.expiry_date ? esc(fmtDate(g.expiry_date)) : dash("")) + "</td>" +
            "<td>" + shippable + "</td>" +
            "<td>" + (g.on_hold ? '<span class="pill warn">On hold</span>' : dash(g.reason_comments)) + "</td>" +
            "</tr>";
        }).join("") + "</tbody></table></div>";
    }

    return '<div class="card"><h1 style="font-size:19px">GTIN Lookup</h1>' +
      '<p class="muted" style="margin:4px 0 14px">Whether a GTIN may ship into a country, and ' +
      "if not, why. Shipping eligibility accounts for approval, blocking, active holds and " +
      "expiry &mdash; an expired authorization is not shippable.</p>" +
      '<div class="filter-row" style="margin-bottom:14px">' +
        '<input type="text" id="gtinSearch" placeholder="GTIN&hellip;" value="' +
        esc(state.filters.gtin || "") + '">' +
        '<button class="btn" id="gtinGo">Search</button>' +
        '<button class="btn" id="gtinClear">Clear</button>' +
      "</div>" + body + "</div>";
  }

  // ---- holds ----

  function pageHolds() {
    var rows = state.holds;
    var body;
    if (!rows.length) {
      body = '<div class="empty">No active holds.</div>';
    } else {
      body = '<div class="table-wrap"><table class="rec-table" style="min-width:900px">' +
        "<thead><tr><th>GTIN</th><th>Scope</th><th>Reason</th><th>Reference</th>" +
        "<th>Applied</th><th>By</th><th></th></tr></thead><tbody>" +
        rows.map(function (h) {
          return "<tr>" +
            '<td class="mono">' + dash(h.gtin) + "</td>" +
            "<td>" + (h.scope ? esc(h.scope) : '<span class="muted">All markets</span>') + "</td>" +
            "<td>" + dash(h.reason) + "</td>" +
            '<td class="mono">' + dash(h.source_ref) + "</td>" +
            '<td class="mono nowrap">' + esc(fmtWhen(h.applied_at)) + "</td>" +
            "<td>" + dash(h.applied_by) + "</td>" +
            "<td>" + (canEdit()
              ? '<button class="rec-act" data-lift="' + esc(h.id) + '">Lift</button>'
              : "") + "</td></tr>";
        }).join("") + "</tbody></table></div>";
    }

    return '<div class="card">' +
      '<div class="rec-bar"><h3>Holds</h3>' +
      (canEdit() ? '<button class="btn add" id="holdNew">+ Apply hold</button>' : "") + "</div>" +
      '<p class="muted" style="margin-bottom:12px">Holds are recorded as events rather than a ' +
      "flag, so a GTIN's hold history survives the hold being lifted.</p>" +
      body + "</div>";
  }

  // ---- request form intake ----

  function pageIntake() {
    var forms = state.forms;
    var list;
    if (!forms.length) {
      list = '<div class="empty">No Registration Request Forms uploaded yet.</div>';
    } else {
      list = '<div class="table-wrap"><table class="rec-table" style="min-width:940px">' +
        '<colgroup><col style="width:300px"><col style="width:120px"><col style="width:150px">' +
        '<col style="width:130px"><col style="width:70px"><col style="width:80px">' +
        '<col style="width:110px"></colgroup>' +
        "<thead><tr><th>File</th><th>Country</th><th>Distributor</th><th>Requested</th>" +
        "<th>SKUs</th><th>Licences</th><th>State</th></tr></thead><tbody>" +
        forms.map(function (f) {
          var warnings = Array.isArray(f.warnings) ? f.warnings
            : (f.warnings ? JSON.parse(f.warnings) : []);
          var pill = f.state === "accepted" ? "good" : f.state === "rejected" ? "bad" : "pending";
          return '<tr class="clickable" data-form="' + esc(f.id) + '">' +
            '<td class="nowrap" title="' + esc(f.filename) + '">' + dash(f.filename) +
              (warnings.length ? ' <span class="pill warn tiny">' + warnings.length +
                (warnings.length === 1 ? " warning" : " warnings") + "</span>" : "") + "</td>" +
            "<td>" + dash(f.country_name) + "</td>" +
            "<td>" + dash(f.distributor) + "</td>" +
            '<td class="mono">' + (f.request_date ? esc(fmtDate(f.request_date)) : dash("")) + "</td>" +
            '<td class="mono">' + dash(f.sku_count) + "</td>" +
            '<td class="mono">' + dash(f.licences_expected) + "</td>" +
            '<td><span class="pill ' + pill + '"><span class="dot"></span>' +
              esc(f.state) + "</span></td></tr>";
        }).join("") + "</tbody></table></div>";
    }

    return '<div class="card">' +
      '<div class="rec-bar"><h3>Registration Request Forms</h3></div>' +
      '<p class="muted" style="margin-bottom:12px">Upload a completed form and the country, ' +
      "distributor, SKU list, licence grouping and requirements checklist are read from it. " +
      "Nothing is created until you review what was read and accept it.</p>" +
      (canEdit()
        ? '<div class="drop" id="formDrop">Drop a completed Registration Request Form here, ' +
          "or click to choose one (.xlsx)" +
          '<input type="file" id="formFile" accept=".xlsx" style="display:none"></div>'
        : '<p class="readonly-note muted">You have read access; uploading a form requires the ' +
          "editor group.</p>") +
      "</div>" +
      '<div class="card"><h3 style="margin-bottom:10px">Uploaded forms</h3>' + list + "</div>" +
      formPanel();
  }

  function formPanel() {
    var form = state.openForm;
    if (!form) return "";
    var parsed = form.parsed;
    if (typeof parsed === "string") { try { parsed = JSON.parse(parsed); } catch (e) { parsed = {}; } }
    var warnings = Array.isArray(form.warnings) ? form.warnings
      : (form.warnings ? JSON.parse(form.warnings) : []);
    var groups = (parsed && parsed.groups) || [];
    var requirements = (parsed && parsed.requirements) || [];

    function kv(label, value) {
      return "<div><div class=\"kv-label\">" + esc(label) + "</div>" +
        '<div class="kv-value">' + (value === null || value === undefined || value === ""
          ? '<span class="muted">&mdash;</span>' : esc(value)) + "</div></div>";
    }

    var outstanding = requirements.filter(function (r) { return !r.received; }).length;

    return '<div class="rec-scrim" id="formScrim"></div>' +
      '<div class="rec-panel" role="dialog" aria-label="Request form">' +
        '<div class="rec-panel-head"><div>' +
          '<span class="rec-badge" style="background:var(--brand); color:#fff">' +
          esc(form.state) + "</span>" +
          '<div class="ttl" style="margin-top:6px">' + esc(form.filename) + "</div>" +
          '<div class="sub">' + esc(form.country_name || "") +
          (form.distributor ? " &middot; " + esc(form.distributor) : "") + "</div>" +
        '</div><button class="rec-panel-close" id="formClose" title="Close">&times;</button></div>' +
        '<div class="rec-panel-body">' +

        (warnings.length
          ? '<div class="rec-group"><div class="section-h">Needs a decision (' +
            warnings.length + ")</div>" +
            '<div class="warn-list">' + warnings.map(function (w) {
              return '<div class="warn-item"><span class="ic">!</span><span>' + esc(w) + "</span></div>";
            }).join("") + "</div>" +
            '<p class="proto-note">The parser does not guess. Confirm or correct these before ' +
            "accepting &mdash; a wrong grouping means a wrong number of licence applications.</p></div>"
          : "") +

        '<div class="rec-group"><div class="section-h">From the form</div>' +
          '<div class="kv-pairs">' +
            kv("Country", form.country_name) + kv("Distributor", form.distributor) +
            kv("RA contact", form.ra_contact) + kv("Date of request", fmtDate(form.request_date)) +
            kv("Signed by", form.signed_by) + kv("SKUs", form.sku_count) +
            kv("Licences expected", form.licences_expected) +
            kv("Sheet", form.source_sheet) +
          "</div></div>" +

        '<div class="rec-group"><div class="section-h">Licence applications (' + groups.length + ")</div>" +
          groups.map(function (g) {
            return '<div class="group-row"><div class="hd"><span class="n">Licence ' +
              esc(g.index) + '</span><span class="muted">' + g.skus.length + " SKUs</span></div>" +
              "<div>" + esc(g.description || "") + "</div>" +
              '<div class="skus muted">' + esc(g.skus.join(", ")) + "</div></div>";
          }).join("") + "</div>" +

        '<div class="rec-group"><div class="section-h">Requirements (' + requirements.length + ")</div>" +
          (requirements.length
            ? '<div class="table-wrap"><table><thead><tr><th>#</th><th>Requirement</th>' +
              "<th>Comments</th><th>Received</th></tr></thead><tbody>" +
              requirements.map(function (r) {
                return "<tr><td>" + dash(r.number) + "</td><td>" + dash(r.requirement) + "</td>" +
                  "<td>" + dash(r.comments) + "</td><td>" +
                  (r.received ? esc(r.received) : '<span class="pill warn tiny">outstanding</span>') +
                  "</td></tr>";
              }).join("") + "</tbody></table></div>" +
              (outstanding ? '<p class="proto-note">' + outstanding +
                " requirement(s) have no 'Received' entry.</p>" : "")
            : '<p class="muted">No requirements sheet found in the workbook.</p>') +
        "</div>" +

        "</div>" +
        '<div class="rec-panel-foot">' +
          (form.state === "parsed" && canEdit()
            ? '<button class="btn tiny danger" id="formReject">Reject</button>' +
              '<div class="btn-row"><button class="btn tiny good" id="formAccept">' +
              "Accept &amp; raise " + groups.length + " registration" +
              (groups.length === 1 ? "" : "s") + "</button></div>"
            : '<span class="readonly-note muted">' +
              (form.state !== "parsed" ? "Already " + esc(form.state) : "Read only") + "</span>") +
        "</div></div>";
  }

  // -------------------------------------------------------------- render

  function render() {
    document.getElementById("tabbar").innerHTML = NAV.map(function (item) {
      return '<a href="#' + item[0] + '"' +
        (state.route === item[0] ? ' class="active"' : "") + ">" + esc(item[1]) + "</a>";
    }).join("");

    var who = document.getElementById("whoami");
    var pill = document.getElementById("accessPill");
    if (state.user) {
      who.textContent = state.user.email || state.user.username || "unknown";
      pill.className = "pill " + (state.user.can_edit ? "good" : "neutral");
      pill.textContent = state.user.can_edit ? "Editor" : "Viewer";
    }
    var badge = document.getElementById("envBadge");
    if (state.health) {
      var ephemeral = /in-memory/.test(state.health.storage || "");
      badge.textContent = ephemeral ? "DEV — data is not saved" : "Prototype — not validated";
      badge.style.background = ephemeral ? "rgba(255,180,80,.3)" : "";
    }

    var main = document.getElementById("main");
    main.className = state.route === "/" ? "" : "wide";

    var html;
    if (state.loading) {
      html = '<div class="card"><div class="empty">Loading&hellip;</div></div>';
    } else if (state.error) {
      html = '<div class="card"><div class="banner" style="display:block">' +
        esc(state.error) + "</div></div>";
    } else if (state.route === "/registrations") html = pageRegistrations();
    else if (state.route === "/gtins") html = pageGtins();
    else if (state.route === "/holds") html = pageHolds();
    else if (state.route === "/intake") html = pageIntake();
    else html = pageHome();

    main.innerHTML = html;
    document.getElementById("overlay").innerHTML = modalHtml();
    wire();
  }

  // ---------------------------------------------------------------- wire

  function wire() {
    wireModal();

    document.querySelectorAll("tr[data-reg]").forEach(function (tr) {
      tr.addEventListener("click", function () { openRegistration(this.getAttribute("data-reg")); });
    });
    document.querySelectorAll("tr[data-form]").forEach(function (tr) {
      tr.addEventListener("click", function () { openForm(this.getAttribute("data-form")); });
    });

    bind("regClose", "click", function () {
      state.openReg = null; state.openRegDetail = null;
      state.openRegHistory = null; state.editing = false; render();
    });
    bind("regScrim", "click", function () {
      state.openReg = null; state.openRegDetail = null;
      state.openRegHistory = null; state.editing = false; render();
    });
    bind("regEdit", "click", function () { state.editing = true; render(); });
    bind("regCancel", "click", function () { state.editing = false; render(); });
    bind("regSave", "click", saveRegistration);
    bind("regHistoryBtn", "click", loadHistory);

    bind("regSearch", "input", function () {
      state.filters.search = this.value;
      debounce(loadRegistrations, 300)();
    });
    bind("fCountry", "change", function () { state.filters.country = this.value; loadRegistrations(); });
    bind("fClass", "change", function () { state.filters.product_class = this.value; loadRegistrations(); });
    bind("fStatus", "change", function () { state.filters.status_filter = this.value; loadRegistrations(); });
    bind("fClear", "click", function () {
      state.filters = { country: "", product_class: "", status_filter: "", search: "" };
      loadRegistrations();
    });

    bind("gtinGo", "click", function () {
      var box = document.getElementById("gtinSearch");
      state.filters.gtin = box ? box.value.trim() : "";
      loadGtins();
    });
    bind("gtinClear", "click", function () { state.filters.gtin = ""; loadGtins(); });

    bind("holdNew", "click", newHold);
    document.querySelectorAll("[data-lift]").forEach(function (btn) {
      btn.addEventListener("click", function () { liftHold(this.getAttribute("data-lift")); });
    });

    bind("formDrop", "click", function () {
      var input = document.getElementById("formFile");
      if (input) input.click();
    });
    bind("formFile", "change", function () {
      if (this.files && this.files[0]) uploadForm(this.files[0]);
    });
    var drop = document.getElementById("formDrop");
    if (drop) {
      ["dragenter", "dragover"].forEach(function (name) {
        drop.addEventListener(name, function (ev) {
          ev.preventDefault(); drop.classList.add("on");
        });
      });
      ["dragleave", "drop"].forEach(function (name) {
        drop.addEventListener(name, function (ev) {
          ev.preventDefault(); drop.classList.remove("on");
        });
      });
      drop.addEventListener("drop", function (ev) {
        if (ev.dataTransfer && ev.dataTransfer.files[0]) uploadForm(ev.dataTransfer.files[0]);
      });
    }
    bind("formClose", "click", function () { state.openForm = null; render(); });
    bind("formScrim", "click", function () { state.openForm = null; render(); });
    bind("formAccept", "click", acceptForm);
    bind("formReject", "click", rejectForm);
  }

  function bind(id, event, handler) {
    var el = document.getElementById(id);
    if (el) el.addEventListener(event, handler);
  }

  var timers = {};
  function debounce(fn, ms) {
    return function () {
      clearTimeout(timers[fn]);
      timers[fn] = setTimeout(fn, ms);
    };
  }

  // -------------------------------------------------------------- actions

  function loadRegistrations() {
    var query = [];
    if (state.filters.country) query.push("country=" + encodeURIComponent(state.filters.country));
    if (state.filters.product_class) query.push("product_class=" + encodeURIComponent(state.filters.product_class));
    if (state.filters.status_filter) query.push("status_filter=" + encodeURIComponent(state.filters.status_filter));
    if (state.filters.search) query.push("search=" + encodeURIComponent(state.filters.search));
    return api("/api/registrations" + (query.length ? "?" + query.join("&") : ""))
      .then(function (data) {
        state.registrations = data.registrations || [];
        render();
        var box = document.getElementById("regSearch");
        if (box && state.filters.search) {
          box.focus();
          box.setSelectionRange(box.value.length, box.value.length);
        }
      })
      .catch(fail);
  }

  function loadGtins() {
    var query = state.filters.gtin ? "?gtin=" + encodeURIComponent(state.filters.gtin) : "";
    return api("/api/gtins" + query)
      .then(function (data) { state.gtins = data.gtins || []; render(); })
      .catch(fail);
  }

  function loadHolds() {
    return api("/api/holds")
      .then(function (data) { state.holds = data.holds || []; render(); })
      .catch(fail);
  }

  function loadForms() {
    return api("/api/intake/forms")
      .then(function (data) { state.forms = data.forms || []; render(); })
      .catch(fail);
  }

  function openRegistration(id) {
    state.openReg = id;
    state.openRegHistory = null;
    state.editing = false;
    api("/api/registrations/" + id)
      .then(function (reg) { state.openRegDetail = reg; render(); })
      .catch(fail);
  }

  function loadHistory() {
    if (!state.openReg) return;
    api("/api/registrations/" + state.openReg + "/history")
      .then(function (data) { state.openRegHistory = data.history || []; render(); })
      .catch(fail);
  }

  function saveRegistration() {
    var changes = {};
    document.querySelectorAll("[data-field]").forEach(function (input) {
      var key = input.getAttribute("data-field");
      var value = input.value;
      if (input.type === "number") value = value === "" ? null : Number(value);
      changes[key] = value === "" ? null : value;
    });

    askReason(
      "Save changes to this registration",
      state.openRegDetail.product_description || state.openRegDetail.sku || "",
      function (reason) {
        api("/api/registrations/" + state.openReg, {
          method: "PATCH", body: { reason: reason, changes: changes }
        }).then(function (result) {
          state.editing = false;
          state.openRegDetail = result.registration;
          state.openRegHistory = null;
          if (!result.changed.length) {
            toast("info", "Nothing changed", "The values sent match what is stored.");
          } else {
            toast("ok", "Saved", result.changed.length + " field(s) changed and recorded.");
          }
          loadRegistrations();
        }).catch(function (err) { fail(err); render(); });
      }
    );
  }

  function newHold() {
    askReason(
      "Apply a hold",
      "Blocks the GTIN from shipping until the hold is lifted.",
      function (reason) {
        var gtin = (document.getElementById("holdGtin") || {}).value;
        var scope = (document.getElementById("holdScope") || {}).value;
        var ref = (document.getElementById("holdRef") || {}).value;
        if (!gtin || gtin.trim().length < 8) {
          toast("bad", "A GTIN is required", "Enter the GTIN this hold applies to.");
          return;
        }
        api("/api/holds", {
          method: "POST",
          body: {
            gtin: gtin.trim(), scope: scope ? scope.trim() : null,
            reason: reason, source_ref: ref ? ref.trim() : null
          }
        }).then(function () {
          toast("ok", "Hold applied", "The GTIN is blocked from shipping.");
          loadHolds();
        }).catch(function (err) { fail(err); render(); });
      },
      '<label><b>GTIN</b></label><input type="text" id="holdGtin" placeholder="00812457000934">' +
      '<label><b>Scope</b></label><input type="text" id="holdScope" ' +
      'placeholder="Country code, or leave blank for all markets">' +
      '<label><b>Reference</b></label><input type="text" id="holdRef" placeholder="CAPA / DCR / NCR number">'
    );
  }

  function liftHold(holdId) {
    askReason(
      "Lift this hold",
      "The GTIN becomes shippable again once this is recorded.",
      function (reason) {
        api("/api/holds/" + holdId + "/lift", { method: "POST", body: { reason: reason } })
          .then(function () {
            toast("ok", "Hold lifted", "Recorded against your name.");
            loadHolds();
          }).catch(function (err) { fail(err); render(); });
      }
    );
  }

  function uploadForm(file) {
    var data = new FormData();
    data.append("file", file);
    toast("info", "Reading " + file.name, "Parsing the workbook…");
    api("/api/intake/forms", { method: "POST", form: data })
      .then(function (result) {
        var warnings = result.warnings || [];
        toast(warnings.length ? "info" : "ok", "Form read",
          result.parsed.sku_count + " SKUs, " + result.parsed.groups.length +
          " licence application(s)" +
          (warnings.length ? ", " + warnings.length + " thing(s) to confirm" : ""));
        return loadForms().then(function () { openForm(result.form.id); });
      })
      .catch(fail);
  }

  function openForm(id) {
    api("/api/intake/forms/" + id)
      .then(function (form) { state.openForm = form; render(); })
      .catch(fail);
  }

  function acceptForm() {
    var form = state.openForm;
    var parsed = form.parsed;
    if (typeof parsed === "string") parsed = JSON.parse(parsed);
    var groups = parsed.groups || [];

    askReason(
      "Accept this form",
      "Raises " + groups.length + " pending registration" + (groups.length === 1 ? "" : "s") + ".",
      function (reason) {
        api("/api/intake/forms/" + form.id + "/accept", {
          method: "POST", body: { reason: reason }
        }).then(function (result) {
          toast("ok", "Accepted", result.created + " registration(s) raised.");
          state.openForm = null;
          loadForms();
          loadRegistrations();
        }).catch(function (err) { fail(err); render(); });
      },
      '<div class="warn-item"><span class="ic">i</span><span>This raises one pending ' +
      "registration per licence application below. If the grouping is wrong, reject the form " +
      "and have it resubmitted, or correct it after the registrations are raised.</span></div>" +
      '<div class="group-editor">' + groups.map(function (g) {
        return '<div class="group-row"><div class="hd"><span class="n">Licence ' + esc(g.index) +
          '</span><span class="muted">' + g.skus.length + " SKUs</span></div>" +
          '<div class="skus muted">' + esc(g.skus.join(", ")) + "</div></div>";
      }).join("") + "</div>"
    );
  }

  function rejectForm() {
    var form = state.openForm;
    askReason(
      "Reject this form",
      form.filename,
      function (reason) {
        api("/api/intake/forms/" + form.id + "/reject", {
          method: "POST", body: { reason: reason }
        }).then(function () {
          toast("ok", "Rejected", "Recorded against your name.");
          state.openForm = null;
          loadForms();
        }).catch(function (err) { fail(err); render(); });
      }
    );
  }

  // --------------------------------------------------------------- boot

  function routeChanged() {
    state.route = (location.hash || "#/").slice(1) || "/";
    state.openReg = null; state.openRegDetail = null; state.openForm = null;
    state.editing = false;
    render();
    if (state.route === "/registrations") loadRegistrations();
    else if (state.route === "/gtins") loadGtins();
    else if (state.route === "/holds") loadHolds();
    else if (state.route === "/intake") loadForms();
  }

  window.addEventListener("hashchange", routeChanged);

  Promise.all([
    api("/api/me").catch(function () { return null; }),
    api("/api/health").catch(function () { return null; })
  ]).then(function (results) {
    state.user = results[0];
    state.health = results[1];
    state.loading = false;
    // the home page needs all four to count anything
    return Promise.all([
      api("/api/registrations").then(function (d) { state.registrations = d.registrations || []; }),
      api("/api/holds").then(function (d) { state.holds = d.holds || []; }),
      api("/api/intake/forms").then(function (d) { state.forms = d.forms || []; })
    ]).catch(function () { /* an empty home page is better than no app */ });
  }).then(routeChanged, function (err) {
    state.loading = false;
    state.error = err.message;
    render();
  });
})();

/* Podium front-end behaviour. Vanilla JS, no build step, CSP-safe (no inline handlers). */
(function () {
  "use strict";

  // ---- theme -------------------------------------------------------------------------------
  var THEME_KEY = "podium-theme";
  function applyTheme(value) {
    var root = document.documentElement;
    if (value === "light" || value === "dark") root.setAttribute("data-theme", value);
    else root.removeAttribute("data-theme");
    document.querySelectorAll("[data-theme-set]").forEach(function (btn) {
      btn.setAttribute("aria-pressed", String((btn.getAttribute("data-theme-set") || "system") === (value || "system")));
    });
  }
  function storedTheme() {
    try { return localStorage.getItem(THEME_KEY) || "system"; } catch (e) { return "system"; }
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-theme-set]");
    if (!btn) return;
    var value = btn.getAttribute("data-theme-set");
    try { if (value === "system") localStorage.removeItem(THEME_KEY); else localStorage.setItem(THEME_KEY, value); } catch (err) {}
    applyTheme(value === "system" ? null : value);
  });
  function syncTheme() { var v = storedTheme(); applyTheme(v === "system" ? null : v); }
  syncTheme();
  document.body.addEventListener("htmx:load", syncTheme);

  // ---- htmx wiring ---------------------------------------------------------------------------
  var csrfMeta = document.querySelector('meta[name="csrf-token"]');
  document.body.addEventListener("htmx:configRequest", function (e) {
    if (csrfMeta) e.detail.headers["X-CSRF-Token"] = csrfMeta.getAttribute("content");
  });
  var progress = document.querySelector(".nav-progress");
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    if (progress && e.detail.boosted) progress.classList.add("is-active");
  });
  document.body.addEventListener("htmx:afterRequest", function () {
    if (progress) progress.classList.remove("is-active");
  });
  function showAlert(kind, text) {
    var box = document.getElementById("alerts");
    if (!box) return;
    var el = document.createElement("div");
    el.className = "alert alert--" + kind;
    el.setAttribute("role", kind === "critical" ? "alert" : "status");
    el.textContent = text;
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, 6000);
  }
  // 4xx responses that carry an HTML partial (vote control, comments, save status, re-rendered
  // forms) swap in place: the message lives inside the control. JSON 4xx and 5xx keep the alert.
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var xhr = e.detail.xhr;
    if (!xhr || xhr.status < 400 || xhr.status >= 500) return;
    var type = xhr.getResponseHeader("Content-Type") || "";
    if (type.indexOf("text/html") === -1) return;
    e.detail.shouldSwap = true;
    e.detail.isError = false;
  });
  function inReviewForm(e) { var elt = e.detail && e.detail.elt; return !!(elt && elt.closest && elt.closest("[data-review-form]")); }
  document.body.addEventListener("htmx:responseError", function (e) {
    if (inReviewForm(e)) return;  // the save status beside the buttons says it, with a Retry
    var xhr = e.detail.xhr;
    if (xhr.status === 401) { showAlert("warning", "Your session has expired. Sign in to continue."); return; }
    var msg = "Something went wrong (" + xhr.status + "). Try again.";
    try { var data = JSON.parse(xhr.responseText); if (data.error && data.error.message) msg = data.error.message; } catch (err) {
      var plain = xhr.getResponseHeader("X-Podium-Message"); if (plain) msg = plain;
    }
    showAlert(xhr.status >= 500 ? "critical" : "warning", msg);
  });
  document.body.addEventListener("htmx:sendError", function (e) {
    if (inReviewForm(e)) return;
    showAlert("critical", "You're offline. Your changes are kept on this page — try again when you're back.");
  });
  // the progress page polls only while it is visible (a hx-trigger filter would need eval, which
  // the CSP forbids)
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    if (document.hidden && e.detail.elt && e.detail.elt.hasAttribute && e.detail.elt.hasAttribute("data-poll")) e.preventDefault();
  });

  // ---- date entry in the organizer's local time (stored as UTC by the server) -----------------
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function localizeDateForms() {
    document.querySelectorAll("[data-tz-offset]").forEach(function (hidden) {
      var form = hidden.closest("form");
      if (!form || form.dataset.tzDone) return;
      form.dataset.tzDone = "1";
      var alreadyLocal = hidden.value !== "";  // re-rendered after a failed save: values are as typed
      hidden.value = String(-new Date().getTimezoneOffset());
      var zone = "";
      try { zone = Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (err) {}
      form.querySelectorAll("label").forEach(function (label) {
        label.childNodes.forEach(function (node) {
          if (node.nodeType === 3 && node.nodeValue.indexOf("(UTC)") !== -1) node.nodeValue = node.nodeValue.replace("(UTC)", "(your local time)");
        });
      });
      if (!alreadyLocal) form.querySelectorAll('input[type="datetime-local"]').forEach(function (input) {
        if (!input.value) return;
        var d = new Date(input.value + "Z");
        if (isNaN(d)) return;
        input.value = d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + "T" + pad(d.getHours()) + ":" + pad(d.getMinutes());
      });
      var note = form.querySelector("[data-tz-note]");
      if (note) note.textContent = "Times are in your local time" + (zone ? " (" + zone + ")" : "") + " and stored in UTC.";
    });
  }
  localizeDateForms();
  document.body.addEventListener("htmx:load", localizeDateForms);

  // ---- local time beside UTC ------------------------------------------------------------------
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function zoneOf(d) {  // "UTC" for UTC viewers, else the browser's short zone ("GMT+5:30", "CEST")
    if (d.getTimezoneOffset() === 0) return "UTC";
    try {
      var part = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(d)
        .filter(function (p) { return p.type === "timeZoneName"; })[0];
      return part ? part.value : "";
    } catch (e) { return ""; }
  }
  function withZone(text, d) { var zone = zoneOf(d); return zone ? text + " " + zone : text; }
  function fmtLocal(d) {  // one format everywhere, same month names as the server, always with its zone
    return withZone(d.getDate() + " " + MONTHS[d.getMonth()] + " " + d.getFullYear() + ", " + pad(d.getHours()) + ":" + pad(d.getMinutes()), d);
  }
  function fmtUtcTime(d) { return pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes()) + " UTC"; }
  function fmtDelta(ms) {
    var s = Math.round(Math.abs(ms) / 1000), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
    if (d >= 2) return d + " d " + h + " h";
    if (s >= 3600) return (d * 24 + h) + " h " + m + " m";
    if (s >= 60) return m + " m";
    return "under a minute";
  }
  function enhanceCountdowns(root) {
    (root || document).querySelectorAll("[data-countdown]").forEach(function (el) {
      var d = new Date(el.getAttribute("data-countdown"));
      if (isNaN(d)) return;
      var diff = d - Date.now();
      var time = el.querySelector("time");
      var abs = time ? time.textContent : fmtLocal(d);
      // deadlines state both clocks: the viewer's and UTC
      if (d.getTimezoneOffset() !== 0 && abs.indexOf("UTC") === -1) abs += " · " + fmtUtcTime(d);
      el.textContent = diff > 0 ? el.getAttribute("data-before") + " in " + fmtDelta(diff) + " (" + abs + ")" : el.getAttribute("data-after") + " " + fmtDelta(diff) + " ago (" + abs + ")";
    });
  }
  function enhanceTimes(root) {
    (root || document).querySelectorAll("time[datetime][data-local]").forEach(function (t) {
      if (t.dataset.done) return;
      var d = new Date(t.getAttribute("datetime"));
      if (isNaN(d)) return;
      var utc = t.textContent.trim();
      t.setAttribute("title", utc);
      t.setAttribute("aria-label", fmtLocal(d) + " local time, " + utc);
      if (t.hasAttribute("data-time-only")) t.textContent = withZone(pad(d.getHours()) + ":" + pad(d.getMinutes()), d);
      else if (t.hasAttribute("data-short")) t.textContent = withZone(d.getDate() + " " + MONTHS[d.getMonth()] + ", " + pad(d.getHours()) + ":" + pad(d.getMinutes()), d);
      else t.textContent = fmtLocal(d);
      t.dataset.done = "1";
    });
    enhanceCountdowns(root);
  }
  enhanceTimes();
  document.body.addEventListener("htmx:afterSettle", function (e) { enhanceTimes(e.target); });

  // ---- close header menus on outside click / Escape ---------------------------------------------
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.menu[open]").forEach(function (d) { if (!d.contains(e.target)) d.removeAttribute("open"); });
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") document.querySelectorAll("details.menu[open]").forEach(function (d) { d.removeAttribute("open"); }); });

  // ---- copy buttons ------------------------------------------------------------------------------
  document.addEventListener("click", function (e) {
    var reveal = e.target.closest("[data-reveal]");
    if (reveal) {
      var input = document.getElementById(reveal.getAttribute("data-reveal"));
      if (input) { var show = input.type === "password"; input.type = show ? "text" : "password"; reveal.textContent = show ? "Hide" : "Show"; reveal.setAttribute("aria-pressed", show ? "true" : "false"); }
      return;
    }
    var btn = e.target.closest("[data-copy]");
    if (!btn) return;
    var text = btn.getAttribute("data-copy");
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(function () {
        var old = btn.textContent; btn.textContent = "Copied"; setTimeout(function () { btn.textContent = old; }, 1500);
      });
    }
  });

  // ---- print button --------------------------------------------------------------------------------
  document.addEventListener("click", function (e) { if (e.target.closest("[data-print]")) window.print(); });

  // ---- search shortcut "/" -------------------------------------------------------------------------
  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && !/input|textarea|select/i.test(document.activeElement.tagName)) {
      var search = document.querySelector("[data-search]");
      if (search) { e.preventDefault(); search.focus(); }
    }
  });

  // ---- judge review: live weighted total + keyboard scoring ------------------------------------------
  function reviewTotal(form) {
    var groups = form.querySelectorAll("[data-criterion]");
    var sumW = 0, sum = 0;
    groups.forEach(function (g) {
      var checked = g.querySelector("input[type=radio]:checked");
      var radios = g.querySelectorAll("input[type=radio]");
      if (!checked || !radios.length) return;
      var min = parseFloat(g.getAttribute("data-min")), max = parseFloat(g.getAttribute("data-max"));
      var w = parseFloat(g.getAttribute("data-weight")) || 1;
      if (max > min) { sum += w * (parseFloat(checked.value) - min) / (max - min); sumW += w; }
    });
    var out = form.querySelector("[data-total]");
    if (out) out.textContent = sumW ? (100 * sum / sumW).toFixed(1) : "—";
  }
  function currentReviewForm() { return document.querySelector("[data-review-form]"); }
  document.addEventListener("change", function (e) { var f = e.target.closest && e.target.closest("[data-review-form]"); if (f) reviewTotal(f); });
  document.body.addEventListener("htmx:load", function () { var f = currentReviewForm(); if (f) reviewTotal(f); });
  document.addEventListener("DOMContentLoaded", function () { var f = currentReviewForm(); if (f) reviewTotal(f); });
  document.addEventListener("keydown", function (e) {
    var reviewForm = currentReviewForm();
    if (!reviewForm || !(e.target.closest && e.target.closest("[data-review-form]"))) return;
    var submitCombo = (e.metaKey || e.ctrlKey) && e.key === "Enter";
    if (!submitCombo && (e.altKey || e.ctrlKey || e.metaKey || e.repeat)) return;
    if (/input|textarea|select/i.test(document.activeElement.tagName) && document.activeElement.type !== "radio") {
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); var s = reviewForm.querySelector("[data-submit-review]"); if (s) s.click(); }
      return;
    }
    var groups = Array.prototype.slice.call(reviewForm.querySelectorAll("[data-criterion]"));
    var active = document.activeElement.closest ? document.activeElement.closest("[data-criterion]") : null;
    var idx = active ? groups.indexOf(active) : -1;
    if (e.key === "j" || e.key === "k") {
      e.preventDefault();
      var next = groups[Math.min(groups.length - 1, Math.max(0, idx + (e.key === "j" ? 1 : -1)))];
      if (next) { var r = next.querySelector("input[type=radio]:checked") || next.querySelector("input[type=radio]"); if (r) r.focus(); }
    } else if (/^[0-9]$/.test(e.key) && idx >= 0) {
      var target = active.querySelector('input[type=radio][value="' + e.key + '"]');
      if (target && !target.disabled) { e.preventDefault(); target.checked = true; target.focus(); target.dispatchEvent(new Event("change", { bubbles: true })); }
    } else if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();  // otherwise Enter also submits through the first button (Save draft)
      var btn = reviewForm.querySelector("[data-submit-review]"); if (btn) btn.click();
    }
  });
  // a scored criterion drops its "score this first" error; a saved draft drops the banner
  document.addEventListener("change", function (e) {
    var crit = e.target.closest && e.target.closest("[data-criterion]");
    if (!crit) return;
    crit.classList.remove("is-invalid");
    var err = crit.querySelector(".field__error"); if (err) err.remove();
    var form = crit.closest("[data-review-form]");
    if (!form) return;
    var groups = Array.prototype.slice.call(form.querySelectorAll("[data-criterion]"));
    var values = groups.map(function (g) { var c = g.querySelector("input[type=radio]:checked"); return c ? c.value : null; });
    var nudge = form.querySelector("[data-flat-nudge]");
    if (nudge && groups.length > 1 && values.every(function (v) { return v !== null; }) && values.every(function (v) { return v === values[0]; })) {
      nudge.textContent = "Every criterion scored " + values[0] + " — sure? Identical scores carry no ranking information."; nudge.hidden = false;
    } else if (nudge) { nudge.hidden = true; }
  });
  document.body.addEventListener("htmx:afterRequest", function (e) {
    var form = e.target.closest && e.target.closest("[data-review-form]");
    if (form && e.detail.successful) {
      var box = document.getElementById("review-errors"); if (box) box.innerHTML = "";
      failedSave = null; dirty = false;
    }
  });
  var failedSave = null;  // the review form whose last autosave never reached the server
  document.body.addEventListener("htmx:beforeRequest", function (e) {
    if (!inReviewForm(e) || e.defaultPrevented) return;
    var status = document.getElementById("save-status");
    if (status) status.innerHTML = '<span class="save-status__pending">Saving…</span>';
  });
  function saveFailed(e, text) {
    if (!inReviewForm(e)) return;
    var status = document.getElementById("save-status");
    if (!status) return;
    failedSave = e.detail.elt.closest("[data-review-form]");
    dirty = true;  // leaving now would lose the scores, so the guard asks
    var msg = document.createElement("span");
    msg.className = "save-status__err";
    msg.setAttribute("role", "alert");
    msg.textContent = text + " ";
    var retry = document.createElement("button");
    retry.type = "button"; retry.className = "btn btn--ghost btn--sm"; retry.setAttribute("data-retry-save", ""); retry.textContent = "Retry";
    msg.appendChild(retry);
    status.innerHTML = ""; status.appendChild(msg);
  }
  document.body.addEventListener("htmx:sendError", function (e) { saveFailed(e, "Not saved: you're offline. Your scores are still on this page."); });
  document.body.addEventListener("htmx:responseError", function (e) {
    if (e.detail.xhr && e.detail.xhr.status >= 500) saveFailed(e, "Not saved: the server had a problem. Your scores are still on this page.");
  });
  function retrySave() { if (failedSave && window.htmx) window.htmx.trigger(failedSave, "change"); }
  document.addEventListener("click", function (e) { if (e.target.closest && e.target.closest("[data-retry-save]")) retrySave(); });
  window.addEventListener("online", retrySave);

  // ---- compare mode keyboard -------------------------------------------------------------------
  document.addEventListener("keydown", function (e) {
    var box = document.querySelector("[data-compare]");
    if (!box || e.altKey || e.ctrlKey || e.metaKey || e.repeat) return;
    var active = document.activeElement;
    if (active && active !== document.body && !box.contains(active)) return;
    if (/input|textarea|select/i.test(active.tagName)) return;
    var map = { ArrowLeft: "[data-compare-pick=left]", ArrowRight: "[data-compare-pick=right]", s: "[data-compare-skip]", u: "[data-compare-undo]" };
    var sel = map[e.key];
    if (!sel) return;
    var btn = box.querySelector(sel);
    if (btn && !btn.disabled) { e.preventDefault(); btn.click(); }
  });

  // ---- confirm-before-submit (destructive actions state their consequence) --------------------
  // ---- confirmation dialog (an in-app <dialog>, never window.confirm) ---------------------------
  var confirmDialog = document.getElementById("confirm-dialog");
  var confirmPending = null;  // { onOk } while the dialog is open
  function closeConfirm() { confirmPending = null; if (confirmDialog && confirmDialog.open) confirmDialog.close(); }
  // Opens the in-app dialog; falls back to window.confirm only where <dialog> is unsupported.
  function askConfirm(opts) {
    if (!confirmDialog || typeof confirmDialog.showModal !== "function") {
      if (window.confirm(opts.body)) opts.onOk();
      return;
    }
    confirmPending = { onOk: opts.onOk };
    confirmDialog.querySelector("[data-confirm-title]").textContent = opts.title;
    var bodyEl = confirmDialog.querySelector("[data-confirm-body]");
    bodyEl.textContent = opts.body || "";
    bodyEl.hidden = !opts.body;
    var ok = confirmDialog.querySelector("[data-confirm-ok]");
    ok.textContent = opts.label || opts.title;
    ok.className = "btn " + (opts.danger ? "btn--danger" : "btn--primary");
    confirmDialog.showModal();
    ok.focus();
  }
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-confirm")) return;
    if (form.dataset.confirmed === "1") { delete form.dataset.confirmed; return; }
    e.preventDefault();
    e.stopImmediatePropagation();  // htmx must not send the request until the person confirms
    var submitter = e.submitter || form.querySelector('button[type="submit"]');
    var label = submitter ? submitter.textContent.trim() : "Confirm";
    // "Remove X? It stops working." → title "Remove X?", body "It stops working."; a statement
    // without a question gets the button's verb as its title ("Close now?").
    var text = (form.getAttribute("data-confirm") || "").trim();
    var title = form.getAttribute("data-confirm-title");
    var body = text;
    if (!title) {
      var question = text.match(/^([^?]*\?)\s*([\s\S]*)$/);
      if (question && question[1].length <= 120) { title = question[1]; body = question[2]; }
      else title = label + "?";
    } else if (body.indexOf(title) === 0) body = body.slice(title.length).trim();
    askConfirm({
      title: title, body: body, label: label,
      danger: !!(submitter && (submitter.classList.contains("btn--danger") || submitter.classList.contains("btn--danger-text"))) ||
        /^(Remove|Delete|Revoke|Void|Withdraw|Leave|Archive|Unpublish)/i.test(label),
      onOk: function () { form.dataset.confirmed = "1"; form.requestSubmit(submitter || undefined); }
    });
  }, true);
  if (confirmDialog) {
    confirmDialog.querySelector("[data-confirm-cancel]").addEventListener("click", closeConfirm);
    confirmDialog.querySelector("[data-confirm-ok]").addEventListener("click", function () {
      var pending = confirmPending;
      closeConfirm();
      if (pending) pending.onOk();
    });
    confirmDialog.addEventListener("click", function (e) { if (e.target === confirmDialog) closeConfirm(); });
    confirmDialog.addEventListener("cancel", function () { confirmPending = null; });
  }
  // ---- unsaved changes: a guarded form that was edited asks before the page is left -------------
  var dirty = false;
  function isDirty() { return dirty && !!document.querySelector("form[data-guard]"); }
  document.addEventListener("input", function (e) { if (e.target.closest && e.target.closest("form[data-guard]")) dirty = true; });
  document.addEventListener("submit", function () { dirty = false; });
  window.addEventListener("beforeunload", function (e) { if (isDirty()) { e.preventDefault(); e.returnValue = ""; } });
  document.body.addEventListener("htmx:afterSettle", function (e) { if (e.detail.target === document.body) dirty = false; });  // a new page
  document.body.addEventListener("htmx:confirm", function (e) {
    var elt = e.detail.elt;  // only a link that loads another page is "leaving"; autosaves and polls are not
    if (!(elt && elt.tagName === "A" && String(e.detail.verb).toLowerCase() === "get")) return;
    if (!isDirty()) return;
    e.preventDefault();  // hold the navigation; the dialog decides
    askConfirm({
      title: "Leave this page?", body: "You have unsaved changes. Leaving now discards them.", label: "Leave without saving", danger: true,
      onOk: function () { dirty = false; e.detail.issueRequest(true); }
    });
  });

  // ---- a form being sent can't be sent twice (htmx forms handle their own state) -----------------------
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || e.defaultPrevented || form.target === "_blank") return;
    if (form.dataset.submitting) { e.preventDefault(); return; }
    form.dataset.submitting = "1";
    var btn = e.submitter;
    if (btn) { btn.classList.add("is-loading"); btn.setAttribute("aria-disabled", "true"); }
    // a download or a stopped request never leaves the form stuck
    setTimeout(function () { delete form.dataset.submitting; if (btn) { btn.classList.remove("is-loading"); btn.removeAttribute("aria-disabled"); } }, 8000);
  });
  window.addEventListener("pageshow", function () {
    document.querySelectorAll("form[data-submitting]").forEach(function (f) { delete f.dataset.submitting; });
    document.querySelectorAll(".btn.is-loading").forEach(function (b) { b.classList.remove("is-loading"); b.removeAttribute("aria-disabled"); });
  });
  // an error summary takes focus so keyboard and screen-reader users land on what to fix
  function focusErrorSummary() { var box = document.querySelector("[data-error-summary]"); if (box) box.focus(); }
  focusErrorSummary();
  document.body.addEventListener("htmx:load", function (e) { if (e.detail.elt === document.body) focusErrorSummary(); });

  // ---- sticky columns: stick when they fit under the header, otherwise scroll with the page --------
  var STICKY_TOP = 76, STICKY_GAP = 16, stickyTimer;
  function stickyColumns() {
    var wide = window.matchMedia("(min-width: 1024px)").matches;
    document.querySelectorAll("[data-sticky]").forEach(function (el) {
      el.classList.remove("is-sticky-top", "is-sticky-bottom");  // measure in normal flow
      if (!wide) return;
      el.classList.add(el.offsetHeight + STICKY_TOP + STICKY_GAP <= window.innerHeight ? "is-sticky-top" : "is-sticky-bottom");
    });
  }
  function stickySoon() { clearTimeout(stickyTimer); stickyTimer = setTimeout(stickyColumns, 120); }
  stickyColumns();
  window.addEventListener("resize", stickySoon);
  window.addEventListener("load", stickyColumns);
  document.body.addEventListener("htmx:afterSwap", stickyColumns);  // boosted pages and polled regions
  document.addEventListener("toggle", stickySoon, true);  // an opened <details> changes a column's height

  // ---- checkbox groups: select all / clear, live filter, count (plain checkboxes without JS) -------
  function checkCount(group) {
    var count = group.querySelector("[data-check-count]");
    if (!count) return;
    var boxes = group.querySelectorAll('input[type="checkbox"]'), on = 0;
    boxes.forEach(function (box) { if (box.checked) on += 1; });
    count.textContent = on + " of " + boxes.length + " selected";
  }
  document.addEventListener("click", function (e) {
    var btn = e.target.closest("[data-check-all], [data-check-none]");
    if (!btn) return;
    var group = btn.closest("[data-check-group]");
    if (!group) return;
    var on = btn.hasAttribute("data-check-all");
    group.querySelectorAll('input[type="checkbox"]:not(:disabled)').forEach(function (box) {
      var label = box.closest("label");
      if (!label || !label.hidden) box.checked = on;
    });
    checkCount(group);
  });
  document.addEventListener("input", function (e) {
    var input = e.target.closest && e.target.closest("[data-check-filter]");
    if (!input) return;
    var group = input.closest("[data-check-group]");
    if (!group) return;
    var needle = input.value.trim().toLowerCase();
    group.querySelectorAll("label.check").forEach(function (label) {
      label.hidden = !!needle && label.textContent.toLowerCase().indexOf(needle) === -1;
    });
  });
  document.addEventListener("change", function (e) {
    var group = e.target.closest && e.target.closest("[data-check-group]");
    if (group) checkCount(group);
  });
  function revealJsOnly(root) {
    (root || document).querySelectorAll("[data-js-only]").forEach(function (el) { el.removeAttribute("hidden"); });
    (root || document).querySelectorAll("[data-check-group]").forEach(checkCount);
  }
  revealJsOnly();
  document.body.addEventListener("htmx:load", function (e) { revealJsOnly(e.detail && e.detail.elt); });

  // ---- API reference: instant filter (the form still works without JavaScript) ------------------
  function filterApiReference(input) {
    var form = input.form;
    if (form && form.dataset.serverFiltered) return;  // the server already narrowed the page
    var needle = input.value.trim().toLowerCase();
    var shown = 0;
    var ops = document.querySelectorAll("[data-api-op]");
    ops.forEach(function (op) {
      var hit = !needle || (op.getAttribute("data-search") || "").indexOf(needle) !== -1;
      op.hidden = !hit;
      if (hit) shown += 1;
    });
    document.querySelectorAll("[data-api-section]").forEach(function (section) {
      section.hidden = !section.querySelector("[data-api-op]:not([hidden])");
    });
    document.querySelectorAll("[data-api-index-for]").forEach(function (row) {
      var op = document.getElementById(row.getAttribute("data-api-index-for"));
      row.hidden = !op || op.hidden;
    });
    document.querySelectorAll("[data-api-guide]").forEach(function (guide) { guide.hidden = !!needle; });
    var count = document.querySelector("[data-api-count]");
    if (count) count.textContent = shown + " of " + (count.getAttribute("data-total") || ops.length) + " endpoints";
  }
  document.addEventListener("input", function (e) {
    var input = e.target && e.target.closest ? e.target.closest("[data-api-filter]") : null;
    if (input) filterApiReference(input);
  });

  // ---- API reference: one language for every example (remembered) and a rail that follows ----------
  var LANG_KEY = "podium-api-lang";
  function applyApiLang(lang) {
    document.querySelectorAll('input.api-panel__radio[value="' + lang + '"]').forEach(function (radio) { radio.checked = true; });
  }
  document.addEventListener("change", function (e) {
    var radio = e.target;
    if (!radio.classList || !radio.classList.contains("api-panel__radio")) return;
    applyApiLang(radio.value);
    try { localStorage.setItem(LANG_KEY, radio.value); } catch (err) { /* private mode: still works for this page */ }
  });
  var apiSpy = null;
  function startScrollSpy() {
    if (apiSpy) { apiSpy.disconnect(); apiSpy = null; }
    var links = Array.prototype.slice.call(document.querySelectorAll('.rail__nav a[href^="#"]'));
    if (!links.length || !("IntersectionObserver" in window)) return;
    var targets = links.map(function (a) { return document.getElementById(a.getAttribute("href").slice(1)); }).filter(Boolean);
    targets.sort(function (a, b) { return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1; });
    var visible = {};
    apiSpy = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) { visible[entry.target.id] = entry.isIntersecting; });
      var current = null;
      for (var i = 0; i < targets.length; i++) { if (visible[targets[i].id]) { current = targets[i].id; break; } }
      if (!current) return;
      links.forEach(function (a) {
        if (a.getAttribute("href") === "#" + current) a.setAttribute("aria-current", "true");
        else a.removeAttribute("aria-current");
      });
    }, { rootMargin: "-88px 0px -55% 0px" });
    targets.forEach(function (target) { apiSpy.observe(target); });
  }
  function initApiReference() {
    if (!document.querySelector("[data-api-op]")) { if (apiSpy) { apiSpy.disconnect(); apiSpy = null; } return; }
    var stored = null;
    try { stored = localStorage.getItem(LANG_KEY); } catch (err) { stored = null; }
    if (stored && /^(curl|python|javascript)$/.test(stored)) applyApiLang(stored);
    startScrollSpy();
  }
  initApiReference();
  document.body.addEventListener("htmx:load", initApiReference);  // boosted visits to or from the page
})();

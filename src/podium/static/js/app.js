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
  // forms) swap in place: the message lives inside the control. JSON 4xx and 5xx keep the toast.
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    var xhr = e.detail.xhr;
    if (!xhr || xhr.status < 400 || xhr.status >= 500) return;
    var type = xhr.getResponseHeader("Content-Type") || "";
    if (type.indexOf("text/html") === -1) return;
    e.detail.shouldSwap = true;
    e.detail.isError = false;
  });
  document.body.addEventListener("htmx:responseError", function (e) {
    var xhr = e.detail.xhr;
    if (xhr.status === 401) { showAlert("warning", "Your session has expired. Sign in to continue."); return; }
    var msg = "Something went wrong (" + xhr.status + "). Try again.";
    try { var data = JSON.parse(xhr.responseText); if (data.error && data.error.message) msg = data.error.message; } catch (err) {
      var plain = xhr.getResponseHeader("X-Podium-Message"); if (plain) msg = plain;
    }
    showAlert(xhr.status >= 500 ? "critical" : "warning", msg);
  });
  document.body.addEventListener("htmx:sendError", function () {
    showAlert("critical", "You're offline. Your changes are kept on this page — try again when you're back.");
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
      if (note) note.textContent = "Times are in your local time" + (zone ? " (" + zone + ")" : "") + " and stored in UTC. Deadlines are enforced server-side to the minute.";
    });
  }
  localizeDateForms();
  document.body.addEventListener("htmx:load", localizeDateForms);

  // ---- local time beside UTC ------------------------------------------------------------------
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function fmtLocal(d) {  // one format everywhere, same month names as the server
    return d.getDate() + " " + MONTHS[d.getMonth()] + " " + d.getFullYear() + ", " + pad(d.getHours()) + ":" + pad(d.getMinutes());
  }
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
      t.textContent = t.hasAttribute("data-time-only") ? pad(d.getHours()) + ":" + pad(d.getMinutes()) : fmtLocal(d);
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
    if (!reviewForm) return;
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
    if (form && e.detail.successful) { var box = document.getElementById("review-errors"); if (box) box.innerHTML = ""; }
  });

  // ---- compare mode keyboard -------------------------------------------------------------------
  document.addEventListener("keydown", function (e) {
    var box = document.querySelector("[data-compare]");
    if (!box || /input|textarea|select/i.test(document.activeElement.tagName)) return;
    var map = { ArrowLeft: "[data-compare-pick=left]", ArrowRight: "[data-compare-pick=right]", s: "[data-compare-skip]", u: "[data-compare-undo]" };
    var sel = map[e.key];
    if (!sel) return;
    var btn = box.querySelector(sel);
    if (btn && !btn.disabled) { e.preventDefault(); btn.click(); }
  });

  // ---- confirm-before-submit (destructive actions state their consequence) --------------------
  // ---- confirmation dialog (an in-app <dialog>, never window.confirm) ---------------------------
  var confirmDialog = document.getElementById("confirm-dialog");
  var confirmPending = null;
  function closeConfirm() { confirmPending = null; if (confirmDialog && confirmDialog.open) confirmDialog.close(); }
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement) || !form.hasAttribute("data-confirm")) return;
    if (form.dataset.confirmed === "1") { delete form.dataset.confirmed; return; }
    e.preventDefault();
    e.stopImmediatePropagation();  // htmx must not send the request until the person confirms
    var submitter = e.submitter || form.querySelector('button[type="submit"]');
    var label = submitter ? submitter.textContent.trim() : "Confirm";
    if (!confirmDialog || typeof confirmDialog.showModal !== "function") {
      if (window.confirm(form.getAttribute("data-confirm"))) { form.dataset.confirmed = "1"; form.requestSubmit(submitter || undefined); }
      return;
    }
    confirmPending = { form: form, submitter: submitter };
    confirmDialog.querySelector("[data-confirm-title]").textContent = label;
    confirmDialog.querySelector("[data-confirm-body]").textContent = form.getAttribute("data-confirm");
    var ok = confirmDialog.querySelector("[data-confirm-ok]");
    ok.textContent = label;
    ok.className = "btn " + (submitter && submitter.classList.contains("btn--danger") ? "btn--danger" : "btn--primary");
    confirmDialog.showModal();
    ok.focus();
  }, true);
  if (confirmDialog) {
    confirmDialog.querySelector("[data-confirm-cancel]").addEventListener("click", closeConfirm);
    confirmDialog.querySelector("[data-confirm-ok]").addEventListener("click", function () {
      var pending = confirmPending;
      closeConfirm();
      if (!pending) return;
      pending.form.dataset.confirmed = "1";
      pending.form.requestSubmit(pending.submitter || undefined);
    });
    confirmDialog.addEventListener("click", function (e) { if (e.target === confirmDialog) closeConfirm(); });
    confirmDialog.addEventListener("cancel", function () { confirmPending = null; });
  }
  document.addEventListener("submit", function () { dirty = false; });
  window.addEventListener("beforeunload", function (e) { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  document.body.addEventListener("htmx:confirm", function (e) {
    if (dirty && e.detail.boosted && !window.confirm("You have unsaved changes. Leave this page?")) e.preventDefault();
    else if (e.detail.boosted) dirty = false;
  });
})();

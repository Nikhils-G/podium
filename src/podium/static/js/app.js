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

  // ---- local time beside UTC ------------------------------------------------------------------
  function enhanceTimes(root) {
    (root || document).querySelectorAll("time[datetime][data-local]").forEach(function (t) {
      if (t.dataset.done) return;
      var d = new Date(t.getAttribute("datetime"));
      if (isNaN(d)) return;
      var local = d.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
      var span = document.createElement("span");
      span.className = "muted";
      span.textContent = " (" + local + " local)";
      t.appendChild(span);
      t.dataset.done = "1";
    });
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
      if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { var s = reviewForm.querySelector("[data-submit-review]"); if (s) s.click(); }
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
      var btn = reviewForm.querySelector("[data-submit-review]"); if (btn) btn.click();
    }
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
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.hasAttribute("data-confirm") && !window.confirm(form.getAttribute("data-confirm"))) e.preventDefault();
  }, true);

  // ---- unsaved changes guard --------------------------------------------------------------------
  var dirty = false;
  document.addEventListener("input", function (e) { if (e.target.closest("form[data-guard]")) dirty = true; });
  document.addEventListener("submit", function () { dirty = false; });
  window.addEventListener("beforeunload", function (e) { if (dirty) { e.preventDefault(); e.returnValue = ""; } });
  document.body.addEventListener("htmx:confirm", function (e) {
    if (dirty && e.detail.boosted && !window.confirm("You have unsaved changes. Leave this page?")) e.preventDefault();
    else if (e.detail.boosted) dirty = false;
  });
})();

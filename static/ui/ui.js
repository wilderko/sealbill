// Interface behaviour for the staff and client pages. All of it is optional: every control here also
// works without JavaScript (the theme form posts to /theme/, every method panel is shown, nothing is
// copied but everything can be selected). No inline handlers: the CSP forbids them.
(function () {
  "use strict";

  var THEMES = ["black", "white", "dim"];
  // Enhanced controls swap the browser's own (untranslated) file picker text for the page's own.
  document.documentElement.classList.add("js");
  var BAR_COLORS = { white: "#f1f3f6", dim: "#1b1f26", black: "#0f1013" };

  function each(nodes, fn) { Array.prototype.forEach.call(nodes, fn); }

  // ---- Theme: set the same cookie the server would, and the attribute, without a round trip.
  function applyTheme(value) {
    var secure = location.protocol === "https:" ? "; Secure" : "";
    var root = document.documentElement;
    if (THEMES.indexOf(value) >= 0) {
      document.cookie = "theme=" + value + "; Max-Age=31536000; Path=/; SameSite=Lax" + secure;
      root.setAttribute("data-theme", value);
    } else {
      document.cookie = "theme=; Max-Age=0; Path=/; SameSite=Lax" + secure;
      root.removeAttribute("data-theme");
    }
    var scheme = document.querySelector('meta[name="color-scheme"]');
    if (scheme) { scheme.setAttribute("content", value === "white" ? "light" : (BAR_COLORS[value] ? "dark" : "light dark")); }
    if (BAR_COLORS[value]) {
      each(document.querySelectorAll('meta[name="theme-color"]'), function (meta) {
        meta.removeAttribute("media");
        meta.setAttribute("content", BAR_COLORS[value]);
      });
    }
    each(document.querySelectorAll("[data-theme-switch] button[name='theme']"), function (button) {
      button.setAttribute("aria-pressed", button.value === value || (value === "auto" && button.value === "auto") ? "true" : "false");
    });
  }

  document.addEventListener("submit", function (event) {
    var form = event.target;
    if (!form.hasAttribute || !form.hasAttribute("data-theme-switch")) { return; }
    var button = event.submitter;
    if (!button || button.name !== "theme") { return; } // an older browser: let the form post
    event.preventDefault();
    applyTheme(button.value);
  });

  // ---- Tabs: the payment methods. Without this script every panel is shown, one under another.
  function initTabs(root) {
    var list = root.querySelector("[data-tablist]");
    if (!list) { return; }
    var tabs = Array.prototype.slice.call(list.querySelectorAll('[role="tab"]'));
    var panels = tabs.map(function (tab) { return document.getElementById(tab.getAttribute("aria-controls")); });
    if (!tabs.length || panels.indexOf(null) >= 0) { return; }
    function select(index, focus) {
      tabs.forEach(function (tab, i) {
        var on = i === index;
        tab.setAttribute("aria-selected", on ? "true" : "false");
        tab.tabIndex = on ? 0 : -1;
        panels[i].hidden = !on;
      });
      if (focus) { tabs[index].focus(); }
    }
    var start = 0;
    var wanted = root.getAttribute("data-tabs-start");
    tabs.forEach(function (tab, i) { if (tab.getAttribute("aria-controls") === wanted) { start = i; } });
    list.hidden = false;
    root.setAttribute("data-enhanced", "");
    select(start, false);
    list.addEventListener("click", function (event) {
      var tab = event.target.closest('[role="tab"]');
      if (tab) { select(tabs.indexOf(tab), false); }
    });
    list.addEventListener("keydown", function (event) {
      var i = tabs.indexOf(document.activeElement);
      if (i < 0) { return; }
      var next = { ArrowRight: (i + 1) % tabs.length, ArrowLeft: (i - 1 + tabs.length) % tabs.length,
                   Home: 0, End: tabs.length - 1 }[event.key];
      if (next === undefined) { return; }
      event.preventDefault();
      select(next, true);
    });
  }

  // ---- Copy buttons: amounts, addresses, references, links. Delegated: htmx swaps the quote in.
  function copyText(text, done) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { legacyCopy(text, done); });
    } else {
      legacyCopy(text, done);
    }
  }
  function legacyCopy(text, done) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.className = "sr-only";
    document.body.appendChild(area);
    area.select();
    try { if (document.execCommand("copy")) { done(); } } catch (e) { /* nothing to do: the text stays selectable */ }
    document.body.removeChild(area);
  }

  document.addEventListener("click", function (event) {
    if (!event.target.closest) { return; }
    var choice = event.target.closest("button.choice");
    if (choice) {
      each(document.querySelectorAll("button.choice"), function (b) { b.setAttribute("aria-pressed", b === choice ? "true" : "false"); });
      return;
    }
    var button = event.target.closest("button[data-copy]");
    if (!button) { return; }
    var label = button.querySelector("[data-copy-label]") || button;
    if (!button.hasAttribute("data-label")) { button.setAttribute("data-label", label.textContent); }
    var original = button.getAttribute("data-label");
    var use = button.querySelector("use");
    copyText(button.getAttribute("data-copy"), function () {
      label.textContent = button.getAttribute("data-done") || original;
      button.classList.add("is-copied");
      if (use) { use.setAttribute("href", "#i-check"); }
      clearTimeout(button.copyTimer);
      button.copyTimer = setTimeout(function () {
        label.textContent = original;
        button.classList.remove("is-copied");
        if (use) { use.setAttribute("href", "#i-copy"); }
      }, 1600);
    });
  });

  // ---- data-confirm: a form (or its submit button) that carries it asks before it is sent.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var submitter = event.submitter;
    var text = (submitter && submitter.getAttribute && submitter.getAttribute("data-confirm")) ||
      (form && form.getAttribute && form.getAttribute("data-confirm"));
    if (text && !window.confirm(text)) { event.preventDefault(); }
  });

  // ---- File pickers: the chosen file's name, in place of the browser's "No file chosen".
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches("[data-file-pick] input[type='file']")) { return; }
    var out = input.parentNode.querySelector("[data-file-name]");
    if (out) { out.textContent = input.files && input.files.length ? input.files[0].name : out.getAttribute("data-empty"); }
  });

  // ---- A file input that names a size limit (data-max-bytes) says so beside the field, before the
  // browser sends a body the web server would refuse whole. The message is in data-max-bytes-message.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches("input[type='file'][data-max-bytes]")) { return; }
    var limit = parseInt(input.getAttribute("data-max-bytes"), 10);
    var big = input.files && input.files.length && limit > 0 && input.files[0].size > limit;
    input.setCustomValidity(big ? (input.getAttribute("data-max-bytes-message") || "The file is too large.") : "");
    if (big && input.reportValidity) { input.reportValidity(); }
  });

  each(document.querySelectorAll("[data-tabs]"), initTabs);
})();

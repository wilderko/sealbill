// The quote's countdown and validity bar on the crypto pay page. Delegated, because htmx swaps the
// quote in. (Copy buttons live in ui/ui.js, shared with the staff pages.)
(function () {
  "use strict";
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function clock(seconds) {
    var h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    return h > 0 ? h + ":" + pad(m) + ":" + pad(s) : m + ":" + pad(s);
  }
  function tick() {
    var nodes = document.querySelectorAll("time[data-expires]");
    for (var i = 0; i < nodes.length; i++) {
      var t = nodes[i], left = Math.floor((Date.parse(t.getAttribute("data-expires")) - Date.now()) / 1000);
      var out = t.parentNode.querySelector("[data-countdown]");
      var bar = t.parentNode.parentNode.querySelector("[data-validity]");
      if (!out) { continue; }
      if (isNaN(left)) { out.textContent = ""; continue; }
      if (bar) {
        var max = Number(bar.getAttribute("max")) || 1;
        bar.value = Math.max(0, Math.min(max, left));
        bar.classList.toggle("is-low", left < max * 0.1);
      }
      if (left <= 0) {
        out.textContent = t.getAttribute("data-expired");
        out.className = "countdown expired";
      } else {
        out.textContent = "(" + clock(left) + ")";
      }
    }
  }
  tick();
  setInterval(tick, 1000);
  document.addEventListener("htmx:afterSwap", tick);
})();

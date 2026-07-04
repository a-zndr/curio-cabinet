/* Curio-Cabinet client behaviors. Small, dependency-free where possible;
   Alpine handles component state, htmx handles server round-trips.
   Rule: no component-local Alpine state inside htmx swap targets —
   shared state lives in Alpine.store, markup binds from it. */

(function () {
  "use strict";

  // Theme toggle -----------------------------------------------------------
  document.addEventListener("click", function (event) {
    var btn = event.target.closest("#theme-toggle");
    if (!btn) return;
    var root = document.documentElement;
    var current =
      root.dataset.theme ||
      (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    var next = current === "dark" ? "light" : "dark";
    root.dataset.theme = next;
    localStorage.setItem("cc-theme", next);
  });

  // Conditional form groups (group.when in the config) ----------------------
  function syncConditionalGroups() {
    document.querySelectorAll("[data-when-field]").forEach(function (group) {
      var source = document.getElementById(group.dataset.whenField);
      if (!source) return;
      var allowed = JSON.parse(group.dataset.whenValues || "[]").map(String);
      var update = function () {
        group.hidden = allowed.indexOf(String(source.value)) === -1;
      };
      if (!source.dataset.whenBound) {
        source.dataset.whenBound = "1";
        source.addEventListener("input", update);
        source.addEventListener("change", update);
      }
      update();
    });
  }
  document.addEventListener("DOMContentLoaded", syncConditionalGroups);
  document.body.addEventListener("htmx:afterSwap", syncConditionalGroups);

  // Draft autosave: TEXT FIELDS ONLY (photos are not saved in drafts) -------
  var AUTOSAVE_MS = 1500;

  function autosaveKey(form) {
    return "cc-draft-" + location.pathname + "-" + form.dataset.autosave;
  }

  function initAutosave() {
    var form = document.querySelector("form[data-autosave]");
    if (!form) return;
    var key = autosaveKey(form);

    var saved = localStorage.getItem(key);
    if (saved && form.dataset.autosave === "new") {
      try {
        var values = JSON.parse(saved);
        Object.keys(values).forEach(function (name) {
          var input = form.elements[name];
          if (input && !input.value) input.value = values[name];
        });
      } catch (e) {
        /* corrupt draft: ignore */
      }
    }

    var timer = null;
    form.addEventListener("input", function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var data = {};
        Array.prototype.forEach.call(form.elements, function (el) {
          if (el.name && el.type !== "file" && el.type !== "hidden" && el.value) {
            data[el.name] = el.value;
          }
        });
        localStorage.setItem(key, JSON.stringify(data));
      }, AUTOSAVE_MS);
    });

    form.addEventListener("submit", function () {
      localStorage.removeItem(key);
    });
  }
  document.addEventListener("DOMContentLoaded", initAutosave);
})();

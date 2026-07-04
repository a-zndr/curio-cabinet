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

  // Drop empty params so pushed/bookmarked URLs stay clean (the filter form
  // always contains every range input; most are blank).
  document.body.addEventListener("htmx:configRequest", function (evt) {
    var p = evt.detail.parameters;
    Object.keys(p).forEach(function (k) {
      if (p[k] === "" || p[k] == null) delete p[k];
    });
  });

  // Confirmation dialogs (inline handlers are blocked by the strict CSP) ----
  document.addEventListener("submit", function (event) {
    var form = event.target.closest("form[data-confirm]");
    if (form && !confirm(form.dataset.confirm)) event.preventDefault();
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

  // Selection + share ------------------------------------------------------
  // Shared state, not per-element: survives htmx swaps and page navigation
  // via sessionStorage. Markup binds its checked/selected state FROM here.
  var Selection = (function () {
    var browse = document.querySelector("[data-selection-key]");
    var key = browse ? "cc-sel-" + browse.dataset.selectionKey : null;
    var ids = [];
    var active = false;

    function load() {
      if (!key) return;
      try {
        ids = JSON.parse(sessionStorage.getItem(key) || "[]");
      } catch (e) {
        ids = [];
      }
    }
    function save() {
      if (key) sessionStorage.setItem(key, JSON.stringify(ids));
    }
    function has(id) {
      return ids.indexOf(id) !== -1;
    }
    function toggle(id) {
      var i = ids.indexOf(id);
      if (i === -1) ids.push(id);
      else ids.splice(i, 1);
      save();
      render();
    }
    function clear() {
      ids = [];
      save();
      render();
    }
    function setActive(on) {
      active = on;
      document.body.classList.toggle("selecting", active);
      document
        .querySelectorAll("[data-select-col]")
        .forEach(function (el) {
          el.hidden = !active;
        });
      render();
    }

    function render() {
      var tray = document.querySelector("[data-selection-tray]");
      if (tray) {
        tray.hidden = !active || ids.length === 0;
        var count = tray.querySelector("[data-tray-count]");
        if (count)
          count.textContent =
            ids.length + " selected" + (ids.length >= 100 ? " (max)" : "");
      }
      document.querySelectorAll("[data-select-id]").forEach(function (el) {
        var on = has(el.dataset.selectId);
        el.classList.toggle("is-selected", on);
        var check = el.querySelector("[data-select-check]");
        if (check && check.type === "checkbox") check.checked = on;
      });
    }

    load();
    return {
      ids: function () {
        return ids;
      },
      toggle: toggle,
      clear: clear,
      setActive: setActive,
      isActive: function () {
        return active;
      },
      render: render,
    };
  })();

  // toggle selection mode
  document.addEventListener("click", function (event) {
    if (event.target.closest("[data-toggle-select]")) {
      Selection.setActive(!Selection.isActive());
    }
    if (event.target.closest("[data-selection-clear]")) {
      Selection.clear();
    }
  });

  // In selection mode, clicking a card toggles instead of navigating
  document.addEventListener(
    "click",
    function (event) {
      if (!Selection.isActive()) return;
      var el = event.target.closest("[data-select-id]");
      if (!el) return;
      if (event.target.closest("[data-select-check]") && event.target.type === "checkbox")
        return; // let the checkbox handle itself
      event.preventDefault();
      Selection.toggle(el.dataset.selectId);
    },
    true
  );

  document.addEventListener("change", function (event) {
    var check = event.target.closest("[data-select-check]");
    if (check && check.type === "checkbox") {
      var el = check.closest("[data-select-id]");
      if (el) Selection.toggle(el.dataset.selectId);
    }
  });

  // rebind visual state after every htmx swap
  document.body.addEventListener("htmx:afterSwap", function () {
    Selection.render();
  });

  // Share dialog
  function shareUrl() {
    var ids = Selection.ids().slice(0, 100);
    return location.origin + "/list?ids=" + encodeURIComponent(ids.join(","));
  }
  document.addEventListener("click", function (event) {
    var dialog = document.querySelector("[data-share-dialog]");
    if (event.target.closest("[data-share-open]") && dialog) {
      var url = shareUrl();
      dialog.querySelector("[data-share-url]").value = url;
      var cap = dialog.querySelector("[data-share-cap]");
      if (cap) cap.hidden = Selection.ids().length <= 100;
      var native = dialog.querySelector("[data-share-native]");
      if (native && navigator.share) native.hidden = false;
      if (typeof dialog.showModal === "function") dialog.showModal();
    }
    if (event.target.closest("[data-share-close]") && dialog) dialog.close();
    if (event.target.closest("[data-share-copy]") && dialog) {
      var field = dialog.querySelector("[data-share-url]");
      navigator.clipboard.writeText(field.value).then(function () {
        var btn = event.target.closest("[data-share-copy]");
        var was = btn.textContent;
        btn.textContent = "Copied!";
        setTimeout(function () {
          btn.textContent = was;
        }, 1500);
      });
    }
    if (event.target.closest("[data-share-native]")) {
      navigator.share({ url: shareUrl() }).catch(function () {});
    }
  });

  // Mobile filter sheet
  document.addEventListener("click", function (event) {
    var sheet = document.querySelector("[data-filter-sheet]");
    if (!sheet) return;
    if (event.target.closest("[data-open-filters]")) {
      sheet.hidden = false;
      document.body.style.overflow = "hidden";
    } else if (
      event.target === sheet ||
      event.target.closest("[data-close-filters]")
    ) {
      sheet.hidden = true;
      document.body.style.overflow = "";
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    Selection.render();
  });

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

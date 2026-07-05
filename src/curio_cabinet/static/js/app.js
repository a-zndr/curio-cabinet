/* Curio-Cabinet client behaviors. Vanilla JS only (no framework), so the
   strict CSP holds. htmx does the server round-trips; the URL is the single
   source of truth for browse state, and the small helpers below keep the
   toolbar/filter controls (which live outside the swapped #results) in sync
   with it. */

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

  // Filter/search/field-picker forms carry ONLY their own fields. On submit
  // we rebuild the request from the live URL (source of truth) minus the keys
  // this form owns, plus the form's current values — so no form ever holds
  // stale copies of the whole query. `data-owns` lists owned keys; a trailing
  // * means prefix (e.g. "f.*"). Empty params are dropped to keep URLs clean.
  document.body.addEventListener("htmx:configRequest", function (evt) {
    var el = evt.detail.elt;
    var form = el && el.closest ? el.closest("form[data-owns]") : null;
    if (form) {
      var owns = form.dataset.owns.split(/\s+/).filter(Boolean);
      var owned = function (k) {
        if (k === "page") return true;
        return owns.some(function (o) {
          return o.charAt(o.length - 1) === "*"
            ? k.indexOf(o.slice(0, -1)) === 0
            : k === o;
        });
      };
      var merged = {};
      new URLSearchParams(location.search).forEach(function (v, k) {
        if (owned(k)) return;
        if (merged[k] === undefined) merged[k] = v;
        else if (Array.isArray(merged[k])) merged[k].push(v);
        else merged[k] = [merged[k], v];
      });
      var fp = evt.detail.parameters;
      Object.keys(fp).forEach(function (k) {
        merged[k] = fp[k];
      });
      evt.detail.parameters = merged;
    }
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

  // View switcher lives outside the swapped #results region, so its active
  // state must be reconciled from the URL after each swap / history nav.
  function syncViewSwitch() {
    var current = new URLSearchParams(location.search).get("view") || "cards";
    document.querySelectorAll(".seg[data-view]").forEach(function (el) {
      el.classList.toggle("is-active", el.dataset.view === current);
    });
  }
  document.addEventListener("DOMContentLoaded", syncViewSwitch);
  document.body.addEventListener("htmx:afterSwap", syncViewSwitch);
  window.addEventListener("popstate", syncViewSwitch);

  // Chart bar sizes — set as a CSS custom property from data-bar, since the
  // strict CSP blocks inline style="width/height:x%". CSS maps --bar to the
  // right dimension per chart (bar width, histogram column height, etc.).
  function paintBars() {
    document.querySelectorAll("[data-bar]").forEach(function (el) {
      el.style.setProperty("--bar", el.dataset.bar + "%");
    });
  }
  document.addEventListener("DOMContentLoaded", paintBars);
  document.body.addEventListener("htmx:afterSwap", paintBars);

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
  var SHARE_TITLE_MAX = 30;
  function shareUrl() {
    var ids = Selection.ids().slice(0, 100);
    var url = location.origin + "/list?ids=" + encodeURIComponent(ids.join(","));
    var t = document.querySelector("[data-share-title]");
    var name = t && t.value.trim().slice(0, SHARE_TITLE_MAX);
    if (name) url += "&title=" + encodeURIComponent(name);
    return url;
  }
  function refreshShareUrl() {
    var f = document.querySelector("[data-share-url]");
    if (f) f.value = shareUrl();
  }
  document.addEventListener("input", function (event) {
    if (event.target.closest("[data-share-title]")) refreshShareUrl();
  });
  document.addEventListener("click", function (event) {
    var dialog = document.querySelector("[data-share-dialog]");
    if (event.target.closest("[data-share-open]") && dialog) {
      refreshShareUrl();
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

  // Filter panel (slide-over on desktop, bottom sheet on mobile) -----------
  function setPanel(open) {
    var sheet = document.querySelector("[data-filter-sheet]");
    if (!sheet) return;
    sheet.hidden = !open;
    document.body.classList.toggle("panel-open", open);
  }
  document.addEventListener("click", function (event) {
    var sheet = document.querySelector("[data-filter-sheet]");
    if (!sheet) return;
    if (event.target.closest("[data-open-filters]")) setPanel(true);
    else if (event.target === sheet || event.target.closest("[data-close-filters]"))
      setPanel(false);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") setPanel(false);
  });

  // Search-within a facet's checkbox list
  document.addEventListener("input", function (event) {
    var box = event.target.closest("[data-facet-search]");
    if (!box) return;
    var q = box.value.trim().toLowerCase();
    box
      .closest(".facet-body")
      .querySelectorAll(".facet-opt")
      .forEach(function (opt) {
        opt.hidden = q && (opt.dataset.optText || "").indexOf(q) === -1;
      });
  });

  // Live counts on facet headers + the Filters badge, from the filter form.
  function updateFilterCounts() {
    var form = document.querySelector(".filter-form");
    if (!form) return;
    var total = 0;
    form.querySelectorAll("[data-facet]").forEach(function (facet) {
      var n = facet.querySelectorAll("input[type=checkbox]:checked").length;
      var nums = facet.querySelectorAll("input[type=number]");
      var ranged = false;
      nums.forEach(function (i) {
        if (i.value.trim() !== "") ranged = true;
      });
      if (ranged) n += 1;
      total += n;
      var badge = facet.querySelector("[data-facet-count]");
      if (badge) badge.textContent = n ? String(n) : "";
      facet.classList.toggle("has-active", n > 0);
    });
    if (new URLSearchParams(location.search).get("q")) total += 1;
    document.querySelectorAll("[data-filter-count]").forEach(function (b) {
      b.textContent = total ? String(total) : "";
      b.hidden = total === 0;
    });
  }

  // Keep the FILTER and SEARCH controls in sync with the URL (source of truth)
  // after chip removal, clear-all, or any swap. The field picker is excluded:
  // when its param is absent the server renders the *default* selection, which
  // the URL can't express — so we must not override it here.
  function syncStateForms() {
    var p = new URLSearchParams(location.search);
    document
      .querySelectorAll(".filter-form input[type=checkbox]")
      .forEach(function (cb) {
        cb.checked = p.getAll(cb.name).indexOf(cb.value) !== -1;
      });
    document
      .querySelectorAll(".filter-form input[type=number], .search-form input[type=search]")
      .forEach(function (i) {
        if (document.activeElement !== i) i.value = p.get(i.name) || "";
      });
    updateFilterCounts();
  }
  document.addEventListener("input", function (event) {
    if (event.target.closest(".filter-form")) updateFilterCounts();
  });

  // Close an open toolbar dropdown (Fields/Columns) on outside click.
  document.addEventListener("click", function (event) {
    document.querySelectorAll("details.dd[open]").forEach(function (dd) {
      if (!dd.contains(event.target)) dd.open = false;
    });
  });

  document.addEventListener("DOMContentLoaded", function () {
    Selection.render();
    syncStateForms();
  });
  document.body.addEventListener("htmx:afterSwap", syncStateForms);
  window.addEventListener("popstate", syncStateForms);

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

  // Admin form: pre-fill a linked field (e.g. maker website) when a known
  // value (e.g. an existing maker) is chosen. Data from a JSON <script> tag.
  function initAutofill() {
    var el = document.getElementById("cc-autofill");
    if (!el) return;
    var data;
    try {
      data = JSON.parse(el.textContent);
    } catch (e) {
      return;
    }
    Object.keys(data).forEach(function (srcKey) {
      var src = document.getElementById("f-" + srcKey);
      var conf = data[srcKey];
      var tgt = document.getElementById("f-" + conf.target);
      if (!src || !tgt) return;
      var fill = function () {
        var v = conf.map[src.value];
        if (v && !tgt.value.trim()) tgt.value = v;
      };
      src.addEventListener("input", fill);
      src.addEventListener("change", fill);
    });
  }
  document.addEventListener("DOMContentLoaded", initAutofill);

  // Customize page: accent color picker. The native <input type=color> and a
  // hex field stay in sync, preset swatches set both, and the whole page
  // previews live (sets --accent-override app-wide; --accent-soft derives from
  // it in CSS, and we compute a readable --accent-contrast-override).
  function bindColor() {
    var picker = document.querySelector("[data-color-input]");
    var hex = document.querySelector("[data-color-hex]");
    if (!picker) return;

    var HEX = /^#[0-9a-fA-F]{6}$/;
    var contrast = function (h) {
      var r = parseInt(h.slice(1, 3), 16) / 255,
        g = parseInt(h.slice(3, 5), 16) / 255,
        b = parseInt(h.slice(5, 7), 16) / 255;
      return 0.2126 * r + 0.7152 * g + 0.0722 * b > 0.6 ? "#1c1917" : "#ffffff";
    };
    var preview = function (h) {
      var root = document.documentElement.style;
      root.setProperty("--accent-override", h);
      root.setProperty("--accent-contrast-override", contrast(h));
    };
    var setAll = function (h, from) {
      h = h.toLowerCase();
      if (from !== "picker") picker.value = h;
      if (from !== "hex" && hex) hex.value = h;
      preview(h);
    };

    if (!picker.dataset.bound) {
      picker.dataset.bound = "1";
      picker.addEventListener("input", function () {
        setAll(picker.value, "picker");
      });
    }
    if (hex && !hex.dataset.bound) {
      hex.dataset.bound = "1";
      hex.addEventListener("input", function () {
        var v = hex.value.trim();
        if (v && v[0] !== "#") v = "#" + v;
        if (HEX.test(v)) setAll(v, "hex");
      });
    }
    document.querySelectorAll("[data-color-preset]").forEach(function (btn) {
      btn.style.background = btn.dataset.colorPreset;
      if (btn.dataset.bound) return;
      btn.dataset.bound = "1";
      btn.addEventListener("click", function () {
        setAll(btn.dataset.colorPreset, "");
      });
    });
    preview(picker.value); // reflect current on load
  }
  document.addEventListener("DOMContentLoaded", bindColor);
})();

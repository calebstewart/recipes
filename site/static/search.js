/* Homepage search & tag filtering.
   Progressive enhancement: the cards are already in the HTML, the search UI
   ships `hidden` and is only revealed once this script runs. Vanilla, no deps. */
(function () {
  "use strict";

  var indexEl = document.getElementById("search-index");
  if (!indexEl) return;

  var records;
  try {
    records = JSON.parse(indexEl.textContent || indexEl.innerHTML || "[]");
  } catch (err) {
    return;
  }
  if (!Array.isArray(records)) return;

  var cards = Array.prototype.slice.call(document.querySelectorAll(".card"));
  if (!cards.length) return;

  var sections = Array.prototype.slice.call(
    document.querySelectorAll(".category")
  );

  /* ---------------------------------------------------------------- text */

  function normalize(value) {
    var text = String(value == null ? "" : value);
    if (text.normalize) {
      text = text.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
    }
    return text.toLowerCase().replace(/\s+/g, " ").trim();
  }

  function flatten(value, out) {
    if (value == null) return out;
    if (Array.isArray(value)) {
      for (var i = 0; i < value.length; i++) flatten(value[i], out);
    } else {
      out.push(String(value));
    }
    return out;
  }

  /* -------------------------------------------------------------- wiring */

  var byId = Object.create(null);
  records.forEach(function (record) {
    if (record && record.id) byId[record.id] = record;
  });

  var allTags = [];
  var seenTags = Object.create(null);

  cards.forEach(function (card) {
    var record = byId[card.getAttribute("data-id")] || {};
    var parts = flatten(
      [
        record.title,
        record.description,
        record.categoryLabel,
        record.category,
        record.tags,
        record.ingredients,
        record.time,
        record.servings
      ],
      []
    );
    if (!parts.length) parts.push(card.textContent || "");

    card.searchText = normalize(parts.join(" "));
    card.searchTags = flatten(record.tags, []).map(normalize);
    card.searchTags.forEach(function (tag) {
      if (tag && !seenTags[tag]) {
        seenTags[tag] = true;
        allTags.push(tag);
      }
    });
  });

  allTags.sort();

  /* --------------------------------------------------------- UI scaffold */

  var main = document.querySelector("#main") || document.body;
  var anchor = sections[0] || null;

  function insertBlock(el, after) {
    if (after && after.parentNode) {
      after.parentNode.insertBefore(el, after.nextSibling);
    } else if (anchor && anchor.parentNode) {
      anchor.parentNode.insertBefore(el, anchor);
    } else {
      main.appendChild(el);
    }
  }

  var form = document.querySelector("form.search, .search");
  if (!form) {
    form = document.createElement("form");
    form.className = "search";
    form.setAttribute("role", "search");
    insertBlock(form, null);
  }
  form.setAttribute("role", "search");
  form.addEventListener("submit", function (event) {
    event.preventDefault();
  });

  var input = form.querySelector("input[type='search'], input#q");
  if (!input) {
    input = document.createElement("input");
    input.type = "search";
    input.id = "q";
    input.placeholder = "Search recipes…";
    input.autocomplete = "off";
    form.insertBefore(input, form.firstChild);
  }
  input.setAttribute("aria-label", "Search recipes");

  var counter = form.querySelector(".result-count");
  if (!counter) {
    counter = document.querySelector(".result-count");
  }
  if (!counter) {
    counter = document.createElement("p");
    counter.className = "result-count";
    form.appendChild(counter);
  }
  counter.setAttribute("aria-live", "polite");

  var filters = document.querySelector(".tag-filters");
  if (!filters && allTags.length) {
    filters = document.createElement("div");
    filters.className = "tag-filters";
    insertBlock(filters, form);
  }

  if (filters) {
    filters.setAttribute("aria-label", "Filter by tag");
    if (!filters.querySelector(".chip")) {
      allTags.forEach(function (tag) {
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.setAttribute("data-tag", tag);
        chip.textContent = tag;
        filters.appendChild(chip);
      });
    }
  }

  var chips = filters
    ? Array.prototype.slice.call(filters.querySelectorAll(".chip"))
    : [];
  chips.forEach(function (chip) {
    if (chip.tagName === "BUTTON" && !chip.getAttribute("type")) {
      chip.type = "button";
    }
    chip.setAttribute("aria-pressed", "false");
  });

  var emptyState = document.querySelector(".empty-state");
  if (!emptyState) {
    emptyState = document.createElement("p");
    emptyState.className = "empty-state";
    emptyState.textContent = "No recipes match that search.";
    insertBlock(emptyState, filters || form);
  }
  emptyState.hidden = true;

  form.hidden = false;
  form.removeAttribute("hidden");
  if (filters && chips.length) {
    filters.hidden = false;
    filters.removeAttribute("hidden");
  } else if (filters) {
    filters.hidden = true;
  }

  /* The static "12 recipes" line is superseded by the live result count. */
  var staticCount = document.querySelector(".home-count");
  if (staticCount) staticCount.hidden = true;

  /* ------------------------------------------------------------ filtering */

  var activeTags = [];
  var total = cards.length;

  function noun(n) {
    return n === 1 ? "recipe" : "recipes";
  }

  function apply() {
    var terms = normalize(input.value).split(" ").filter(Boolean);
    var filtering = terms.length > 0 || activeTags.length > 0;
    var shown = 0;

    cards.forEach(function (card) {
      var text = card.searchText;
      var tags = card.searchTags;

      var matches =
        terms.every(function (term) {
          return text.indexOf(term) !== -1;
        }) &&
        activeTags.every(function (tag) {
          return tags.indexOf(tag) !== -1;
        });

      card.hidden = !matches;
      if (matches) shown++;
    });

    sections.forEach(function (section) {
      var visible = Array.prototype.some.call(
        section.querySelectorAll(".card"),
        function (card) {
          return !card.hidden;
        }
      );
      section.hidden = !visible;
    });

    counter.textContent = filtering
      ? shown + " of " + total + " " + noun(total)
      : total + " " + noun(total);

    emptyState.hidden = shown !== 0;
    syncUrl(terms.length ? input.value.trim() : "");
  }

  function syncUrl(query) {
    if (!window.history || !window.history.replaceState) return;
    try {
      var params = [];
      if (query) params.push("q=" + encodeURIComponent(query));
      activeTags.forEach(function (tag) {
        params.push("tag=" + encodeURIComponent(tag));
      });
      var url =
        window.location.pathname +
        (params.length ? "?" + params.join("&") : "") +
        window.location.hash;
      window.history.replaceState(null, "", url);
    } catch (err) {
      /* file:// and some sandboxes disallow this — filtering still works. */
    }
  }

  var timer = null;
  input.addEventListener("input", function () {
    if (timer) window.clearTimeout(timer);
    timer = window.setTimeout(function () {
      timer = null;
      apply();
    }, 120);
  });

  input.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && input.value) {
      input.value = "";
      apply();
    }
  });

  chips.forEach(function (chip) {
    chip.addEventListener("click", function () {
      var tag = normalize(chip.getAttribute("data-tag") || chip.textContent);
      var at = activeTags.indexOf(tag);
      if (at === -1) {
        activeTags.push(tag);
        chip.setAttribute("aria-pressed", "true");
      } else {
        activeTags.splice(at, 1);
        chip.setAttribute("aria-pressed", "false");
      }
      apply();
    });
  });

  /* ------------------------------------------------------------- restore */

  function restore() {
    var search = window.location.search || "";
    if (!search || search.length < 2) return;
    var pairs = search.slice(1).split("&");
    pairs.forEach(function (pair) {
      if (!pair) return;
      var eq = pair.indexOf("=");
      var key = eq === -1 ? pair : pair.slice(0, eq);
      var raw = eq === -1 ? "" : pair.slice(eq + 1);
      var value = "";
      try {
        value = decodeURIComponent(raw.replace(/\+/g, " "));
      } catch (err) {
        value = raw;
      }
      if (key === "q") {
        input.value = value;
      } else if (key === "tag" && value) {
        var tag = normalize(value);
        if (activeTags.indexOf(tag) === -1) activeTags.push(tag);
      }
    });

    chips.forEach(function (chip) {
      var tag = normalize(chip.getAttribute("data-tag") || chip.textContent);
      chip.setAttribute(
        "aria-pressed",
        activeTags.indexOf(tag) === -1 ? "false" : "true"
      );
    });
  }

  restore();
  apply();
})();

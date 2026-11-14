/* The plan page: pick the week's recipes and how many meals of each, and get
   the shopping list.

   build.py has already done the hard part. Every ingredient line arrives
   parsed — amount, unit family, catalog item or sub-recipe — in the JSON at
   the bottom of the page, along with an order in which every recipe comes
   before the recipes it uses. This file walks that order once, multiplying
   and handing needs down to sub-recipes, then adds up what is left.

   Two things are stored, apart on purpose:
   - the week (`recipes:plan`): meals per recipe, sub-recipes already on hand,
     and what has been checked off. "New week" clears it.
   - preferences (`recipes:plan-prefs`): optional ingredients left out, and
     which alternative was picked. These outlive the week, so the sausage stays
     out of the feta pasta until it is put back. */
(function () {
  "use strict";

  var root = document.getElementById("plan");
  var source = document.getElementById("shopping-data");
  if (!root || !source) return;

  var data;
  try {
    data = JSON.parse(source.textContent);
  } catch (err) {
    return;
  }

  var PLAN_KEY = "recipes:plan";
  var PREFS_KEY = "recipes:plan-prefs";

  var tabs = root.querySelector(".plan-tabs");
  var tabPlan = document.getElementById("tab-plan");
  var tabList = document.getElementById("tab-list");
  var panelPlan = document.getElementById("panel-plan");
  var panelList = document.getElementById("panel-list");

  var recipes = {};
  data.recipes.forEach(function (recipe) {
    recipes[recipe.id] = recipe;
  });

  var sections = {};
  data.sections.forEach(function (section) {
    sections[section.id] = section;
  });

  /* ------------------------------------------------------------- storage */

  function read(key) {
    try {
      var value = JSON.parse(window.localStorage.getItem(key) || "null");
      return value && typeof value === "object" ? value : {};
    } catch (err) {
      return {};
    }
  }

  function write(key, value) {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch (err) {
      /* private mode or a full quota: the page still works for this visit */
    }
  }

  var plan = read(PLAN_KEY);
  var prefs = read(PREFS_KEY);

  function normalise() {
    ["meals", "onHand", "checked"].forEach(function (key) {
      if (!plan[key] || typeof plan[key] !== "object") plan[key] = {};
    });
    ["optional", "choice"].forEach(function (key) {
      if (!prefs[key] || typeof prefs[key] !== "object") prefs[key] = {};
    });
    /* A recipe renamed or removed since the plan was made just drops out. */
    Object.keys(plan.meals).forEach(function (id) {
      var meals = Number(plan.meals[id]);
      if (!recipes[id] || !(meals > 0)) delete plan.meals[id];
      else plan.meals[id] = meals;
    });
  }
  normalise();

  function save() {
    write(PLAN_KEY, plan);
    write(PREFS_KEY, prefs);
  }

  /* Another tab (a recipe page's "Add to plan") changed the plan. */
  window.addEventListener("storage", function (event) {
    if (event.key !== PLAN_KEY && event.key !== PREFS_KEY) return;
    plan = read(PLAN_KEY);
    prefs = read(PREFS_KEY);
    normalise();
    render();
  });

  /* ------------------------------------------------------------- numbers */

  /* Kitchen fractions, the same way cooking mode writes a scaled amount. */
  var DENOMINATORS = [1, 2, 3, 4, 8];
  var SNAP = 1 / 128;

  function gcd(a, b) {
    while (b) {
      var rest = a % b;
      a = b;
      b = rest;
    }
    return a;
  }

  function quantity(value) {
    for (var i = 0; i < DENOMINATORS.length; i++) {
      var d = DENOMINATORS[i];
      var n = Math.round(value * d);
      if (n <= 0 || Math.abs(value * d - n) >= SNAP) continue;
      var factor = gcd(n, d);
      n /= factor;
      d /= factor;
      if (d === 1) return String(n);
      var whole = Math.floor(n / d);
      return (whole ? whole + " " : "") + (n - whole * d) + "/" + d;
    }
    return String(Math.round(value * 100) / 100);
  }

  /* Up to the next step, so the list never comes in a hair short: 1.1 c. of
     oil reads 1 1/4, and 4 1/2 garlic cloves is 5. */
  function roundUp(value, step) {
    /* The slack absorbs unit-conversion error: 3 oz. through grams and back
       is 3.0000001 oz., which must not round up to 3 1/4. */
    return Math.ceil(value / step - 1e-6) * step;
  }

  var VOLUME = { "tsp.": 1, "Tbsp.": 3, "c.": 48, "pt.": 96, "qt.": 192, ml: 0.202884, l: 202.884 };
  var WEIGHT = { g: 1, kg: 1000, "oz.": 28.349523125, "lb.": 453.59237 };

  /* A summed volume or weight, in the unit the recipes used if they all
     agree, and otherwise the one a shopper would say. */
  function measure(family, total, units, fine) {
    var table = family === "volume" ? VOLUME : WEIGHT;
    var used = Object.keys(units || {});
    var unit;
    if (used.length === 1) {
      unit = used[0];
    } else if (family === "volume") {
      /* Cups from half a cup up; below that, 6 Tbsp. says more than 3/8 c. */
      unit = total >= 24 ? "c." : total >= 3 ? "Tbsp." : "tsp.";
    } else if (used.length && used.every(function (u) { return u === "g" || u === "kg"; })) {
      unit = total >= 1000 ? "kg" : "g";
    } else {
      unit = total >= WEIGHT["lb."] ? "lb." : "oz.";
    }
    var step = unit === "g" || unit === "ml" ? 5 : unit === "tsp." ? 0.125 : unit === "Tbsp." ? 0.5 : 0.25;
    /* `fine` is for an amount shown for reference next to what to buy, which
       should agree with the count beside it: 10 Tbsp. of juice is 5/8 c. next
       to 5 lemons, not the 3/4 c. a shopping amount would round to. */
    if (fine && step === 0.25) step = 0.125;
    return quantity(roundUp(total / table[unit], step)) + " " + unit;
  }

  function counted(amount, unit, plural) {
    var n = roundUp(amount, 1);
    var word = n === 1 ? unit : plural;
    return word ? n + " " + word : String(n);
  }

  function times(value) {
    return quantity(value) + "×";
  }

  function capitalise(text) {
    return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
  }

  function plural(n, one, many) {
    return n + " " + (n === 1 ? one : many);
  }

  /* ---------------------------------------------------------- choices */

  function prefKey(recipe, line) {
    return recipe.id + "#" + line.id;
  }

  function included(recipe, line) {
    if (!line.optional) return true;
    return prefs.optional[prefKey(recipe, line)] !== false;
  }

  /* The alternative in use, by label so reordering them in the recipe does
     not flip anyone's choice. Null means nobody has picked yet. */
  function chosen(recipe, line) {
    if (line.choices.length === 1) return line.choices[0];
    var label = prefs.choice[prefKey(recipe, line)];
    for (var i = 0; i < line.choices.length; i++) {
      if (line.choices[i].label === label) return line.choices[i];
    }
    return null;
  }

  function choose(recipe, line, label) {
    prefs.choice[prefKey(recipe, line)] = label;
    save();
    render();
  }

  /* -------------------------------------------------------------- the sum */

  /* Walks recipes parents-first. Each recipe in use is made at `mult` times
     its written amounts: meals ÷ servings for one picked directly, plus
     batches for whatever the recipes above it need — rounded up to the half
     batch, since nobody makes 0.3 of a harissa paste but a batch and a half of
     tempeh is a real thing to make, and zero when it is already on hand. */
  var BATCH_STEP = 0.5;

  function compute() {
    var info = {};
    var need = {};
    var neededBy = {};
    var rows = {};
    var pending = [];

    data.order.forEach(function (id) {
      var recipe = recipes[id];
      var direct = plan.meals[id] ? plan.meals[id] / recipe.servings : 0;
      var needed = need[id] || 0;
      if (!direct && !needed) return;

      var onHand = needed > 0 && !!plan.onHand[id];
      var batches = needed > 0 && !onHand
        ? Math.max(roundUp(needed, BATCH_STEP), recipe.minBatch || 0)
        : 0;
      var mult = direct + batches;
      info[id] = {
        direct: direct,
        needed: needed,
        batches: batches,
        onHand: onHand,
        mult: mult,
        by: Object.keys(neededBy[id] || {})
      };
      if (!mult) return;

      recipe.lines.forEach(function (line) {
        if (!included(recipe, line)) return;
        var choice = chosen(recipe, line);
        if (!choice) {
          pending.push({ recipe: recipe, line: line });
          return;
        }
        if (choice.recipe) {
          if (choice.batches) {
            need[choice.recipe] = (need[choice.recipe] || 0) + choice.batches * mult;
            neededBy[choice.recipe] = neededBy[choice.recipe] || {};
            neededBy[choice.recipe][id] = true;
          }
          return;
        }
        addToRow(rows, recipe, line, choice, mult);
      });
    });

    return { info: info, rows: rows, pending: pending };
  }

  function addToRow(rows, recipe, line, choice, mult) {
    var item = data.items[choice.item] || { name: choice.item, section: "other" };
    if (item.section === "none") return;
    var row = rows[choice.item];
    if (!row) {
      row = rows[choice.item] = {
        key: choice.item,
        name: item.name,
        section: sections[item.section] ? item.section : "other",
        families: {},
        order: [],
        sources: []
      };
    }
    row.sources.push({ recipe: recipe, line: line, choice: choice, mult: mult });
    if (choice.amount == null) return;

    var family = row.families[choice.family];
    if (!family) {
      family = row.families[choice.family] = { total: 0, units: {}, unit: choice.unit, plural: choice.plural };
      row.order.push(choice.family);
    }
    family.total += choice.base * mult;
    family.units[choice.unit] = true;
  }

  /* One family's total as the recipes measured it: `9 Tbsp.`, `13 cloves`. */
  function familyAmount(row, name, fine) {
    var family = row.families[name];
    if (name === "volume" || name === "weight") return measure(name, family.total, family.units, fine);
    return counted(family.total, family.unit, family.plural);
  }

  /* What a row says to buy. With a `buy:` in ingredients.yml, every family it
     can convert is summed into the purchase unit and rounded up there — 13
     cloves is 2 heads — with the recipe amounts kept beside it in parentheses
     so you know how much of it the week uses. A family it cannot convert is
     listed after, the same as a row with no `buy:` at all.

     Returns the two halves apart — what to buy, and the recipe amounts it
     covers — so the row can put the second on a line of its own. */
  function rowParts(row) {
    var item = data.items[row.key] || {};
    var buy = item.buy;
    if (!buy) {
      return {
        buy: row.order.map(function (name) {
          return familyAmount(row, name);
        }).join(" + "),
        covers: ""
      };
    }

    var bought = 0;
    var used = [];
    var converted = false;
    var rest = [];
    row.order.forEach(function (name) {
      var family = row.families[name];
      if (buy.per[name]) {
        bought += family.total / buy.per[name];
        used.push(familyAmount(row, name, true));
        converted = true;
      } else if (name === buy.family) {
        /* Already in the unit it is bought in: counts toward it as it is, and
           is listed with the converted amounts so the total adds up on sight. */
        bought += family.total;
        used.push(familyAmount(row, name, true));
      } else {
        rest.push(familyAmount(row, name));
      }
    });
    if (!bought) return { buy: rest.join(" + "), covers: "" };

    var text;
    if (buy.kind === "measure") {
      var units = {};
      units[buy.one] = true;
      text = measure(buy.family, bought, units) + (buy.suffix ? " " + buy.suffix : "");
    } else {
      var n = roundUp(bought, 1);
      text = buy.bare ? String(n) : n + " " + (n === 1 ? buy.one : buy.many);
    }
    /* Only worth showing when something was converted; a row bought just as
       the recipes wrote it would only repeat itself. */
    return { buy: [text].concat(rest).join(" + "), covers: converted ? used.join(" + ") : "" };
  }

  /* The same on one line, for the copied list: `2 heads (14 cloves)`. */
  function rowAmount(row) {
    var parts = rowParts(row);
    return parts.covers ? parts.buy + " (" + parts.covers + ")" : parts.buy;
  }

  /* ------------------------------------------------------------------ DOM */

  function el(tag, attrs, children) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (name) {
      var value = attrs[name];
      if (value == null || value === false) return;
      if (name === "text") node.textContent = value;
      else if (name.slice(0, 2) === "on") node.addEventListener(name.slice(2), value);
      else node.setAttribute(name, value === true ? "" : value);
    });
    (children || []).forEach(function (child) {
      if (child == null || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  /* Swap a panel's contents. Nulls in the list are the parts left out. */
  function fill(panel, nodes) {
    panel.replaceChildren.apply(panel, nodes.filter(Boolean));
  }

  function button(label, attrs, onClick) {
    attrs = attrs || {};
    attrs.type = "button";
    attrs.text = label;
    attrs.onclick = onClick;
    return el("button", attrs);
  }

  var announcer = el("span", { class: "visually-hidden", role: "status", "aria-live": "polite" });
  document.body.appendChild(announcer);

  function announce(message) {
    announcer.textContent = "";
    window.setTimeout(function () {
      announcer.textContent = message;
    }, 30);
  }

  /* -------------------------------------------------------------- planning */

  function setMeals(id, meals) {
    if (meals > 0) plan.meals[id] = meals;
    else delete plan.meals[id];
    save();
    render();
  }

  function stepper(recipe) {
    var meals = plan.meals[recipe.id];
    var title = recipe.title;
    return el("div", { class: "meal-stepper" }, [
      button("−", {
        class: "step-btn",
        "data-key": "less:" + recipe.id,
        "aria-label": meals > 1 ? "One fewer meal of " + title : "Remove " + title + " from the plan"
      }, function () {
        setMeals(recipe.id, meals - 1);
      }),
      el("span", { class: "meal-count", "aria-live": "polite" }, [
        el("strong", { text: String(meals) }),
        meals === 1 ? " meal" : " meals"
      ]),
      button("+", { class: "step-btn", "data-key": "more:" + recipe.id, "aria-label": "One more meal of " + title }, function () {
        setMeals(recipe.id, meals + 1);
      })
    ]);
  }

  /* Optional lines as checkboxes and alternatives as a row of pills. The pill
     row doubles as the swap: tapping the other one swaps. */
  function options(recipe) {
    var lines = recipe.lines.filter(function (line) {
      return line.optional || line.choices.length > 1;
    });
    if (!lines.length) return null;

    return el("ul", { class: "plan-options" }, lines.map(function (line) {
      var key = prefKey(recipe, line);
      var on = included(recipe, line);
      var parts = [];

      if (line.optional) {
        parts.push(el("label", { class: "opt-toggle" }, [
          el("input", {
            type: "checkbox",
            checked: on,
            "data-key": "opt:" + key,
            onchange: function (event) {
              prefs.optional[key] = event.target.checked;
              save();
              render();
            }
          }),
          el("span", { text: line.text })
        ]));
      }

      if (line.choices.length > 1 && on) {
        var current = chosen(recipe, line);
        var lead = line.optional ? null : el("span", { class: "choice-line", text: line.text });
        parts.push(lead);
        parts.push(el("div", {
          class: "choice-row" + (current ? "" : " needs-choice"),
          role: "group",
          "aria-label": "Choose for " + line.text
        }, [
          current ? null : el("span", { class: "choice-prompt", text: "Pick one:" })
        ].concat(line.choices.map(function (choice) {
          return button(choice.label, {
            class: "chip",
            "aria-pressed": current === choice ? "true" : "false",
            "data-key": "choice:" + key + ":" + choice.label
          }, function () {
            choose(recipe, line, choice.label);
          });
        }))));
      }

      return el("li", null, parts);
    }));
  }

  function plannedCard(recipe, state) {
    var meals = plan.meals[recipe.id];
    var note = times(state.direct) + " the recipe (" + (recipe.servingsLabel || "1 serving") + ")";
    return el("li", { class: "plan-card" }, [
      el("div", { class: "plan-card-head" }, [
        el("a", { class: "plan-card-title", href: recipe.url, text: recipe.title }),
        stepper(recipe)
      ]),
      el("p", { class: "plan-card-note", text: meals ? note : "" }),
      options(recipe)
    ]);
  }

  function madeCard(recipe, state) {
    var by = state.by.map(function (id) {
      return recipes[id].title;
    });
    var uses = "";
    if (recipe.yieldBase && (recipe.yieldFamily === "volume" || recipe.yieldFamily === "weight")) {
      uses = "Uses about " + measure(recipe.yieldFamily, state.needed * recipe.yieldBase) +
        " of the " + recipe.yield + " a batch makes";
    } else {
      var share = roundUp(state.needed, 0.05);
      uses = "Needs " + quantity(share) + (share > 1 ? " batches" : " batch");
    }
    var made = state.onHand
      ? "On hand — nothing to buy for it."
      : uses + "; making " + quantity(state.batches) + (state.batches > 1 ? " batches" : " batch") + ".";
    var key = "onhand:" + recipe.id;

    return el("li", { class: "plan-card made-card" + (state.onHand ? " is-on-hand" : "") }, [
      el("div", { class: "plan-card-head" }, [
        el("a", { class: "plan-card-title", href: recipe.url, text: recipe.title }),
        el("label", { class: "on-hand" }, [
          el("input", {
            type: "checkbox",
            checked: state.onHand,
            "data-key": key,
            onchange: function (event) {
              if (event.target.checked) plan.onHand[recipe.id] = true;
              else delete plan.onHand[recipe.id];
              save();
              render();
            }
          }),
          " I have this on hand"
        ])
      ]),
      el("p", { class: "plan-card-note" }, [
        "For " + by.join(" and ") + ". ",
        made
      ]),
      /* A recipe also planned directly already shows its options above. */
      state.onHand || plan.meals[recipe.id] ? null : options(recipe)
    ]);
  }

  function renderPlan(result) {
    var planned = data.order.filter(function (id) {
      return plan.meals[id];
    });
    /* Category order for the cards, which is the order the home page uses. */
    var byCategory = data.recipes.map(function (recipe) {
      return recipe.id;
    });
    planned.sort(function (a, b) {
      return byCategory.indexOf(a) - byCategory.indexOf(b);
    });
    var made = byCategory.filter(function (id) {
      return result.info[id] && result.info[id].needed > 0;
    });

    var nodes = [];

    if (planned.length) {
      var mealCount = planned.reduce(function (sum, id) {
        return sum + plan.meals[id];
      }, 0);
      nodes.push(el("h2", { class: "plan-heading", text: "This week" }));
      nodes.push(el("p", { class: "plan-sub", text: plural(mealCount, "meal", "meals") + " from " + plural(planned.length, "recipe", "recipes") }));
      nodes.push(el("ul", { class: "plan-cards" }, planned.map(function (id) {
        return plannedCard(recipes[id], result.info[id]);
      })));
    } else {
      nodes.push(el("p", { class: "empty-state", text: "Nothing planned yet. Add recipes below; each starts at one batch's worth of meals." }));
    }

    if (made.length) {
      nodes.push(el("h2", { class: "plan-heading", text: "Made along the way" }));
      nodes.push(el("p", { class: "plan-sub", text: "Sub-recipes the plan needs, rounded up to the half batch." }));
      nodes.push(el("ul", { class: "plan-cards" }, made.map(function (id) {
        return madeCard(recipes[id], result.info[id]);
      })));
    }

    if (planned.length) {
      nodes.push(el("div", { class: "plan-actions" }, [
        button("See the shopping list", { class: "cook-toggle" }, function () {
          showTab("list", true);
        })
      ]));
    }

    nodes.push(el("h2", { class: "plan-heading", text: "Add recipes" }));
    var current = null;
    var list = null;
    data.recipes.forEach(function (recipe) {
      if (recipe.category !== current) {
        current = recipe.category;
        nodes.push(el("h3", { class: "group-title", text: recipe.categoryLabel }));
        list = el("ul", { class: "add-list" });
        nodes.push(list);
      }
      var inPlan = !!plan.meals[recipe.id];
      list.appendChild(el("li", null, [
        el("a", { href: recipe.url, text: recipe.title }),
        el("span", { class: "add-meta", text: recipe.servingsLabel }),
        inPlan
          ? el("span", { class: "add-done", text: plural(plan.meals[recipe.id], "meal", "meals") })
          : button("Add", { class: "scale-btn", "data-key": "add:" + recipe.id, "aria-label": "Add " + recipe.title }, function () {
            setMeals(recipe.id, Math.max(1, Math.round(recipe.servings)));
            announce(recipe.title + " added");
          })
      ]));
    });

    fill(panelPlan, nodes);
  }

  /* ------------------------------------------------------------- the list */

  function sortedRows(result) {
    var bySection = {};
    Object.keys(result.rows).forEach(function (key) {
      var row = result.rows[key];
      (bySection[row.section] = bySection[row.section] || []).push(row);
    });
    Object.keys(bySection).forEach(function (id) {
      bySection[id].sort(function (a, b) {
        return a.name.localeCompare(b.name);
      });
    });
    return bySection;
  }

  function sourceLine(source) {
    var recipe = source.recipe;
    var line = source.line;
    var bits = [
      el("span", { class: "source-recipe", text: recipe.title + (source.mult === 1 ? "" : " (" + times(source.mult) + ")") }),
      ": ",
      el("span", { text: source.choice.text })
    ];
    if (line.choices.length > 1) {
      var next = line.choices[(line.choices.indexOf(source.choice) + 1) % line.choices.length];
      bits.push(" ");
      bits.push(button("Swap to " + next.label, {
        class: "swap-btn",
        "data-key": "swap:" + prefKey(recipe, line)
      }, function () {
        choose(recipe, line, next.label);
        announce("Swapped to " + next.label + " in " + recipe.title);
      }));
    }
    return el("li", null, bits);
  }

  function rowNode(row) {
    var checked = !!plan.checked[row.key];
    var parts = rowParts(row);
    var detailsId = "src-" + row.key.replace(/[^a-z0-9]+/gi, "-");
    var open = expanded[row.key];
    return el("li", { class: "shop-row" + (checked ? " is-checked" : "") }, [
      el("label", { class: "shop-item" }, [
        el("input", {
          type: "checkbox",
          checked: checked,
          "data-key": "check:" + row.key,
          onchange: function (event) {
            if (event.target.checked) plan.checked[row.key] = true;
            else delete plan.checked[row.key];
            save();
            render();
          }
        }),
        el("span", { class: "shop-name", text: capitalise(row.name) }),
        el("span", { class: "shop-amount" }, [
          parts.buy,
          parts.covers ? el("span", { class: "shop-covers", text: "for " + parts.covers }) : null
        ])
      ]),
      button(open ? "Hide" : "From", {
        class: "src-toggle",
        "aria-expanded": open ? "true" : "false",
        "aria-controls": detailsId,
        "aria-label": (open ? "Hide where " : "Show where ") + row.name + " comes from",
        "data-key": "src:" + row.key
      }, function () {
        expanded[row.key] = !open;
        render();
      }),
      el("ul", { class: "shop-sources", id: detailsId, hidden: !open }, row.sources.map(sourceLine))
    ]);
  }

  /* Which rows have their sources showing. Belongs to the visit, not the plan. */
  var expanded = {};

  function pendingNode(pending) {
    if (!pending.length) return null;
    return el("div", { class: "pending" }, [
      el("h2", { class: "plan-heading", text: pending.length === 1 ? "One thing to decide" : pending.length + " things to decide" }),
      el("p", { class: "plan-sub", text: "These lines give a choice. Pick once and it is remembered; swap any time." }),
      el("ul", { class: "plan-options" }, pending.map(function (entry) {
        var recipe = entry.recipe;
        var line = entry.line;
        return el("li", null, [
          el("span", { class: "choice-line", text: recipe.title + ": " + line.text }),
          el("div", { class: "choice-row needs-choice", role: "group", "aria-label": "Choose for " + line.text },
            line.choices.map(function (choice) {
              return button(choice.label, {
                class: "chip",
                "aria-pressed": "false",
                "data-key": "choice:" + prefKey(recipe, line) + ":" + choice.label
              }, function () {
                choose(recipe, line, choice.label);
              });
            }))
        ]);
      }))
    ]);
  }

  function listText(result) {
    var bySection = sortedRows(result);
    var out = ["Shopping list"];
    data.sections.forEach(function (section) {
      var rows = (bySection[section.id] || []).filter(function (row) {
        return !plan.checked[row.key];
      });
      if (!rows.length) return;
      out.push("");
      out.push(section.label.toUpperCase());
      rows.forEach(function (row) {
        var amount = rowAmount(row);
        out.push("- " + capitalise(row.name) + (amount ? " — " + amount : ""));
      });
    });
    return out.join("\n");
  }

  function share(result) {
    var text = listText(result);
    if (navigator.share) {
      navigator.share({ title: "Shopping list", text: text }).catch(function () {});
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        announce("Shopping list copied");
        flash("Copied");
      }, function () {
        announce("Copying failed");
      });
    }
  }

  var shareLabel = null;
  function flash(label) {
    if (!shareLabel) return;
    shareLabel.textContent = label;
    window.setTimeout(function () {
      if (shareLabel) shareLabel.textContent = navigator.share ? "Share" : "Copy as text";
    }, 2000);
  }

  function renderList(result) {
    var nodes = [];
    var bySection = sortedRows(result);
    var keys = Object.keys(result.rows);

    if (!Object.keys(plan.meals).length) {
      nodes.push(el("p", { class: "empty-state" }, [
        "No recipes planned. ",
        button("Plan the week", { class: "link-btn" }, function () {
          showTab("plan", true);
        })
      ]));
      fill(panelList, nodes);
      return;
    }

    nodes.push(pendingNode(result.pending));

    var left = keys.filter(function (key) {
      return !plan.checked[key] && result.rows[key].section !== "pantry";
    }).length;
    shareLabel = el("span", { text: navigator.share ? "Share" : "Copy as text" });
    nodes.push(el("div", { class: "list-bar" }, [
      el("p", { class: "plan-sub", text: left ? plural(left, "thing", "things") + " to get" : "Everything is in the cart." }),
      el("div", { class: "list-actions" }, [
        el("button", { type: "button", class: "scale-btn", onclick: function () { share(result); } }, [shareLabel]),
        Object.keys(plan.checked).length
          ? button("Uncheck all", { class: "scale-btn" }, function () {
            plan.checked = {};
            save();
            render();
          })
          : null
      ])
    ]));

    data.sections.forEach(function (section) {
      var rows = bySection[section.id];
      if (!rows || !rows.length) return;
      /* Checked rows sink, so what is still to find stays at the top. */
      rows = rows.filter(function (row) { return !plan.checked[row.key]; })
        .concat(rows.filter(function (row) { return plan.checked[row.key]; }));
      var list = el("ul", { class: "shop-list" }, rows.map(rowNode));
      if (section.pantry) {
        nodes.push(el("details", {
          class: "pantry",
          open: pantryOpen,
          ontoggle: function (event) { pantryOpen = event.target.open; }
        }, [
          el("summary", null, [
            el("span", { class: "group-title", text: section.label }),
            el("span", { class: "plan-sub", text: " — " + plural(rows.length, "item", "items") + ", worth a look before you go" })
          ]),
          list
        ]));
      } else {
        nodes.push(el("h3", { class: "group-title", text: section.label }));
        nodes.push(list);
      }
    });

    nodes.push(el("div", { class: "plan-actions" }, [
      button("New week", { class: "scale-btn" }, function () {
        if (!window.confirm("Clear this week's plan and checkmarks? Your picks and left-out ingredients are kept.")) return;
        plan = { meals: {}, onHand: {}, checked: {} };
        save();
        showTab("plan", true);
      })
    ]));

    fill(panelList, nodes);
  }

  var pantryOpen = false;

  /* ----------------------------------------------------------------- tabs */

  function showTab(name, user) {
    plan.tab = name;
    save();
    tabPlan.setAttribute("aria-selected", name === "plan" ? "true" : "false");
    tabList.setAttribute("aria-selected", name === "list" ? "true" : "false");
    tabPlan.tabIndex = name === "plan" ? 0 : -1;
    tabList.tabIndex = name === "list" ? 0 : -1;
    panelPlan.hidden = name !== "plan";
    panelList.hidden = name !== "list";
    render();
    if (user) {
      (name === "plan" ? tabPlan : tabList).focus();
      window.scrollTo(0, 0);
    }
  }

  tabPlan.addEventListener("click", function () {
    showTab("plan", true);
  });
  tabList.addEventListener("click", function () {
    showTab("list", true);
  });
  tabs.addEventListener("keydown", function (event) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    showTab(plan.tab === "plan" ? "list" : "plan", true);
  });

  /* --------------------------------------------------------------- render */

  /* Every change redraws both panels from the state. A button that was
     focused is focused again afterwards, found by its data-key, so stepping a
     count with the keyboard keeps its place. */
  function render() {
    var active = document.activeElement;
    var key = active && active.getAttribute && active.getAttribute("data-key");

    var result = compute();
    renderPlan(result);
    renderList(result);

    var count = Object.keys(result.rows).filter(function (k) {
      return !plan.checked[k] && result.rows[k].section !== "pantry";
    }).length;
    tabList.textContent = count ? "Shopping list (" + count + ")" : "Shopping list";

    if (key) {
      var again = root.querySelector('[data-key="' + key.replace(/["\\]/g, "\\$&") + '"]');
      if (again) again.focus();
    }
  }

  tabs.hidden = false;
  showTab(plan.tab === "list" ? "list" : "plan", false);
})();

/* Recipe page behaviour: copy the import URL to the clipboard, and add the
   recipe to the week's plan.
   Everything else on the page works without JS. */
(function () {
  "use strict";

  var buttons = Array.prototype.slice.call(
    document.querySelectorAll(".copy-btn[data-copy-target]")
  );
  if (!buttons.length) return;

  var announcer = null;

  function announce(message) {
    if (!announcer) {
      announcer = document.createElement("span");
      announcer.className = "visually-hidden";
      announcer.setAttribute("role", "status");
      announcer.setAttribute("aria-live", "polite");
      document.body.appendChild(announcer);
    }
    /* Clearing first makes repeat announcements fire again. */
    announcer.textContent = "";
    window.setTimeout(function () {
      announcer.textContent = message;
    }, 30);
  }

  function legacyCopy(field) {
    var scratch = document.createElement("textarea");
    scratch.value = field.value != null ? field.value : field.textContent || "";
    scratch.setAttribute("readonly", "readonly");
    scratch.style.position = "fixed";
    scratch.style.top = "-1000px";
    scratch.style.opacity = "0";
    document.body.appendChild(scratch);

    var selection = document.getSelection();
    var previous = selection && selection.rangeCount ? selection.getRangeAt(0) : null;

    scratch.select();
    scratch.setSelectionRange(0, scratch.value.length);

    var ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (err) {
      ok = false;
    }

    document.body.removeChild(scratch);
    if (previous && selection) {
      selection.removeAllRanges();
      selection.addRange(previous);
    }
    return ok;
  }

  function flash(button, label) {
    if (button.resetTimer) window.clearTimeout(button.resetTimer);
    if (!button.originalLabel) button.originalLabel = button.textContent;
    button.textContent = label;
    button.setAttribute("data-copied", "true");
    button.resetTimer = window.setTimeout(function () {
      button.textContent = button.originalLabel;
      button.removeAttribute("data-copied");
      button.resetTimer = null;
    }, 2000);
  }

  function succeed(button) {
    flash(button, "Copied");
    announce("Import URL copied to clipboard");
  }

  function fail(button, field) {
    flash(button, "Press ⌘C");
    announce("Copying failed — the URL is selected, press Control or Command C");
    try {
      field.focus();
      if (field.select) field.select();
    } catch (err) {
      /* nothing else to try */
    }
  }

  buttons.forEach(function (button) {
    button.addEventListener("click", function () {
      var selector = button.getAttribute("data-copy-target");
      if (!selector) return;

      var field;
      try {
        field = document.querySelector(selector);
      } catch (err) {
        field = null;
      }
      if (!field) return;

      var text = field.value != null ? field.value : field.textContent || "";
      if (!text) return;

      /* Secure contexts (https, localhost, file in some browsers). */
      if (
        window.navigator &&
        navigator.clipboard &&
        typeof navigator.clipboard.writeText === "function"
      ) {
        navigator.clipboard.writeText(text).then(
          function () {
            succeed(button);
          },
          function () {
            if (legacyCopy(field)) succeed(button);
            else fail(button, field);
          }
        );
        return;
      }

      /* Non-secure context: fall back to the old selection-based copy. */
      if (legacyCopy(field)) succeed(button);
      else fail(button, field);
    });
  });
})();

/* "Add to plan": puts this recipe on the plan page at one batch's worth of
   meals, or says how many meals it is already down for. The plan itself lives
   in localStorage under the same key plan.js reads. */
(function () {
  "use strict";

  var recipe = document.querySelector(".recipe[data-recipe-id]");
  if (!recipe) return;

  var KEY = "recipes:plan";
  var id = recipe.getAttribute("data-recipe-id");
  var servings = Math.max(1, Math.round(parseFloat(recipe.getAttribute("data-servings")) || 1));
  var planUrl = recipe.getAttribute("data-plan-url");

  function read() {
    try {
      var plan = JSON.parse(window.localStorage.getItem(KEY) || "null");
      return plan && typeof plan === "object" ? plan : {};
    } catch (err) {
      return null;
    }
  }

  /* Storage that cannot be read cannot be written either; no button then. */
  if (read() === null) return;

  var box = document.createElement("span");
  box.className = "plan-add";

  function draw() {
    var plan = read() || {};
    var meals = plan.meals && plan.meals[id];
    box.textContent = "";
    if (meals > 0) {
      var link = document.createElement("a");
      link.className = "plan-add-btn";
      link.href = planUrl;
      link.textContent = "In this week’s plan · " + meals + (meals === 1 ? " meal" : " meals");
      box.appendChild(link);
      return;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.className = "plan-add-btn";
    button.textContent = "Add to plan";
    button.addEventListener("click", function () {
      var plan = read() || {};
      if (!plan.meals || typeof plan.meals !== "object") plan.meals = {};
      plan.meals[id] = servings;
      try {
        window.localStorage.setItem(KEY, JSON.stringify(plan));
      } catch (err) {
        return;
      }
      draw();
      var link = box.querySelector("a");
      if (link) link.focus();
    });
    box.appendChild(button);
  }

  window.addEventListener("storage", function (event) {
    if (event.key === KEY) draw();
  });

  /* After every deferred script, so cooking mode's button row exists to join. */
  document.addEventListener("DOMContentLoaded", function () {
    var head = recipe.querySelector(".recipe-head");
    if (!head) return;
    var actions = head.querySelector(".cook-actions");
    if (!actions) {
      actions = document.createElement("div");
      actions.className = "cook-actions";
      var meta = head.querySelector(".recipe-meta");
      if (meta && meta.nextSibling) head.insertBefore(actions, meta.nextSibling);
      else head.appendChild(actions);
    }
    actions.appendChild(box);
    draw();
  });
})();

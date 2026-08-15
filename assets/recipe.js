/* Recipe page behaviour: copy the import URL to the clipboard.
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

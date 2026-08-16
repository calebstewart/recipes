/* Cooking mode: check ingredients off, scale the batch, run step timers, keep
   the screen awake.

   Everything here is an enhancement. Without JavaScript the page is the plain
   recipe: no toggle, no timer buttons, and the ingredient checkboxes stay
   disabled and hidden, which is how build.py emits them. */
(function () {
  "use strict";

  var recipe = document.querySelector(".recipe");
  if (!recipe) return;

  var head = recipe.querySelector(".recipe-head");
  if (!head) return;

  function slice(nodes) {
    return Array.prototype.slice.call(nodes);
  }

  var boxes = slice(recipe.querySelectorAll('.ing-list input[type="checkbox"]'));
  var timerGroups = slice(recipe.querySelectorAll(".step-timers"));
  var steps = slice(document.querySelectorAll(".step-list > li"));

  /* Every quantity build.py could parse: the ingredient amounts and the serving
     count. `written` is what the recipe says, kept so 1× is the page verbatim
     rather than the formatter's idea of it. */
  var amounts = slice(recipe.querySelectorAll(".amount"))
    .map(function (el) {
      var high = parseFloat(el.getAttribute("data-amount-high"));
      return {
        el: el,
        written: el.textContent,
        low: parseFloat(el.getAttribute("data-amount")),
        high: isFinite(high) ? high : null,
        sep: el.getAttribute("data-amount-sep") || " to "
      };
    })
    .filter(function (amount) {
      return isFinite(amount.low) && amount.low > 0;
    });

  if (!boxes.length && !timerGroups.length) return;

  var STORAGE_KEY = "recipes:cook:" + window.location.pathname;
  var TICK_MS = 250;

  /* A finished timer keeps sounding until it is dismissed or this long passes */
  var ALARM_MAX_MS = 60 * 1000;
  var ALARM_GAP_S = 4; // seconds between bursts
  var ALARM_PEAK = 0.6; // gain at the top of a pip

  /* Multipliers worth a button. Anything finer is arithmetic the cook can do
     faster than they can tap. */
  var SCALES = [
    { value: 0.5, label: "½×", name: "half" },
    { value: 1, label: "1×", name: "the written amounts" },
    { value: 1.5, label: "1½×", name: "one and a half times" },
    { value: 2, label: "2×", name: "double" },
    { value: 3, label: "3×", name: "triple" }
  ];

  var cooking = false;
  var timers = [];
  var nextId = 1;
  var ticker = null;
  var wakeLock = null;
  var wakeState = "unknown"; // "unknown" | "on" | "off"
  var audio = null;
  var askedNotify = false;
  var baseTitle = document.title;
  var recipeName = (recipe.querySelector(".recipe-title") || {}).textContent || baseTitle;
  var announcer = null;

  var scale = 1;
  var scaleGroup = null;
  var scaleButtons = [];

  var toggle = null;
  var bar = null;
  var barList = null;
  var barHint = null;

  /* ---------------------------------------------------------------- utils */

  function announce(message) {
    if (!announcer) {
      announcer = document.createElement("span");
      announcer.className = "visually-hidden";
      announcer.setAttribute("role", "status");
      announcer.setAttribute("aria-live", "polite");
      document.body.appendChild(announcer);
    }
    announcer.textContent = "";
    window.setTimeout(function () {
      announcer.textContent = message;
    }, 30);
  }

  function clock(ms) {
    var total = Math.max(0, Math.round(ms / 1000));
    var hours = Math.floor(total / 3600);
    var minutes = Math.floor((total % 3600) / 60);
    var seconds = total % 60;
    function pad(value) {
      return value < 10 ? "0" + value : String(value);
    }
    if (hours) return hours + ":" + pad(minutes) + ":" + pad(seconds);
    return minutes + ":" + pad(seconds);
  }

  /* Step numbers follow the CSS counter, which keeps counting across grouped
     lists rather than restarting at 1. */
  function stepNumber(el) {
    var node = el;
    /* An exact class-token test, not a substring one: the button's own wrapper
       is `.step-timers`, which a substring match would mistake for the step. */
    while (node && node !== document.body) {
      if (node.classList && node.classList.contains("step")) {
        var index = steps.indexOf(node);
        return index === -1 ? 0 : index + 1;
      }
      node = node.parentNode;
    }
    return 0;
  }

  /* ----------------------------------------------------------------- scale */

  /* Denominators to try, simplest first. Past the obvious ones: fifths because
     a portion of a batch is written that way, and sixths and sixteenths because
     that is what a third and three eighths become when halved. */
  var DENOMINATORS = [1, 2, 3, 4, 5, 6, 8, 16];

  /* How far off a denominator may land and still be used. Wide enough to
     absorb the rounding in a value like 1/3, tight enough that the number
     shown is never more than a hair from the real one. */
  var SNAP = 1 / 128;

  function gcd(a, b) {
    while (b) {
      var rest = a % b;
      a = b;
      b = rest;
    }
    return a;
  }

  /* A scaled amount the way a recipe writes it: `3`, `1 1/2`, `2/3`. Decimals
     are the fallback for anything that will not sit on a kitchen fraction. */
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

  function drawAmounts() {
    amounts.forEach(function (amount) {
      if (scale === 1) {
        amount.el.textContent = amount.written;
        return;
      }
      var text = quantity(amount.low * scale);
      if (amount.high) text += amount.sep + quantity(amount.high * scale);
      amount.el.textContent = text;
    });
    /* Marks the numbers as no longer the ones on the page, so a glance at a
       propped-up phone cannot mistake a scaled list for the written one. */
    if (scale === 1) document.body.classList.remove("scaled");
    else document.body.classList.add("scaled");
  }

  function setScale(value, restoring) {
    var choice = null;
    SCALES.forEach(function (option) {
      if (option.value === value) choice = option;
    });
    if (!choice) return; // an unknown multiplier from an older stored session

    scale = choice.value;
    scaleButtons.forEach(function (button) {
      var on = parseFloat(button.getAttribute("data-scale")) === scale;
      button.setAttribute("aria-pressed", on ? "true" : "false");
    });
    drawAmounts();

    if (restoring) return;
    announce(
      scale === 1
        ? "Ingredients back to the written amounts."
        : "Ingredients scaled to " + choice.name + "."
    );
    save();
  }

  function buildScale() {
    if (!amounts.length) return null;
    var section = recipe.querySelector(".ingredients");
    if (!section) return null;

    var group = document.createElement("div");
    group.className = "ing-scale";
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Scale the ingredients");
    /* Like the timer buttons: it does nothing outside cooking mode, so it is
       not on screen outside cooking mode. */
    group.hidden = true;

    var caption = document.createElement("span");
    caption.className = "ing-scale-label";
    caption.textContent = "Batch";
    group.appendChild(caption);

    SCALES.forEach(function (option) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "scale-btn";
      button.textContent = option.label;
      button.setAttribute("data-scale", String(option.value));
      button.setAttribute("aria-pressed", option.value === scale ? "true" : "false");
      button.setAttribute("aria-label", "Scale ingredients to " + option.name);
      button.addEventListener("click", function () {
        setScale(option.value, false);
      });
      scaleButtons.push(button);
      group.appendChild(button);
    });

    var heading = section.querySelector("h2");
    if (heading && heading.nextSibling) section.insertBefore(group, heading.nextSibling);
    else section.appendChild(group);
    return group;
  }

  /* --------------------------------------------------------------- storage */

  function save() {
    var checked = [];
    boxes.forEach(function (box, index) {
      if (box.checked) checked.push(index);
    });

    var stored = timers.map(function (timer) {
      return {
        seconds: timer.seconds,
        label: timer.label,
        step: timer.step,
        endsAt: timer.endsAt,
        remaining: timer.remaining,
        state: timer.state
      };
    });

    try {
      if (!cooking && !stored.length && !checked.length) {
        window.localStorage.removeItem(STORAGE_KEY);
        return;
      }
      window.localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({
          mode: cooking,
          count: boxes.length,
          checked: checked,
          scale: scale,
          timers: stored
        })
      );
    } catch (err) {
      /* private mode, file://, or a full quota — the feature still works */
    }
  }

  function load() {
    try {
      var raw = window.localStorage.getItem(STORAGE_KEY);
      if (!raw) return null;
      var data = JSON.parse(raw);
      return data && typeof data === "object" ? data : null;
    } catch (err) {
      return null;
    }
  }

  /* ----------------------------------------------------------------- alarm */

  function unlockAudio() {
    var Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return;
    try {
      if (!audio) audio = new Ctor();
      if (audio.state === "suspended" && audio.resume) audio.resume();
    } catch (err) {
      audio = null;
    }
  }

  /* A burst of three pips, repeating every few seconds until the timer is
     dismissed or a minute has passed — whichever comes first. Synthesised
     rather than an audio file, so there is nothing extra to cache and it works
     offline like the rest of the site.

     Every burst is scheduled on the audio clock up front rather than fired from
     a repeating setTimeout, because a background tab throttles timers and would
     silence the repeats exactly when the cook has walked away from the phone.
     The scheduled nodes are kept so a dismissal can stop them early. */
  function scheduleAlarm(timer) {
    if (!audio) return;
    try {
      if (audio.state === "suspended" && audio.resume) audio.resume();

      var nodes = [];
      var bursts = Math.ceil(ALARM_MAX_MS / 1000 / ALARM_GAP_S);

      for (var b = 0; b < bursts; b++) {
        for (var i = 0; i < 3; i++) {
          var start = audio.currentTime + b * ALARM_GAP_S + i * 0.28;
          var osc = audio.createOscillator();
          var gain = audio.createGain();
          osc.type = "sine";
          osc.frequency.value = 880;
          gain.gain.setValueAtTime(0.0001, start);
          gain.gain.exponentialRampToValueAtTime(ALARM_PEAK, start + 0.02);
          gain.gain.exponentialRampToValueAtTime(0.0001, start + 0.18);
          osc.connect(gain);
          gain.connect(audio.destination);
          osc.start(start);
          osc.stop(start + 0.2);
          nodes.push(osc);
        }
      }

      timer.alarmNodes = nodes;
    } catch (err) {
      /* nothing to fall back to; the notification and vibration remain */
    }
  }

  /* Vibration cannot be scheduled ahead the way audio can, so it repeats on an
     interval and stops itself at the one-minute mark. */
  function scheduleBuzz(timer) {
    if (!navigator.vibrate) return;

    function buzz() {
      try {
        navigator.vibrate([300, 150, 300, 150, 500]);
      } catch (err) {
        /* iOS Safari has no vibration at all */
      }
    }

    buzz();
    var until = Date.now() + ALARM_MAX_MS;
    timer.buzzTicker = window.setInterval(function () {
      if (Date.now() >= until) {
        window.clearInterval(timer.buzzTicker);
        timer.buzzTicker = null;
        return;
      }
      buzz();
    }, ALARM_GAP_S * 1000);
  }

  function stopAlarm(timer) {
    if (timer.alarmNodes) {
      timer.alarmNodes.forEach(function (osc) {
        try {
          osc.stop();
        } catch (err) {
          /* already finished */
        }
      });
      timer.alarmNodes = null;
    }
    if (timer.buzzTicker) {
      window.clearInterval(timer.buzzTicker);
      timer.buzzTicker = null;
    }
    if (navigator.vibrate) {
      try {
        navigator.vibrate(0);
      } catch (err) {
        /* nothing to cancel */
      }
    }
  }

  function askNotify() {
    if (askedNotify) return;
    askedNotify = true;
    if (typeof window.Notification === "undefined") return;
    if (window.Notification.permission !== "default") return;
    try {
      var request = window.Notification.requestPermission();
      if (request && request.then) request.then(function () {}, function () {});
    } catch (err) {
      /* older callback-style implementations; not worth supporting */
    }
  }

  function notify(timer) {
    if (typeof window.Notification === "undefined") return;
    if (window.Notification.permission !== "granted") return;

    var title = "Timer done — " + timer.label;
    var options = {
      /* The recipe's own heading, not document.title — by the time an alarm
         fires the title already carries the "timer done" prefix. */
      body: "Step " + timer.step + " · " + recipeName,
      tag: "recipe-timer-" + timer.id,
      renotify: true,
      requireInteraction: true,
      vibrate: [300, 150, 300],
      data: { url: window.location.href, timerId: timer.id }
    };

    /* Android Chrome throws on `new Notification()`, so the service worker is
       the only path that works there. The constructor is the fallback for
       desktop browsers without a controlling worker. */
    if (navigator.serviceWorker && navigator.serviceWorker.ready) {
      navigator.serviceWorker.ready.then(
        function (registration) {
          registration.showNotification(title, options);
        },
        function () {}
      );
      return;
    }

    try {
      /* Kept so dismissing the row can close it, the way the service worker
         path does through getNotifications(). */
      timer.notification = new window.Notification(title, options);
      timer.notification.addEventListener("close", function () {
        if (timers.indexOf(timer) !== -1) removeTimer(timer);
      });
    } catch (err) {
      /* no notification, but the beep and the bar still fired */
    }
  }

  /* Dismissing a timer takes its notification with it. */
  function closeNotification(timer) {
    if (timer.notification) {
      try {
        timer.notification.close();
      } catch (err) {
        /* already gone */
      }
      timer.notification = null;
    }

    if (!navigator.serviceWorker || !navigator.serviceWorker.ready) return;
    navigator.serviceWorker.ready.then(function (registration) {
      if (!registration.getNotifications) return;
      registration.getNotifications({ tag: "recipe-timer-" + timer.id }).then(
        function (list) {
          list.forEach(function (note) {
            note.close();
          });
        },
        function () {}
      );
    }, function () {});
  }

  function retitle() {
    var done = timers.filter(function (timer) {
      return timer.state === "done";
    }).length;
    /* Catches a backgrounded desktop tab, where the tab title is the only
       thing the cook can see. */
    document.title = done ? "⏰ " + done + " timer done — " + baseTitle : baseTitle;
  }

  function alarm(timer) {
    timer.state = "done";
    timer.endsAt = null;
    timer.remaining = 0;
    drawRow(timer);
    retitle();

    notify(timer);
    scheduleAlarm(timer);
    scheduleBuzz(timer);
    announce("Timer done: step " + timer.step + ", " + timer.label);
  }

  /* ------------------------------------------------------------- wake lock */

  /* The bar is the only sign that cooking mode is on, so it says so — and it
     reports what the screen is actually doing rather than what the browser
     claims to support. */
  function setHint() {
    if (!barHint) return;
    var text = "Cooking mode";
    if (wakeState === "on") text += " — screen stays awake";
    else if (wakeState === "off") text += " — screen may sleep";
    barHint.textContent = text;
  }

  function acquireWake() {
    if (!navigator.wakeLock) {
      wakeState = "off";
      setHint();
      return;
    }
    if (document.visibilityState !== "visible") return;
    try {
      navigator.wakeLock.request("screen").then(
        function (lock) {
          wakeLock = lock;
          wakeState = "on";
          setHint();
          lock.addEventListener("release", function () {
            wakeLock = null;
            /* Released whenever the page hides; visibilitychange re-requests */
            if (cooking) {
              wakeState = "off";
              setHint();
            }
          });
        },
        function () {
          /* refused: low battery, or a policy that forbids it */
          wakeState = "off";
          setHint();
        }
      );
    } catch (err) {
      wakeState = "off";
      setHint();
    }
  }

  function releaseWake() {
    wakeState = "unknown";
    if (!wakeLock) return;
    try {
      wakeLock.release();
    } catch (err) {
      /* already gone */
    }
    wakeLock = null;
  }

  /* ------------------------------------------------------------------- bar */

  function buildBar() {
    if (bar) return bar;

    bar = document.createElement("aside");
    bar.className = "cook-bar";
    bar.setAttribute("aria-label", "Timers");
    bar.hidden = true;

    barList = document.createElement("ul");
    barList.className = "timer-list";

    barHint = document.createElement("p");
    barHint.className = "cook-hint";

    /* Timers stack above the hint, so the hint is a steady footer line and the
       bar simply grows upward as timers are added. */
    bar.appendChild(barList);
    bar.appendChild(barHint);
    document.body.appendChild(bar);
    setHint();
    return bar;
  }

  function showBar() {
    buildBar();
    bar.hidden = false;
    document.body.classList.add("cook-bar-open");
  }

  /* On a phone the bar overlays the page, so the body needs to reserve room for
     it — more once timers are stacked above the hint line. */
  function syncBarHeight() {
    if (timers.length) document.body.classList.add("has-timers");
    else document.body.classList.remove("has-timers");
  }

  function hideBarIfEmpty() {
    /* The bar belongs to the mode, not to the timers: it stays up as long as
       cooking mode is on, so there is always something saying so. */
    if (cooking || timers.length || !bar) return;
    bar.hidden = true;
    document.body.classList.remove("cook-bar-open");
  }

  function drawRow(timer) {
    if (!timer.row) return;
    timer.row.setAttribute("data-state", timer.state);

    var left =
      timer.state === "paused"
        ? timer.remaining
        : timer.endsAt
          ? timer.endsAt - Date.now()
          : 0;
    timer.clock.textContent = timer.state === "done" ? "Done" : clock(left);

    if (timer.state === "done") {
      timer.pause.hidden = true;
      timer.cancel.textContent = "Dismiss";
      timer.cancel.setAttribute("aria-label", "Dismiss finished timer for step " + timer.step);
      return;
    }

    timer.pause.hidden = false;
    timer.pause.textContent = timer.state === "paused" ? "Resume" : "Pause";
    timer.pause.setAttribute("aria-pressed", timer.state === "paused" ? "true" : "false");
  }

  function addRow(timer) {
    var row = document.createElement("li");
    row.className = "timer";

    var name = document.createElement("span");
    name.className = "timer-name";
    name.textContent = "Step " + timer.step + " · " + timer.label;

    var readout = document.createElement("span");
    readout.className = "timer-clock";
    /* Not a live region: a countdown announced every second is unusable. The
       finish is announced once, through announce(). */
    readout.setAttribute("aria-live", "off");

    var pause = document.createElement("button");
    pause.type = "button";
    pause.className = "timer-pause";
    pause.setAttribute("aria-label", "Pause timer for step " + timer.step);

    var cancel = document.createElement("button");
    cancel.type = "button";
    cancel.className = "timer-cancel";
    cancel.textContent = "Cancel";
    cancel.setAttribute("aria-label", "Cancel timer for step " + timer.step);

    pause.addEventListener("click", function () {
      if (timer.state === "running") {
        timer.remaining = Math.max(0, timer.endsAt - Date.now());
        timer.endsAt = null;
        timer.state = "paused";
      } else if (timer.state === "paused") {
        timer.endsAt = Date.now() + timer.remaining;
        timer.state = "running";
        ensureTicker();
      }
      drawRow(timer);
      save();
    });

    cancel.addEventListener("click", function () {
      removeTimer(timer);
    });

    row.appendChild(name);
    row.appendChild(readout);
    row.appendChild(pause);
    row.appendChild(cancel);

    timer.row = row;
    timer.clock = readout;
    timer.pause = pause;
    timer.cancel = cancel;

    buildBar();
    barList.appendChild(row);
    drawRow(timer);
    syncBarHeight();
  }

  function removeTimer(timer) {
    stopAlarm(timer);
    closeNotification(timer);
    var index = timers.indexOf(timer);
    if (index !== -1) timers.splice(index, 1);
    if (timer.row && timer.row.parentNode) timer.row.parentNode.removeChild(timer.row);
    if (timer.button) timer.button.removeAttribute("data-running");
    stopTickerIfIdle();
    retitle();
    syncBarHeight();
    hideBarIfEmpty();
    save();
  }

  /* ---------------------------------------------------------------- timers */

  function tick() {
    var now = Date.now();
    timers.forEach(function (timer) {
      if (timer.state !== "running") return;
      if (now >= timer.endsAt) {
        alarm(timer);
        return;
      }
      timer.clock.textContent = clock(timer.endsAt - now);
    });
    stopTickerIfIdle();
  }

  function ensureTicker() {
    if (ticker) return;
    ticker = window.setInterval(tick, TICK_MS);
  }

  function stopTickerIfIdle() {
    var live = timers.some(function (timer) {
      return timer.state === "running";
    });
    if (live || !ticker) return;
    window.clearInterval(ticker);
    ticker = null;
  }

  function startTimer(seconds, label, step, button) {
    var timer = {
      id: nextId++,
      seconds: seconds,
      label: label,
      step: step,
      endsAt: Date.now() + seconds * 1000,
      remaining: seconds * 1000,
      state: "running",
      button: button || null
    };
    timers.push(timer);
    addRow(timer);
    showBar();
    ensureTicker();
    if (button) button.setAttribute("data-running", "true");
    announce("Timer started: " + label + " for step " + step);
    save();
    return timer;
  }

  /* ------------------------------------------------------------------ mode */

  function setBoxesEnabled(enabled) {
    boxes.forEach(function (box) {
      box.disabled = !enabled;
    });
  }

  function enter(restoring) {
    cooking = true;
    document.body.classList.add("cooking");
    setBoxesEnabled(true);
    timerGroups.forEach(function (group) {
      group.hidden = false;
    });
    if (scaleGroup) scaleGroup.hidden = false;
    toggle.textContent = "Stop cooking";
    toggle.setAttribute("aria-pressed", "true");

    showBar();
    acquireWake();

    if (!restoring) {
      unlockAudio();
      announce("Cooking mode on. Ingredients can be checked off, and steps with times have timers.");
    }
    save();
  }

  function exit() {
    cooking = false;
    document.body.classList.remove("cooking");
    setBoxesEnabled(false);
    boxes.forEach(function (box) {
      box.checked = false;
    });
    timerGroups.forEach(function (group) {
      group.hidden = true;
    });
    /* The multiplier belongs to the session, like the checkboxes: leaving the
       mode puts the written amounts back. */
    setScale(1, true);
    if (scaleGroup) scaleGroup.hidden = true;
    toggle.textContent = "Start cooking";
    toggle.setAttribute("aria-pressed", "false");

    timers.slice().forEach(removeTimer);
    releaseWake();
    hideBarIfEmpty();
    announce("Cooking mode off.");
    save();
  }

  /* ------------------------------------------------------------------ init */

  toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "cook-toggle";
  toggle.textContent = "Start cooking";
  toggle.setAttribute("aria-pressed", "false");
  toggle.addEventListener("click", function () {
    if (cooking) exit();
    else enter(false);
  });

  scaleGroup = buildScale();

  var actions = document.createElement("div");
  actions.className = "cook-actions";
  actions.appendChild(toggle);

  var meta = head.querySelector(".recipe-meta");
  if (meta && meta.nextSibling) head.insertBefore(actions, meta.nextSibling);
  else head.appendChild(actions);

  timerGroups.forEach(function (group) {
    slice(group.querySelectorAll(".step-timer")).forEach(function (button) {
      var seconds = parseInt(button.getAttribute("data-seconds"), 10);
      if (!seconds) return;
      var step = stepNumber(button);
      button.setAttribute("aria-label", "Start a " + button.textContent + " timer for step " + step);

      button.addEventListener("click", function () {
        askNotify();
        unlockAudio();

        var running = null;
        timers.forEach(function (timer) {
          if (timer.button === button && timer.state !== "done") running = timer;
        });
        if (running) {
          removeTimer(running);
          return;
        }
        startTimer(seconds, button.textContent, step, button);
      });
    });
  });

  boxes.forEach(function (box) {
    box.addEventListener("change", save);
  });

  /* The other half of the sync: notificationclick and notificationclose only
     reach the service worker, which relays them here. The url check matters
     because timer ids are per-page counters, so two recipes open at once would
     otherwise dismiss each other's timers. */
  if (navigator.serviceWorker) {
    navigator.serviceWorker.addEventListener("message", function (event) {
      var data = event.data || {};
      if (data.url && data.url !== window.location.href) return;

      var match = null;
      timers.forEach(function (timer) {
        if (timer.id === data.id) match = timer;
      });
      if (!match) return;

      if (data.type === "timer-dismissed") {
        removeTimer(match);
      } else if (data.type === "timer-acknowledged") {
        /* Tapping the notification means "I heard it" — stop the noise, but
           leave the row, since the tap is bringing the cook back to the page. */
        stopAlarm(match);
      }
    });
    /* Messages queue until this is called when using addEventListener */
    if (navigator.serviceWorker.startMessages) navigator.serviceWorker.startMessages();
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState !== "visible") return;
    /* A throttled or suspended background tab leaves the display stale and can
       sail past an expiry. Recompute the moment the cook looks at the phone. */
    tick();
    if (cooking) acquireWake();
  });

  window.addEventListener("pagehide", save);

  /* Restore a session: the mode, what was checked, and any timer still owed
     time. Timers are stored as absolute end times, so a reload picks them up
     mid-flight rather than restarting them. */
  (function restore() {
    var data = load();
    if (!data) return;

    if (data.mode) enter(true);

    if (data.count === boxes.length && data.checked) {
      data.checked.forEach(function (index) {
        if (boxes[index]) boxes[index].checked = true;
      });
    }

    if (data.mode && data.scale) setScale(data.scale, true);

    if (!data.timers || !data.timers.length) return;

    data.timers.forEach(function (stored) {
      var timer = {
        id: nextId++,
        seconds: stored.seconds,
        label: stored.label,
        step: stored.step,
        endsAt: stored.endsAt,
        remaining: stored.remaining,
        state: stored.state,
        button: null
      };

      /* Anything that ran out while the page was closed comes back already
         finished rather than firing a burst of alarms on load. */
      if (timer.state === "running" && (!timer.endsAt || Date.now() >= timer.endsAt)) {
        timer.state = "done";
        timer.endsAt = null;
      }

      timers.push(timer);
      addRow(timer);
    });

    if (timers.length) {
      showBar();
      ensureTicker();
      retitle();
    }
  })();
})();

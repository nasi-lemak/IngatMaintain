// Progressive enhancement: every form also works without JavaScript.
(function () {
  "use strict";

  // Confirm destructive actions.
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  }, true);

  // Tick boxes save instantly without reloading the page.
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (!form.hasAttribute("data-toggle")) return;
    e.preventDefault();
    var button = form.querySelector("button.check");
    if (!button || button.classList.contains("busy")) return;
    button.classList.add("busy");

    fetch(form.action, {
      method: "POST",
      body: new FormData(form),
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (res) {
        return res.json().then(function (data) {
          if (!res.ok) throw new Error(data.error || "Could not save");
          return data;
        });
      })
      .then(function (data) {
        button.classList.toggle("checked", data.checked);
        button.setAttribute("aria-pressed", data.checked ? "true" : "false");
        form.querySelector(".who").textContent = data.checked ? data.by + " · " + data.at : "";
        var step = form.closest(".step");
        if (step) {
          var all = step.querySelectorAll("button.check");
          var done = step.querySelectorAll("button.check.checked");
          step.classList.toggle("step-done", all.length === done.length);
        }
        document.querySelectorAll("[data-progress-text]").forEach(function (el) {
          el.textContent = data.done + "/" + data.total;
        });
        var pct = data.total ? Math.round((100 * data.done) / data.total) : 0;
        document.querySelectorAll("[data-job-progress] .progress-bar").forEach(function (el) {
          el.style.width = pct + "%";
        });
      })
      .catch(function (err) {
        window.alert(err.message + " – check your connection and try again.");
      })
      .finally(function () {
        button.classList.remove("busy");
      });
  });

  // Checklist editor: add / remove / reorder rows.
  document.addEventListener("click", function (e) {
    var add = e.target.closest("[data-add]");
    if (add) {
      var name = add.getAttribute("data-add");
      var tpl = document.querySelector('template[data-template="' + name + '"]');
      var list = document.querySelector('[data-list="' + name + '"]');
      var row = tpl.content.firstElementChild.cloneNode(true);
      list.appendChild(row);
      var first = row.querySelector("input[name$=_title], input[name$=_name]");
      if (first) first.focus();
      return;
    }
    var row = e.target.closest("[data-row]");
    if (!row) return;
    if (e.target.closest("[data-remove]")) {
      row.remove();
    } else if (e.target.closest("[data-move]")) {
      var dir = parseInt(e.target.closest("[data-move]").getAttribute("data-move"), 10);
      if (dir < 0 && row.previousElementSibling) row.parentNode.insertBefore(row, row.previousElementSibling);
      if (dir > 0 && row.nextElementSibling) row.parentNode.insertBefore(row.nextElementSibling, row);
    }
  });
})();

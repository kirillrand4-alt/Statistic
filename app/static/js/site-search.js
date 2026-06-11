// Turn a long <select name="site_id"> (or <select class="searchable">) into a
// type-to-filter combobox, so you can find a domain by typing. The native
// <select> stays in the DOM (hidden) and still submits its value, and a
// `change` event is dispatched on pick — so existing onchange="this.form.submit()"
// keeps working. Without JS the plain <select> works as before.
(function () {
  function labelOf(o) { return (o.textContent || "").trim(); }

  function enhance(select) {
    if (select.dataset.searchEnhanced) return;
    if (select.options.length < 6) return; // a short list doesn't need search
    try {
      select.dataset.searchEnhanced = "1";

      var wrap = document.createElement("div");
      wrap.className = "site-search";
      select.parentNode.insertBefore(wrap, select);
      wrap.appendChild(select);
      select.style.display = "none";

      var input = document.createElement("input");
      input.type = "text";
      input.className = "site-search-input";
      input.autocomplete = "off";
      input.placeholder = "поиск домена…";
      wrap.appendChild(input);

      var list = document.createElement("ul");
      list.className = "site-search-list";
      list.hidden = true;
      wrap.appendChild(list);

      var active = -1;

      function currentLabel() {
        var o = select.options[select.selectedIndex];
        return o ? labelOf(o) : "";
      }
      input.value = currentLabel();

      function render(filter) {
        list.innerHTML = "";
        var q = (filter || "").toLowerCase();
        var shown = 0;
        Array.prototype.forEach.call(select.options, function (o) {
          var text = labelOf(o);
          if (q && text.toLowerCase().indexOf(q) === -1) return;
          var li = document.createElement("li");
          li.textContent = text;
          li.dataset.value = o.value;
          if (o.value === select.value) li.className = "sel";
          li.addEventListener("mousedown", function (e) {
            e.preventDefault(); // keep focus; fire before blur
            choose(o.value, text);
          });
          list.appendChild(li);
          shown++;
        });
        list.hidden = shown === 0;
        active = -1;
      }

      function open() { render(""); try { input.select(); } catch (e) {} }
      function close() { list.hidden = true; }

      function choose(value, text) {
        select.value = value;
        input.value = text;
        close();
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }

      input.addEventListener("focus", open);
      input.addEventListener("input", function () { render(input.value); });
      input.addEventListener("keydown", function (e) {
        var items = Array.prototype.slice.call(list.querySelectorAll("li"));
        if (e.key === "ArrowDown") { e.preventDefault(); active = Math.min(active + 1, items.length - 1); }
        else if (e.key === "ArrowUp") { e.preventDefault(); active = Math.max(active - 1, 0); }
        else if (e.key === "Enter") {
          if (!list.hidden && items[active]) { e.preventDefault(); choose(items[active].dataset.value, items[active].textContent); }
          return;
        } else if (e.key === "Escape") { close(); input.value = currentLabel(); input.blur(); return; }
        else return;
        items.forEach(function (li, i) { li.classList.toggle("active", i === active); });
        if (items[active]) items[active].scrollIntoView({ block: "nearest" });
      });
      input.addEventListener("blur", function () {
        // if the user typed but didn't pick, restore a valid selection
        setTimeout(function () { close(); input.value = currentLabel(); }, 150);
      });
    } catch (err) {
      select.style.display = ""; // fall back to the native select on any error
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll('select[name="site_id"], select.searchable').forEach(enhance);
  });
})();

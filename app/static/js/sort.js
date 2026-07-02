// Click a column header to sort the table. Add class="sortable" to a <table>.
// For multi-row headers (colspan) mark sortable <th> with data-col="N".
// Tables with > LIMIT rows get a checkbox to show only the first LIMIT rows
// (after sorting); hidden rows stay in the DOM and keep participating in sorts.
(function () {
  const LIMIT = 1000;

  function val(td) {
    const t = (td ? td.textContent : "").trim();
    if (!t || t === "—" || t === "-") return null;
    if (t === "∞") return Infinity;
    const n = parseFloat(t.replace(/\s/g, "").replace(/%/g, "").replace(",", "."));
    return isNaN(n) ? t.toLowerCase() : n;
  }
  function cmp(a, b, dir) {
    if (a === null && b === null) return 0;
    if (a === null) return 1;
    if (b === null) return -1;
    let r;
    if (typeof a === "number" && typeof b === "number") r = a - b;
    else r = String(a).localeCompare(String(b));
    return dir === "asc" ? r : -r;
  }
  function applyLimit(table) {
    const tb = table.tBodies[0];
    if (!tb) return;
    let shown = 0;
    for (const row of tb.rows) {
      if (table._limitOn && shown >= LIMIT) {
        row.style.display = "none";
      } else {
        row.style.display = "";
        shown++;
      }
    }
  }
  function sortBy(table, col, dir) {
    const tb = table.tBodies[0];
    if (!tb) return;
    const rows = Array.from(tb.rows).filter((r) => r.cells.length > col);
    rows.sort((x, y) => cmp(val(x.cells[col]), val(y.cells[col]), dir));
    rows.forEach((r) => tb.appendChild(r));
    applyLimit(table);
  }

  function enhanceTable(table) {
    if (table.dataset.sortInit) return;   // idempotent: safe to re-run after async inject
    table.dataset.sortInit = "1";
    // ----- row-display limit -----
    const tb = table.tBodies[0];
    const total = tb ? tb.rows.length : 0;
    table._limitOn = false;
    if (total > LIMIT) {
      table._limitOn = true;
      const bar = document.createElement("p");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = true;
      const lbl = document.createElement("label");
      lbl.appendChild(cb);
      lbl.appendChild(document.createTextNode(` Показывать первые ${LIMIT} из ${total} строк (сортировка/статистика — по всем)`));
      bar.appendChild(lbl);
      const anchor = table.closest("figure") || table;
      anchor.parentNode.insertBefore(bar, anchor);
      cb.addEventListener("change", function () {
        table._limitOn = cb.checked;
        applyLimit(table);
      });
      applyLimit(table);
    }

    // ----- sortable headers -----
    const explicit = table.querySelectorAll("th[data-col]");
    let headers, getCol;
    if (explicit.length) {
      headers = explicit;
      getCol = (th) => parseInt(th.getAttribute("data-col"), 10);
    } else {
      const trs = table.tHead ? table.tHead.rows : [];
      headers = trs.length ? Array.from(trs[trs.length - 1].cells) : [];
      getCol = (th) => th.cellIndex;
    }
    headers.forEach(function (th) {
      th.style.cursor = "pointer";
      if (!th.title) th.title = "Сортировать";
      th.addEventListener("click", function () {
        const col = getCol(th);
        if (isNaN(col)) return;
        const dir = th.getAttribute("data-dir") === "asc" ? "desc" : "asc";
        headers.forEach((h) => {
          h.removeAttribute("data-dir");
          h.textContent = h.textContent.replace(/[ ▲▼]+$/, "");
        });
        th.setAttribute("data-dir", dir);
        th.textContent = th.textContent.replace(/[ ▲▼]+$/, "") + (dir === "asc" ? " ▲" : " ▼");
        sortBy(table, col, dir);
      });
    });
  }

  // Scan a root (default: whole document) for sortable tables. Exposed so
  // async-injected fragments (progressive pages) can re-arm their tables.
  function initSortable(root) {
    (root || document).querySelectorAll("table.sortable").forEach(enhanceTable);
  }
  window.initSortable = initSortable;

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () { initSortable(document); });
  } else {
    initSortable(document);
  }
})();

// Click a column header to sort the table. Add class="sortable" to a <table>.
// For tables with a multi-row header (colspan), mark sortable <th> with data-col="N".
(function () {
  function val(td) {
    const t = (td ? td.textContent : "").trim();
    if (!t || t === "—" || t === "-") return null;        // empty -> sort last
    if (t === "∞") return Infinity;
    const n = parseFloat(t.replace(/\s/g, "").replace(/%/g, "").replace(",", "."));
    return isNaN(n) ? t.toLowerCase() : n;
  }
  function cmp(a, b, dir) {
    if (a === null && b === null) return 0;
    if (a === null) return 1;                              // nulls always last
    if (b === null) return -1;
    let r;
    if (typeof a === "number" && typeof b === "number") r = a - b;
    else r = String(a).localeCompare(String(b));
    return dir === "asc" ? r : -r;
  }
  function sortBy(table, col, dir) {
    const tb = table.tBodies[0];
    if (!tb) return;
    const rows = Array.from(tb.rows).filter((r) => r.cells.length > col);
    rows.sort((x, y) => cmp(val(x.cells[col]), val(y.cells[col]), dir));
    rows.forEach((r) => tb.appendChild(r));
  }
  document.querySelectorAll("table.sortable").forEach(function (table) {
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
  });
})();

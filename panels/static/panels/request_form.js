(function () {
  var cfg = JSON.parse(document.getElementById("ops-meta").textContent);
  var box = document.getElementById("lines");
  var total = document.getElementById("id_form-TOTAL_FORMS");
  var tpl = document.getElementById("line-template").innerHTML;
  var ENTER_PARENTS = ["purchase", "adjust_in", "dc_return", "repair_return"];  // parts arrive WITH the server
  var CHILD_OPS = ["install", "enter_installed"];

  function f(line, name) { return line.querySelector("[name$='-" + name + "']"); }
  function trimmed(x) { return x.trim(); }
  function serialsOf(line) { return f(line, "serials").value.split(/[\n,،]+/).map(trimmed).filter(Boolean); }
  function meta(line) { return cfg.items[f(line, "item_type").value] || {}; }
  function isServer(line) { return !!meta(line).server; }
  function isChild(line) { return line.classList.contains("child"); }
  function lines() { return Array.prototype.slice.call(box.querySelectorAll(".line")); }
  function childrenOf(parent) {
    var out = [], n = parent.nextElementSibling;
    while (n && isChild(n)) { out.push(n); n = n.nextElementSibling; }
    return out;
  }
  function parentOf(child) {
    var p = child.previousElementSibling;
    while (p && isChild(p)) p = p.previousElementSibling;
    return p;
  }

  function show(line, sel, on) {
    line.querySelectorAll(sel).forEach(function (el) {
      el.classList.toggle("hide", !on);
      if (!on) el.querySelectorAll("select,input,textarea").forEach(function (x) { x.value = ""; });
    });
  }

  function refresh(line) {
    var opSel = f(line, "operation"), op = opSel.value, m = cfg.ops[op] || {};
    var mt = meta(line), child = isChild(line), server = isServer(line);
    line.classList.toggle("is-server", server && !child);

    // child lines may only install from stock or enter new parts; they never pick another server
    opSel.querySelectorAll("option").forEach(function (o) {
      var ok = !child || !o.value || CHILD_OPS.indexOf(o.value) >= 0;
      o.hidden = !ok; o.disabled = !ok;
    });
    f(line, "item_type").querySelectorAll("option").forEach(function (o) {
      var bad = child && cfg.servers.indexOf(Number(o.value)) >= 0;
      o.hidden = bad; o.disabled = bad;
    });

    show(line, ".f-from_location", m.from);
    show(line, ".f-to_location", m.to);
    show(line, ".f-unit_price", m.price);
    var ssf = line.querySelector(".f-server_serial"), ss = f(line, "server_serial");
    ssf.querySelectorAll(".hint:not(.serial-hint)").forEach(function (h) { h.classList.toggle("hide", child); });
    if (child) { ssf.classList.remove("hide"); ss.readOnly = true; }
    else { ss.readOnly = false; show(line, ".f-server_serial", m.server); }

    // serial field depends on how the item is tracked
    var sf = line.querySelector(".f-serials"), hint = sf.querySelector(".serial-hint");
    var allow = !(mt.mode === "quantity" && m.enter);
    sf.classList.toggle("hide", !allow);
    if (!allow) f(line, "serials").value = "";
    var h = "";
    if (mt.mode === "serial") h = "الزامی: دقیقاً به تعداد قلم، هر سطر یک سریال.";
    else if (mt.mode === "both") h = "اختیاری؛ اگر وارد کنی باید به تعداد قلم باشد.";
    else if (mt.mode === "quantity") h = "اختیاری: برای انتخاب قطعهٔ مشخص، کد شناسایی‌اش را بنویس؛ وگرنه خودکار برداشته می‌شود.";
    hint.textContent = h; hint.classList.toggle("hide", !h || !allow);

    // header
    var label = opSel.options[opSel.selectedIndex];
    line.querySelector(".kind-op").textContent = label && label.value ? label.textContent : "قلم جدید";
    var sub = "";
    if (child) sub = "← سخت‌افزار سرور " + (ss.value || "؟");
    else if (server) sub = "(سرور کامل)";
    else if (mt.mode === "quantity" && m.enter) sub = "(برای هر عدد، کد شناسایی خودکار ساخته می‌شود)";
    line.querySelector(".kind-sub").textContent = sub;

    // "add hardware" is offered for every complete-server operation (except scrap)
    var can = server && !child && op && op !== "scrap";
    line.querySelector(".add-parts").classList.toggle("hide", !can);

    // a line that stopped being a server cannot keep children
    if (!server && !child) childrenOf(line).forEach(function (c) { c.classList.remove("child"); refresh(c); });
  }

  function syncChildren(parent) {
    var s = serialsOf(parent)[0] || "";
    childrenOf(parent).forEach(function (c) { f(c, "server_serial").value = s; refresh(c); });
  }
  function autoQty(line) {
    var n = serialsOf(line).length;
    if (n) f(line, "quantity").value = n;
  }
  function renumber() {
    var ls = lines();
    ls.forEach(function (l, i) {
      l.querySelectorAll("[name],[id],label[for]").forEach(function (el) {
        ["name", "id", "for"].forEach(function (a) {
          var v = el.getAttribute(a);
          if (v) el.setAttribute(a, v.replace(/form-\d+-/, "form-" + i + "-"));
        });
      });
      l.querySelector(".ln").textContent = i + 1;
    });
    total.value = ls.length;
  }
  function makeLine() {
    var div = document.createElement("div");
    div.innerHTML = tpl.replace(/__prefix__/g, total.value);
    var line = div.firstElementChild;
    wire(line);
    return line;
  }
  function addLine() {
    var line = makeLine();
    box.appendChild(line); renumber(); refresh(line);
    return line;
  }
  function addPart(parent) {
    var s = serialsOf(parent);
    if (s.length !== 1) {
      alert("برای افزودن سخت‌افزار، در قلم سرور دقیقاً یک سریال وارد کن (برای هر سرور یک قلم جدا).");
      f(parent, "serials").focus();
      return;
    }
    var line = makeLine();
    line.classList.add("child");
    var anchor = parent, kids = childrenOf(parent);
    if (kids.length) anchor = kids[kids.length - 1];
    anchor.after(line);
    renumber();
    f(line, "server_serial").value = s[0];
    var op = f(parent, "operation").value;
    f(line, "operation").value = ENTER_PARENTS.indexOf(op) >= 0 ? "enter_installed" : "install";
    refresh(line);
    line.scrollIntoView({ behavior: "smooth", block: "center" });
    f(line, "item_type").focus();
  }
  function wire(line) {
    f(line, "operation").addEventListener("change", function () { refresh(line); });
    f(line, "item_type").addEventListener("change", function () { refresh(line); if (!isChild(line)) syncChildren(line); });
    f(line, "serials").addEventListener("input", function () {
      autoQty(line);
      if (isServer(line) && !isChild(line)) syncChildren(line);
    });
    f(line, "server_serial").addEventListener("input", function () { refresh(line); });
    line.querySelector(".rm").addEventListener("click", function () {
      var kids = isChild(line) ? [] : childrenOf(line);
      if (kids.length && !confirm("سخت‌افزارهای همین سرور هم حذف شوند؟")) return;
      if (lines().length - kids.length <= 1 && !isChild(line)) { return; }
      kids.forEach(function (k) { k.remove(); });
      line.remove(); renumber();
    });
    line.querySelector(".add-parts").addEventListener("click", function () { addPart(line); });
  }

  document.getElementById("add-line").addEventListener("click", addLine);

  // initial lines (also after a validation error): re-detect which lines are hardware of a server
  var last = null;
  lines().forEach(function (l) {
    wire(l);
    var op = f(l, "operation").value, ss = f(l, "server_serial").value.trim();
    if (last && CHILD_OPS.indexOf(op) >= 0 && !isServer(l) && ss && serialsOf(last)[0] === ss) l.classList.add("child");
    if (isServer(l) && !isChild(l)) last = l;
  });
  renumber();
  lines().forEach(refresh);

  var form = document.getElementById("req-form"), sent = false;
  form.addEventListener("submit", function (e) {
    if (sent) { e.preventDefault(); return; }
    lines().forEach(function (l) { if (!isChild(l) && isServer(l)) syncChildren(l); });
    sent = true;
    var b = form.querySelector("button[type=submit]");
    b.disabled = true; b.textContent = "در حال ثبت...";
  });
})();

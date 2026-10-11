(function () {
  var cfg = JSON.parse(document.getElementById("ops-meta").textContent);
  var box = document.getElementById("lines");
  var total = document.getElementById("id_form-TOTAL_FORMS");
  var tpl = document.getElementById("line-template").innerHTML;

  function show(line, sel, on) {
    line.querySelectorAll(sel).forEach(function (f) {
      f.classList.toggle("hide", !on);
      if (!on) f.querySelectorAll("select,input,textarea").forEach(function (x) { x.value = ""; });
    });
  }
  function refresh(line) {
    var op = line.querySelector("select[name$='-operation']").value;
    var m = cfg.ops[op] || {};
    show(line, ".f-from_location", m.from);
    show(line, ".f-to_location", m.to);
    show(line, ".f-server, .f-server_serial", m.server);
    show(line, ".f-unit_price", m.price);
    var item = Number(line.querySelector("select[name$='-item_type']").value);
    var isServerEntry = cfg.servers.indexOf(item) >= 0 && (op === "purchase" || op === "adjust_in");
    line.querySelector(".add-parts").classList.toggle("hide", !isServerEntry);
  }
  function autoQty(line) {
    var ser = line.querySelector("textarea[name$='-serials']").value
      .split(/[\n,،]+/).filter(function (x) { return x.trim(); });
    if (ser.length) line.querySelector("input[name$='-quantity']").value = ser.length;
  }
  function renumber() {
    var lines = box.querySelectorAll(".line");
    lines.forEach(function (l, i) {
      l.querySelectorAll("[name],[id],label[for]").forEach(function (el) {
        ["name", "id", "for"].forEach(function (a) {
          var v = el.getAttribute(a);
          if (v) el.setAttribute(a, v.replace(/form-\d+-/, "form-" + i + "-"));
        });
      });
      l.querySelector(".ln").textContent = i + 1;
    });
    total.value = lines.length;
  }
  function addLine() {
    var div = document.createElement("div");
    div.innerHTML = tpl.replace(/__prefix__/g, total.value);
    var line = div.firstElementChild;
    box.appendChild(line);
    wire(line); renumber();
    return line;
  }
  function wire(line) {
    line.querySelector("select[name$='-operation']").addEventListener("change", function () { refresh(line); });
    line.querySelector("select[name$='-item_type']").addEventListener("change", function () { refresh(line); });
    line.querySelector("textarea[name$='-serials']").addEventListener("input", function () { autoQty(line); });
    line.querySelector(".rm").addEventListener("click", function () {
      if (box.querySelectorAll(".line").length > 1) { line.remove(); renumber(); }
    });
    line.querySelector(".add-parts").addEventListener("click", function () {
      var first = line.querySelector("textarea[name$='-serials']").value.split(/[\n,،]+/)
        .map(function (x) { return x.trim(); }).filter(Boolean)[0] || "";
      var part = addLine();
      var op = part.querySelector("select[name$='-operation']");
      op.value = "enter_installed";
      refresh(part);
      part.querySelector("input[name$='-server_serial']").value = first;
      part.scrollIntoView({behavior: "smooth", block: "center"});
      if (!first) alert("اول سریال سرور را در قلم سرور وارد کن، بعد همان را در فیلد «سریال سرور» این قطعه بنویس.");
    });
    refresh(line);
  }
  document.getElementById("add-line").addEventListener("click", addLine);
  box.querySelectorAll(".line").forEach(wire);
  renumber();
  var f = document.getElementById("req-form"), sent = false;
  f.addEventListener("submit", function (e) {
    if (sent) { e.preventDefault(); return; }
    sent = true; var b = f.querySelector("button[type=submit]");
    b.disabled = true; b.textContent = "در حال ثبت...";
  });
})();

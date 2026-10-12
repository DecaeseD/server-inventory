"""Hardware summary of a server: category (RAM / CPU / disk ...) -> sub-type (e.g. 2 x 32GB) -> totals."""
from decimal import Decimal

from catalog.models import Category, ItemType

from .models import Unit, UnitStatus
from .services import components_of

TO_GB = {"MB": Decimal("0.001"), "GB": Decimal(1), "TB": Decimal(1000)}


def _trim(d):
    d = Decimal(d).quantize(Decimal("0.01"))
    s = format(d, "f").rstrip("0").rstrip(".")
    return s or "0"


def fmt_capacity(value, unit):
    """12.5 GB -> '12.5GB'; core -> '8 هسته'."""
    if unit == "core":
        return f"{_trim(value)} هسته"
    return f"{_trim(value)}{unit}"


def fmt_storage_gb(gb):
    gb = Decimal(gb)
    return f"{_trim(gb / 1000)}TB" if gb >= 1000 else f"{_trim(gb)}GB"


def root_of(cat, cats):
    seen = set()
    while cat.parent_id and cat.pk not in seen:
        seen.add(cat.pk)
        cat = cats[cat.parent_id]
    return cat


def server_specs(server):
    """[{name, count, totals:[str], rows:[{label, count, subtotal, items:[ItemType], units:[Unit]}]}]"""
    comps = dict(components_of(server))                       # {item_type_id: qty} (ledger = truth)
    if not comps:
        return []
    items = {i.pk: i for i in ItemType.objects.filter(pk__in=comps).select_related("category")}
    cats = {c.pk: c for c in Category.objects.all()}
    parts = {}
    for u in Unit.objects.filter(installed_in=server, status=UnitStatus.INSTALLED).select_related("item_type").order_by("serial_number", "pk"):
        parts.setdefault(u.item_type_id, []).append(u)

    groups = {}   # root category pk -> {"cat", "rows": {key: row}}
    for item_id, qty in comps.items():
        it = items[item_id]
        root = root_of(cats[it.category_id], cats)
        g = groups.setdefault(root.pk, {"cat": root, "rows": {}})
        if it.capacity and it.capacity_unit:
            key = ("cap", it.capacity_unit, it.capacity)
            label = f"{root.name} {fmt_capacity(it.capacity, it.capacity_unit)}"
        else:
            key, label = ("item", it.pk), it.name
        row = g["rows"].setdefault(key, {"label": label, "count": 0, "items": [], "units": [],
                                         "unit_cap": it.capacity, "unit_unit": it.capacity_unit})
        row["count"] += qty
        row["items"].append(it)
        row["units"].extend(parts.get(item_id, []))

    out = []
    for g in sorted(groups.values(), key=lambda g: g["cat"].name):
        storage_gb, cores, uncounted = Decimal(0), Decimal(0), 0
        rows = sorted(g["rows"].values(), key=lambda r: (r["unit_cap"] is None, -(r["unit_cap"] or 0), r["label"]))
        for r in rows:
            r["subtotal"] = None
            if r["unit_cap"] and r["unit_unit"]:
                if r["unit_unit"] == "core":
                    cores += r["unit_cap"] * r["count"]
                    r["subtotal"] = fmt_capacity(r["unit_cap"] * r["count"], "core")
                else:
                    gb = r["unit_cap"] * TO_GB[r["unit_unit"]] * r["count"]
                    storage_gb += gb
                    r["subtotal"] = fmt_storage_gb(gb)
            else:
                uncounted += r["count"]
        totals = []
        if storage_gb:
            totals.append(fmt_storage_gb(storage_gb))
        if cores:
            totals.append(fmt_capacity(cores, "core"))
        out.append({"name": g["cat"].name, "count": sum(r["count"] for r in rows), "totals": totals,
                    "uncounted": uncounted, "rows": rows})
    return out


def server_summary_line(server):
    """Short text for lists: 'RAM 64GB · Disk 1.2TB'."""
    parts = []
    for g in server_specs(server):
        parts.append(f"{g['name']} {' + '.join(g['totals'])}" if g["totals"] else f"{g['name']} ×{g['count']}")
    return " · ".join(parts)

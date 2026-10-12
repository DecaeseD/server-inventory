"""Business-logic layer. ALL stock-affecting writes go through this module.

Model: every approved line is a *move* between two "buckets" (free, installed, in_repair, ...)
or between a bucket and the outside world (None). A move writes immutable LedgerEntry rows;
stock is always SUM(delta) over the ledger. Pending documents never touch the ledger.
"""
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from audit.models import record
from catalog.models import ItemType

from .models import (OPERATION_MOVES, PRICED_OPERATIONS, SERVER_OPERATIONS, Bucket, ChangeRequest,
                     InventorySettings, LedgerEntry, Operation, StockDocument, StockLine, Unit,
                     UnitStatus)

ANY = object()  # wildcard for balance filters (None means "exactly NULL")


class InventoryError(Exception):
    """A business rule was violated. Message is safe to show to users."""


# ---------------------------------------------------------------- permissions
def _require_perm(user, perm):
    if user is None or not user.is_active or not user.has_perm(perm):
        raise PermissionDenied("شما مجوز انجام این عملیات را ندارید.")


def may_self_approve(user, cfg=None):
    """Superusers and the top role («مدیر کل») may approve what they registered themselves.
    For everybody else the global setting decides."""
    cfg = cfg or InventorySettings.get()
    return bool(cfg.allow_self_approval or user.is_superuser
                or user.groups.filter(name="مدیر کل").exists())


# ---------------------------------------------------------------- balances
def get_balance(item_type, bucket, location=ANY, server=ANY):
    """Single source of truth for stock. `None` = exactly NULL, ANY = don't filter."""
    qs = LedgerEntry.objects.filter(item_type=item_type, bucket=bucket)
    if location is not ANY:
        qs = qs.filter(location=location)
    if server is not ANY:
        qs = qs.filter(server=server)
    return qs.aggregate(t=Sum("delta"))["t"] or 0


def stock_summary(item_type):
    """{bucket: quantity} across all locations/servers."""
    rows = LedgerEntry.objects.filter(item_type=item_type).values("bucket").annotate(t=Sum("delta"))
    result = {b.value: 0 for b in Bucket}
    for r in rows:
        result[r["bucket"]] = r["t"] or 0
    return result


def components_of(server):
    """[(item_type_id, quantity)] currently installed on a server unit."""
    rows = (LedgerEntry.objects.filter(bucket=Bucket.INSTALLED, server=server)
            .values("item_type").annotate(t=Sum("delta")).filter(t__gt=0))
    return [(r["item_type"], r["t"]) for r in rows]


def stock_table(queryset=None):
    """ItemType queryset annotated with qty_<bucket> (same SUM(delta) logic as get_balance)."""
    qs = queryset if queryset is not None else ItemType.objects.all()
    return qs.annotate(**{
        f"qty_{b.value}": Coalesce(Sum("ledger_entries__delta",
                                       filter=Q(ledger_entries__bucket=b.value)), 0)
        for b in Bucket})


def preview_line(line):
    """Impact of a pending line on its source bucket (approximate: ignores earlier lines
    of the same document; the real check happens atomically at approval)."""
    if line.from_bucket is None:
        return None
    installed = line.from_bucket == Bucket.INSTALLED
    server = None
    if installed:
        try:
            server = _resolve_server(line, lock=False)
        except InventoryError:
            return {"current": 0, "after": -line.quantity, "ok": False}
    cur = get_balance(line.item_type, line.from_bucket,
                      location=None if installed else line.from_location, server=server)
    return {"current": cur, "after": cur - line.quantity, "ok": cur >= line.quantity}


def find_inconsistencies():
    """Cross-check unit states against the ledger. Returns a list of problem strings."""
    problems = []
    sums = {r["unit"]: r["s"] for r in
            LedgerEntry.objects.filter(unit__isnull=False).values("unit").annotate(s=Sum("delta"))}
    for u in Unit.objects.all():
        expected = 0 if u.status == UnitStatus.OUT else 1
        actual = sums.get(u.pk, 0)
        if expected != actual:
            problems.append(f"Unit {u.pk}: status={u.status} but ledger sum={actual}")
    return problems


# ---------------------------------------------------------------- validation
def _validate_line(d):
    op = d.get("operation")
    if op not in OPERATION_MOVES:
        raise InventoryError("نوع عملیات نامعتبر است.")
    fb, tb = OPERATION_MOVES[op]
    item = d["item_type"]
    if not item.is_active:
        raise InventoryError(f"کالای «{item.name}» غیرفعال است.")
    try:
        qty = int(d.get("quantity"))
    except (TypeError, ValueError):
        raise InventoryError("تعداد نامعتبر است.")
    if qty <= 0:
        raise InventoryError("تعداد باید بزرگتر از صفر باشد.")

    serials = [str(s).strip() for s in (d.get("serials") or [])]
    if any(not s for s in serials) or len(set(serials)) != len(serials):
        raise InventoryError("شماره سریال‌ها نباید خالی یا تکراری باشند.")
    mode = item.tracking_mode
    if mode == ItemType.Tracking.QUANTITY and serials and fb is None:
        raise InventoryError(
            f"«{item.name}» تعدادی است و هنگام ورود سریال نمی‌پذیرد؛ کد شناسایی داخلی خودکار ساخته می‌شود.")
    if mode == ItemType.Tracking.SERIAL and len(serials) != qty:
        raise InventoryError(f"برای «{item.name}» باید دقیقاً {qty} شماره سریال وارد شود.")
    if mode == ItemType.Tracking.BOTH and serials and len(serials) != qty:
        raise InventoryError("تعداد سریال‌ها با تعداد قلم برابر نیست.")
    if item.is_server and not serials:
        raise InventoryError("برای سرور وارد کردن شماره سریال الزامی است.")

    floc, tloc = d.get("from_location"), d.get("to_location")
    for bucket, loc in ((fb, floc), (tb, tloc)):
        if bucket and bucket != Bucket.INSTALLED and loc is None:
            raise InventoryError("محل نگهداری مبدأ/مقصد مشخص نشده است.")
    floc = floc if fb and fb != Bucket.INSTALLED else None
    tloc = tloc if tb and tb != Bucket.INSTALLED else None
    if op == Operation.TRANSFER and floc == tloc:
        raise InventoryError("مبدأ و مقصد انتقال یکسان است.")

    server = d.get("server")
    server_serial = (d.get("server_serial") or "").strip()
    if op in SERVER_OPERATIONS:
        if server is None and not server_serial:
            raise InventoryError("برای این عملیات باید سرور (یا سریال سرور) مشخص شود.")
        if server is not None:
            if not server.item_type.is_server:
                raise InventoryError("واحد انتخاب‌شده سرور نیست.")
            server_serial = ""
        if item.is_server:
            raise InventoryError("سرور را نمی‌توان روی سرور نصب کرد.")
    else:
        server, server_serial = None, ""

    price = d.get("unit_price") if op in PRICED_OPERATIONS else None  # price only where it makes sense
    if price is not None:
        try:
            price = Decimal(str(price))
        except InvalidOperation:
            raise InventoryError("قیمت نامعتبر است.")
        if price < 0:
            raise InventoryError("قیمت نمی‌تواند منفی باشد.")

    return dict(item_type=item, operation=op, quantity=qty, unit_price=price,
                from_bucket=fb, to_bucket=tb, from_location=floc, to_location=tloc,
                server=server, server_serial=server_serial, serials=serials,
                note=d.get("note", ""))


validate_line = _validate_line  # public alias (used by forms to report per-line errors)


# ---------------------------------------------------------------- creating requests
def create_document(*, user, lines, idempotency_key, occurred_at=None, note=""):
    """Register a PENDING request. Never changes stock. Safe against double submit."""
    _require_perm(user, "inventory.submit_document")
    if not idempotency_key:
        raise InventoryError("کلید یکتای درخواست الزامی است.")
    if not lines:
        raise InventoryError("سند باید حداقل یک قلم داشته باشد.")

    existing = StockDocument.objects.filter(idempotency_key=idempotency_key).first()
    if existing:
        if existing.created_by_id != user.pk:
            raise PermissionDenied("کلید درخواست متعلق به کاربر دیگری است.")
        return existing

    normalized = _order_for_servers([_validate_line(d) for d in lines])
    introduced = set()  # server serials entering the system earlier in this same document
    for n in normalized:
        if n["item_type"].is_server and n["from_bucket"] is None and n["to_bucket"]:
            introduced.update(n["serials"])
        ss = n["server_serial"]
        if ss and ss not in introduced and not Unit.objects.filter(
                item_type__is_server=True, serial_number=ss).exists():
            raise InventoryError(
                f"سرور با سریال {ss} وجود ندارد. قلم سرور باید پیش از قطعاتش در همین درخواست بیاید.")
    cfg = InventorySettings.get()
    try:
        with transaction.atomic():
            doc = StockDocument.objects.create(
                occurred_at=occurred_at or timezone.now(), created_by=user, note=note,
                idempotency_key=idempotency_key)
            created = [StockLine.objects.create(document=doc, **n) for n in normalized]
            _dry_run(doc, created, cfg)  # same rules as approval; any error aborts the whole request
            record(user=user, action="document_created", obj_type="StockDocument",
                   obj_id=doc.pk, document_id=doc.pk, after={"lines": len(normalized)})
    except IntegrityError:  # concurrent double submit with the same key
        existing = StockDocument.objects.filter(idempotency_key=idempotency_key).first()
        if existing is None:
            raise
        return existing
    return doc


def _order_for_servers(ns):
    """Parts that travel with a server must be processed in a sensible order:
    - server LEAVES the system (issue / adjust_out): its parts first, then the server;
    - server ENTERS the system (purchase / adjust_in): the server first, then its parts."""
    keys = [float(i) for i in range(len(ns))]
    leaving, entering = {}, {}
    for i, n in enumerate(ns):
        if n["item_type"].is_server:
            for s_ in n["serials"]:
                if n["from_bucket"] and n["to_bucket"] is None:
                    leaving[s_] = i
                elif n["from_bucket"] is None and n["to_bucket"]:
                    entering[s_] = i
    for i, n in enumerate(ns):
        if n["operation"] not in (Operation.INSTALL, Operation.ENTER_INSTALLED):
            continue
        ss = n["server_serial"] or (n["server"].serial_number if n["server"] else "")
        if ss in leaving and i > leaving[ss]:
            keys[i] = leaving[ss] - 0.5 + i / 10000.0
        elif ss in entering and i < entering[ss]:
            keys[i] = entering[ss] + 0.5 + i / 10000.0
    return [ns[i] for i in sorted(range(len(ns)), key=lambda i: keys[i])]


class _DryRunDone(Exception):
    pass


def _dry_run(doc, lines, cfg):
    """Execute the document exactly like approval would, then roll everything back.
    Catches unknown serials, wrong status/location, insufficient stock... at SUBMIT time."""
    try:
        with transaction.atomic():
            _execute(doc, lines, cfg, label_errors=True)
            raise _DryRunDone
    except _DryRunDone:
        pass


# ---------------------------------------------------------------- applying moves
def _matches(unit, bucket, location, server):
    if unit.status != bucket:
        return False
    if bucket == Bucket.INSTALLED:
        return unit.installed_in_id == (server.pk if server else None)
    return unit.location_id == (location.pk if location else None)


def _resolve_server(line, lock=True):
    qs = Unit.objects.select_for_update() if lock else Unit.objects
    if line.server_id:
        return qs.get(pk=line.server_id)
    if line.server_serial:
        found = list(qs.filter(item_type__is_server=True, serial_number=line.server_serial))
        if len(found) != 1:
            raise InventoryError(f"سرور با سریال {line.server_serial} یافت نشد یا یکتا نیست.")
        return found[0]
    return None


def _lookup_units(item, ids):
    """Units of `item` addressed by serial number OR internal asset code (row-locked)."""
    qs = (Unit.objects.select_for_update().filter(item_type=item)
          .filter(Q(serial_number__in=ids) | Q(asset_code__in=ids)).order_by("pk"))
    units = list(qs)
    found = {u.asset_code: u for u in units if u.asset_code}
    found.update({u.serial_number: u for u in units if u.serial_number})  # serial wins on a clash
    return found


def _new_units(item, serials=None, count=0):
    """Create OUT units, each with a unique internal asset code (<item pk>-<running number>).
    Callers hold the item-type lock, so the running number cannot race."""
    specs = list(serials) if serials is not None else [None] * count
    base = Unit.objects.filter(item_type=item).count()
    objs = [Unit(item_type=item, serial_number=sn, status=UnitStatus.OUT,
                 asset_code=f"{item.pk}-{base + i + 1:05d}") for i, sn in enumerate(specs)]
    Unit.objects.bulk_create(objs)
    by_code = {u.asset_code: u for u in Unit.objects.filter(asset_code__in=[o.asset_code for o in objs])}
    return [by_code[o.asset_code] for o in objs]


def _exit_components_with_server(server_unit, line, doc, cfg):
    """Issuing a complete server: every part installed on it leaves together with it.
    Explicit child lines are written so the ledger, history and void stay faithful."""
    comps = components_of(server_unit)
    if not comps:
        return
    ids = sorted(i for i, _ in comps)
    list(ItemType.objects.select_for_update().filter(pk__in=ids).order_by("pk"))
    for item_id, cqty in sorted(comps):
        cit = ItemType.objects.get(pk=item_id)
        idents = [u.ident for u in Unit.objects.filter(
            installed_in=server_unit, item_type_id=item_id, status=UnitStatus.INSTALLED).order_by("pk")]
        child = StockLine.objects.create(
            document=doc, item_type=cit, operation=Operation.EXIT_INSTALLED, quantity=cqty,
            from_bucket=Bucket.INSTALLED, to_bucket=None, server=server_unit,
            serials=idents[:cqty], parent_line=line,
            note=f"همراه سرور {server_unit.ident}")
        _apply_line(child, doc, cfg)


def _apply_line(line, doc, cfg):
    item, qty = line.item_type, line.quantity
    fb, tb = line.from_bucket, line.to_bucket
    floc, tloc = line.from_location, line.to_location
    server = _resolve_server(line)
    if server is None and Bucket.INSTALLED in (fb, tb):
        raise InventoryError("سرور مشخص نشده است.")
    if server is not None and tb == Bucket.INSTALLED and server.status in (
            UnitStatus.OUT, UnitStatus.SCRAPPED):
        raise InventoryError("روی سرور خارج‌شده یا اسقاطی نمی‌توان قطعه نصب کرد.")

    # --- source side: balance check first (clear message), then pick the exact units
    if fb is not None and not cfg.allow_negative_stock:
        bal = get_balance(item, fb,
                          location=None if fb == Bucket.INSTALLED else floc,
                          server=server if fb == Bucket.INSTALLED else None)
        if bal < qty:
            raise InventoryError(
                f"موجودی «{item.name}» کافی نیست (موجود: {bal}، درخواستی: {qty}).")

    units, rest = [], 0
    ids = [str(x) for x in (line.serials or [])]
    picked_ids = None
    if ids:
        found = _lookup_units(item, ids)
        if fb is None:
            missing = [x for x in ids if x not in found]
            created = {u.serial_number: u for u in _new_units(item, serials=missing)} if missing else {}
            for x in ids:
                u = found.get(x) or created[x]
                if u.status != UnitStatus.OUT:
                    raise InventoryError(f"سریال {x} از قبل در سیستم موجود است.")
                units.append(u)
        else:
            for x in ids:
                u = found.get(x)
                if u is None:
                    raise InventoryError(f"سریال/کد {x} برای «{item.name}» یافت نشد.")
                if not _matches(u, fb, floc, server):
                    raise InventoryError(f"سریال/کد {x} در وضعیت/محل مورد نیاز نیست.")
                units.append(u)
        rest = max(qty - len(units), 0)
    elif fb is None:
        # No serials given: every physical piece still gets its own identity (auto asset code).
        units = _new_units(item, count=qty)
        picked_ids = [u.ident for u in units]
    else:
        qs = Unit.objects.select_for_update().filter(item_type=item, status=fb)
        qs = qs.filter(installed_in=server) if fb == Bucket.INSTALLED else qs.filter(location=floc)
        units = list(qs.order_by("pk")[:qty])
        rest = qty - len(units)  # legacy stock recorded before per-piece identity existed
        picked_ids = [u.ident for u in units]
    if picked_ids:
        # Remember exactly which pieces moved (needed for faithful void). Same transaction.
        StockLine.objects.filter(pk=line.pk).update(serials=picked_ids)
        line.serials = picked_ids

    # --- a server leaving the system / being scrapped
    if item.is_server and (tb is None or tb == Bucket.SCRAPPED):
        for u in units:
            if not components_of(u):
                continue
            if line.operation in (Operation.ISSUE, Operation.ADJUST_OUT):
                _exit_components_with_server(u, line, doc, cfg)   # parts leave with the server
            else:
                raise InventoryError(f"ابتدا قطعات نصب‌شده روی سرور {u.ident} را جدا کنید.")

    # --- target side: unit states
    for u in units:
        if tb is None:
            u.status, u.location, u.installed_in = UnitStatus.OUT, None, None
        elif tb == Bucket.INSTALLED:
            u.status, u.location, u.installed_in = UnitStatus.INSTALLED, None, server
        else:
            u.status, u.location, u.installed_in = tb, tloc, None
        u.save(update_fields=["status", "location", "installed_in"])

    # --- ledger
    def entry(bucket, loc, unit, delta):
        installed = bucket == Bucket.INSTALLED
        return LedgerEntry(document=doc, line=line, item_type=item, bucket=bucket,
                           location=None if installed else loc,
                           server=server if installed else None, unit=unit, delta=delta,
                           occurred_at=doc.occurred_at)

    entries = []
    for u in units:
        if fb:
            entries.append(entry(fb, floc, u, -1))
        if tb:
            entries.append(entry(tb, tloc, u, +1))
    if rest > 0:
        if fb:
            entries.append(entry(fb, floc, None, -rest))
        if tb:
            entries.append(entry(tb, tloc, None, +rest))
    LedgerEntry.objects.bulk_create(entries)


def _lock_items(lines):
    # Fixed pk order => no deadlocks between concurrent approvals.
    ids = sorted({l.item_type_id for l in lines})
    list(ItemType.objects.select_for_update().filter(pk__in=ids).order_by("pk"))


def _execute(doc, lines, cfg, label_errors=False):
    _lock_items(lines)
    for i, line in enumerate(lines, 1):
        try:
            _apply_line(line, doc, cfg)
        except InventoryError as e:
            if label_errors:
                raise InventoryError(f"قلم {i} ({line.item_type.name}): {e}") from e
            raise


# ---------------------------------------------------------------- decisions
def approve_document(document_id, user, ip=None):
    _require_perm(user, "inventory.approve_document")
    with transaction.atomic():
        doc = StockDocument.objects.select_for_update().get(pk=document_id)
        if doc.status != StockDocument.Status.PENDING:
            raise InventoryError("فقط درخواست‌های در انتظار تأیید قابل تأیید هستند.")
        cfg = InventorySettings.get()
        if doc.created_by_id == user.pk and not may_self_approve(user, cfg):
            raise InventoryError("ثبت‌کننده نمی‌تواند درخواست خودش را تأیید کند.")
        lines = list(doc.lines.select_related("item_type", "from_location", "to_location")
                     .order_by("pk"))
        _execute(doc, lines, cfg)  # any error => whole transaction rolls back
        doc.status = StockDocument.Status.APPROVED
        doc.decided_by, doc.decided_at = user, timezone.now()
        doc.save(update_fields=["status", "decided_by", "decided_at"])
        record(user=user, action="document_approved", obj_type="StockDocument", obj_id=doc.pk,
               document_id=doc.pk, ip=ip)
    return doc


def reject_document(document_id, user, reason, ip=None):
    _require_perm(user, "inventory.approve_document")
    reason = (reason or "").strip()
    if not reason:
        raise InventoryError("ثبت دلیل رد الزامی است.")
    with transaction.atomic():
        doc = StockDocument.objects.select_for_update().get(pk=document_id)
        if doc.status != StockDocument.Status.PENDING:
            raise InventoryError("فقط درخواست‌های در انتظار تأیید قابل رد هستند.")
        doc.status = StockDocument.Status.REJECTED
        doc.decided_by, doc.decided_at, doc.reject_reason = user, timezone.now(), reason
        doc.save(update_fields=["status", "decided_by", "decided_at", "reject_reason"])
        record(user=user, action="document_rejected", obj_type="StockDocument", obj_id=doc.pk,
               document_id=doc.pk, reason=reason, ip=ip)
    return doc


def _line_idents(line):
    """Current identifiers of the pieces a line really moved (from the ledger, so a later serial
    edit cannot break voiding); falls back to the serials typed on the line."""
    units = {}
    for e in (LedgerEntry.objects.filter(line=line, unit__isnull=False)
              .select_related("unit").order_by("id")):
        units.setdefault(e.unit_id, e.unit)
    return [u.ident for u in units.values()] or list(line.serials or [])


def void_document(document_id, user, reason, ip=None):
    """Void an approved document by applying its exact inverse as a new document.
    Original rows and ledger entries stay untouched."""
    _require_perm(user, "inventory.void_document")
    reason = (reason or "").strip()
    if not reason:
        raise InventoryError("ثبت دلیل ابطال الزامی است.")
    with transaction.atomic():
        orig = StockDocument.objects.select_for_update().get(pk=document_id)
        if orig.status != StockDocument.Status.APPROVED or orig.is_reversal:
            raise InventoryError("فقط سند تأییدشده (غیر معکوس) قابل ابطال است.")
        cfg = InventorySettings.get()
        now = timezone.now()
        rev = StockDocument.objects.create(
            occurred_at=now, created_by=user, note=f"ابطال سند #{orig.pk}: {reason}",
            idempotency_key=f"void-{orig.pk}", reverses=orig)
        rev_lines = []
        orig_lines = list(orig.lines.select_related("item_type").order_by("pk"))
        kids = defaultdict(list)
        for l in orig_lines:
            if l.parent_line_id:
                kids[l.parent_line_id].append(l)
        ordered = []
        for l in reversed([x for x in orig_lines if not x.parent_line_id]):
            ordered.append(l)           # the parent first (server comes back) ...
            ordered.extend(kids.get(l.pk, []))  # ... then the parts that left with it
        for l in ordered:
            rev_lines.append(StockLine.objects.create(
                document=rev, item_type=l.item_type, operation=Operation.REVERSAL,
                quantity=l.quantity, unit_price=l.unit_price,
                from_bucket=l.to_bucket, to_bucket=l.from_bucket,
                from_location=l.to_location, to_location=l.from_location,
                server=l.server, server_serial=l.server_serial, serials=_line_idents(l),
                note=f"معکوس قلم #{l.pk}"))
        _execute(rev, rev_lines, cfg)
        rev.status = StockDocument.Status.APPROVED
        rev.decided_by, rev.decided_at = user, now
        rev.save(update_fields=["status", "decided_by", "decided_at"])
        orig.status, orig.void_reason = StockDocument.Status.VOIDED, reason
        orig.save(update_fields=["status", "void_reason"])
        record(user=user, action="document_voided", obj_type="StockDocument", obj_id=orig.pk,
               document_id=orig.pk, reason=reason, after={"reversal": rev.pk}, ip=ip)
    return rev


# ---------------------------------------------------------------- edits (need approval)
UNIT_FIELDS = {"serial_number": "شماره سریال", "label": "برچسب"}
ITEM_FIELDS = {"name": "نام کالا", "category": "دسته‌بندی", "brand": "برند", "model_name": "مدل",
               "sku": "کد کالا", "description": "توضیحات", "unit_label": "واحد شمارش",
               "min_stock": "حداقل موجودی", "capacity": "ظرفیت هر عدد", "capacity_unit": "واحد ظرفیت",
               "is_active": "فعال"}


def _norm(field, v):
    if field == "category":
        return getattr(v, "pk", v)
    if field == "capacity":
        return None if v in (None, "") else f"{Decimal(str(v)):.2f}"
    if field in ("serial_number",):
        v = (v or "").strip()
        return v or None
    if field in ("min_stock",):
        return int(v or 0)
    if field == "is_active":
        return bool(v)
    return (v or "").strip() if isinstance(v, str) or v is None else v


def _show(field, v):
    if v is None or v == "":
        return "—"
    if field == "category":
        from catalog.models import Category
        c = Category.objects.filter(pk=v).first()
        return c.name if c else str(v)
    if field == "is_active":
        return "فعال" if v else "غیرفعال"
    return str(v)


def _build_changes(*, unit=None, item=None, new_values):
    from catalog.models import Category
    target, labels = (unit, UNIT_FIELDS) if unit is not None else (item, ITEM_FIELDS)
    changes = {}
    for f, label in labels.items():
        if f not in new_values:
            continue
        old = _norm(f, getattr(target, f + "_id") if f == "category" else getattr(target, f))
        new = _norm(f, new_values[f])
        if old != new:
            changes[f] = {"label": label, "old": old, "new": new,
                          "old_display": _show(f, old), "new_display": _show(f, new)}
    if not changes:
        raise InventoryError("هیچ تغییری نسبت به مقدار فعلی وارد نشده است.")
    # ---- rules
    if unit is not None:
        it = unit.item_type
        if "serial_number" in changes:
            new = changes["serial_number"]["new"]
            if new is None and (it.is_server or it.tracking_mode == ItemType.Tracking.SERIAL):
                raise InventoryError("شماره سریال این کالا نمی‌تواند خالی باشد.")
            if new is not None and Unit.objects.filter(item_type=it, serial_number=new).exclude(pk=unit.pk).exists():
                raise InventoryError(f"سریال {new} برای «{it.name}» قبلاً ثبت شده است.")
    else:
        if "name" in changes and not changes["name"]["new"]:
            raise InventoryError("نام کالا نمی‌تواند خالی باشد.")
        if "category" in changes and not Category.objects.filter(pk=changes["category"]["new"]).exists():
            raise InventoryError("دسته‌بندی نامعتبر است.")
        if changes.get("min_stock", {}).get("new", 0) < 0:
            raise InventoryError("حداقل موجودی نمی‌تواند منفی باشد.")
        probe = ItemType.objects.get(pk=item.pk)
        for f, c in changes.items():
            if f == "category":
                probe.category_id = c["new"]
            elif f == "capacity":
                probe.capacity = None if c["new"] is None else Decimal(c["new"])
            else:
                setattr(probe, f, c["new"])
        try:
            probe.clean()
        except Exception as e:  # ValidationError
            raise InventoryError(" ".join(getattr(e, "messages", [str(e)])))
    return changes


def create_change_request(*, user, unit=None, item=None, new_values, note=""):
    _require_perm(user, "inventory.submit_document")
    if (unit is None) == (item is None):
        raise InventoryError("هدف ویرایش مشخص نیست.")
    pending = ChangeRequest.objects.filter(status=ChangeRequest.Status.PENDING,
                                           unit=unit, item_type=item)
    if pending.exists():
        raise InventoryError(f"یک درخواست ویرایش در انتظار تأیید برای همین مورد وجود دارد (#{pending.first().pk}).")
    changes = _build_changes(unit=unit, item=item, new_values=new_values)
    with transaction.atomic():
        cr = ChangeRequest.objects.create(unit=unit, item_type=item, changes=changes, note=note[:255],
                                          created_by=user)
        record(user=user, action="change_created", obj_type="ChangeRequest", obj_id=cr.pk, after=changes)
    return cr


def approve_change(change_id, user, ip=None):
    from catalog.models import Category
    _require_perm(user, "inventory.approve_document")
    with transaction.atomic():
        cr = ChangeRequest.objects.select_for_update().get(pk=change_id)
        if cr.status != ChangeRequest.Status.PENDING:
            raise InventoryError("فقط درخواست‌های در انتظار تأیید قابل تأیید هستند.")
        if cr.created_by_id == user.pk and not may_self_approve(user):
            raise InventoryError("ثبت‌کننده نمی‌تواند درخواست خودش را تأیید کند.")
        target = Unit.objects.select_for_update().get(pk=cr.unit_id) if cr.unit_id else \
            ItemType.objects.select_for_update().get(pk=cr.item_type_id)
        # the world may have changed since the request: re-validate against the current values
        for f, c in cr.changes.items():
            cur = _norm(f, getattr(target, f + "_id") if f == "category" else getattr(target, f))
            if cur != c["old"]:
                raise InventoryError(f"مقدار «{c['label']}» بعد از ثبت درخواست تغییر کرده است؛ این درخواست را رد و دوباره ثبت کنید.")
        _build_changes(unit=target if cr.unit_id else None, item=target if cr.item_type_id else None,
                       new_values={f: c["new"] for f, c in cr.changes.items()})
        for f, c in cr.changes.items():
            v = c["new"]
            if f == "category":
                target.category = Category.objects.get(pk=v)
            elif f == "capacity":
                target.capacity = None if v is None else Decimal(v)
            else:
                setattr(target, f, v)
        target.save()
        cr.status, cr.decided_by, cr.decided_at = ChangeRequest.Status.APPROVED, user, timezone.now()
        cr.save(update_fields=["status", "decided_by", "decided_at"])
        record(user=user, action="change_approved", obj_type="ChangeRequest", obj_id=cr.pk, ip=ip,
               before={f: c["old"] for f, c in cr.changes.items()},
               after={f: c["new"] for f, c in cr.changes.items()})
    return cr


def reject_change(change_id, user, reason, ip=None):
    _require_perm(user, "inventory.approve_document")
    reason = (reason or "").strip()
    if not reason:
        raise InventoryError("ثبت دلیل رد الزامی است.")
    with transaction.atomic():
        cr = ChangeRequest.objects.select_for_update().get(pk=change_id)
        if cr.status != ChangeRequest.Status.PENDING:
            raise InventoryError("فقط درخواست‌های در انتظار تأیید قابل رد هستند.")
        cr.status, cr.decided_by, cr.decided_at, cr.reject_reason = (
            ChangeRequest.Status.REJECTED, user, timezone.now(), reason)
        cr.save(update_fields=["status", "decided_by", "decided_at", "reject_reason"])
        record(user=user, action="change_rejected", obj_type="ChangeRequest", obj_id=cr.pk,
               reason=reason, ip=ip)
    return cr

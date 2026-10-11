"""Business-logic layer. ALL stock-affecting writes go through this module.

Model: every approved line is a *move* between two "buckets" (free, installed, in_repair, ...)
or between a bucket and the outside world (None). A move writes immutable LedgerEntry rows;
stock is always SUM(delta) over the ledger. Pending documents never touch the ledger.
"""
from decimal import Decimal, InvalidOperation

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone

from audit.models import record
from catalog.models import ItemType

from .models import (OPERATION_MOVES, PRICED_OPERATIONS, SERVER_OPERATIONS, Bucket,
                     InventorySettings, LedgerEntry, Operation, StockDocument, StockLine, Unit,
                     UnitStatus)

ANY = object()  # wildcard for balance filters (None means "exactly NULL")


class InventoryError(Exception):
    """A business rule was violated. Message is safe to show to users."""


# ---------------------------------------------------------------- permissions
def _require_perm(user, perm):
    if user is None or not user.is_active or not user.has_perm(perm):
        raise PermissionDenied("شما مجوز انجام این عملیات را ندارید.")


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
    if mode == ItemType.Tracking.QUANTITY and serials:
        raise InventoryError(f"«{item.name}» تعدادی است و سریال نمی‌پذیرد.")
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

    normalized = [_validate_line(d) for d in lines]
    introduced = set()  # server serials entering the system earlier in this same document
    for n in normalized:
        if n["item_type"].is_server and n["from_bucket"] is None and n["to_bucket"]:
            introduced.update(n["serials"])
        ss = n["server_serial"]
        if ss and ss not in introduced and not Unit.objects.filter(
                item_type__is_server=True, serial_number=ss).exists():
            raise InventoryError(
                f"سرور با سریال {ss} وجود ندارد. قلم سرور باید پیش از قطعاتش در همین درخواست بیاید.")
    try:
        with transaction.atomic():
            doc = StockDocument.objects.create(
                occurred_at=occurred_at or timezone.now(), created_by=user, note=note,
                idempotency_key=idempotency_key)
            for n in normalized:
                StockLine.objects.create(document=doc, **n)
            record(user=user, action="document_created", obj_type="StockDocument",
                   obj_id=doc.pk, document_id=doc.pk, after={"lines": len(normalized)})
    except IntegrityError:  # concurrent double submit with the same key
        existing = StockDocument.objects.filter(idempotency_key=idempotency_key).first()
        if existing is None:
            raise
        return existing
    return doc


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

    # --- source side: unit state + balance check (under the item-type lock)
    units = []
    if line.serials:
        found = {u.serial_number: u for u in Unit.objects.select_for_update().filter(
            item_type=item, serial_number__in=line.serials).order_by("pk")}
        if fb is None:
            for s in line.serials:
                u = found.get(s)
                if u is None:
                    u = Unit.objects.create(item_type=item, serial_number=s, status=UnitStatus.OUT)
                elif u.status != UnitStatus.OUT:
                    raise InventoryError(f"سریال {s} از قبل در سیستم موجود است.")
                units.append(u)
        else:
            for s in line.serials:
                u = found.get(s)
                if u is None:
                    raise InventoryError(f"سریال {s} برای «{item.name}» یافت نشد.")
                if not _matches(u, fb, floc, server):
                    raise InventoryError(f"سریال {s} در وضعیت/محل مورد نیاز نیست.")
                units.append(u)

    if fb is not None and not cfg.allow_negative_stock:
        bal = get_balance(item, fb,
                          location=None if fb == Bucket.INSTALLED else floc,
                          server=server if fb == Bucket.INSTALLED else None)
        if bal < qty:
            raise InventoryError(
                f"موجودی «{item.name}» کافی نیست (موجود: {bal}، درخواستی: {qty}).")

    # A server may not leave the system / be scrapped while parts are still installed on it.
    if item.is_server and (tb is None or tb == Bucket.SCRAPPED):
        for u in units:
            if components_of(u):
                raise InventoryError(f"ابتدا قطعات نصب‌شده روی سرور {u.serial_number} را جدا کنید.")

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
    if units:
        for u in units:
            if fb:
                entries.append(entry(fb, floc, u, -1))
            if tb:
                entries.append(entry(tb, tloc, u, +1))
    else:
        if fb:
            entries.append(entry(fb, floc, None, -qty))
        if tb:
            entries.append(entry(tb, tloc, None, +qty))
    LedgerEntry.objects.bulk_create(entries)


def _lock_items(lines):
    # Fixed pk order => no deadlocks between concurrent approvals.
    ids = sorted({l.item_type_id for l in lines})
    list(ItemType.objects.select_for_update().filter(pk__in=ids).order_by("pk"))


def _execute(doc, lines, cfg):
    _lock_items(lines)
    for line in lines:
        _apply_line(line, doc, cfg)


# ---------------------------------------------------------------- decisions
def approve_document(document_id, user, ip=None):
    _require_perm(user, "inventory.approve_document")
    with transaction.atomic():
        doc = StockDocument.objects.select_for_update().get(pk=document_id)
        if doc.status != StockDocument.Status.PENDING:
            raise InventoryError("فقط درخواست‌های در انتظار تأیید قابل تأیید هستند.")
        cfg = InventorySettings.get()
        if doc.created_by_id == user.pk and not cfg.allow_self_approval:
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
        for l in reversed(list(orig.lines.select_related("item_type").order_by("pk"))):
            rev_lines.append(StockLine.objects.create(
                document=rev, item_type=l.item_type, operation=Operation.REVERSAL,
                quantity=l.quantity, unit_price=l.unit_price,
                from_bucket=l.to_bucket, to_bucket=l.from_bucket,
                from_location=l.to_location, to_location=l.from_location,
                server=l.server, server_serial=l.server_serial, serials=l.serials,
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

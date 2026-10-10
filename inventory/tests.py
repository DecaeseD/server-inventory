from decimal import Decimal

from django.contrib.auth.models import Group, User
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase

from audit.models import AuditEvent, ImmutableError
from catalog.models import Category, ItemType

from . import services as S
from .models import (Bucket, InventorySettings, LedgerEntry, Location, Operation,
                     StockDocument, StockLine, Unit, UnitStatus)


_COUNTER = 0


class Base(TestCase):
    @classmethod
    def setUpTestData(cls):
        from accounts.apps import create_roles
        create_roles(None)
        cls.keeper = User.objects.create_user("keeper")
        cls.keeper.groups.add(Group.objects.get(name="انباردار"))
        cls.manager = User.objects.create_user("manager")
        cls.manager.groups.add(Group.objects.get(name="مدیر کل"))
        cls.accountant = User.objects.create_user("acc")
        cls.accountant.groups.add(Group.objects.get(name="حسابدار"))
        cat = Category.objects.create(name="قطعات")
        mk = lambda n, **kw: ItemType.objects.create(name=n, category=cat, **kw)
        cls.ram = mk("RAM 32GB", tracking_mode="serial")
        cls.cpu = mk("CPU Xeon", tracking_mode="quantity")
        cls.server_t = mk("HP G10", tracking_mode="serial", is_server=True)
        cls.main = Location.objects.create(name="انبار اصلی")
        cls.second = Location.objects.create(name="انبار دوم")

    def key(self):
        global _COUNTER
        _COUNTER += 1
        return f"k{_COUNTER}"

    def line(self, item, op, qty, **kw):
        return dict(item_type=item, operation=op, quantity=qty, **kw)

    def submit(self, *lines, user=None):
        return S.create_document(user=user or self.keeper, lines=list(lines),
                                 idempotency_key=self.key())

    def approve(self, *lines):
        doc = self.submit(*lines)
        S.approve_document(doc.pk, self.manager)
        return doc

    def stock(self, item, bucket=Bucket.FREE, **kw):
        return S.get_balance(item, bucket, **kw)

    def buy_server(self, serial="SRV1"):
        return self.approve(self.line(self.server_t, Operation.PURCHASE, 1, to_location=self.main,
                                      serials=[serial]))


class WorkflowTests(Base):
    def test_pending_does_not_change_stock(self):
        self.submit(self.line(self.cpu, Operation.PURCHASE, 5, to_location=self.main))
        self.assertEqual(self.stock(self.cpu), 0)
        self.assertEqual(LedgerEntry.objects.count(), 0)

    def test_approve_increases_and_issue_decreases(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 5, to_location=self.main))
        self.assertEqual(self.stock(self.cpu), 5)
        self.approve(self.line(self.cpu, Operation.ISSUE, 2, from_location=self.main))
        self.assertEqual(self.stock(self.cpu), 3)

    def test_cannot_issue_more_than_available(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 2, to_location=self.main))
        doc = self.submit(self.line(self.cpu, Operation.ISSUE, 3, from_location=self.main))
        with self.assertRaises(S.InventoryError):
            S.approve_document(doc.pk, self.manager)
        doc.refresh_from_db()
        self.assertEqual(doc.status, StockDocument.Status.PENDING)
        self.assertEqual(self.stock(self.cpu), 2)

    def test_multiline_failure_is_all_or_nothing(self):
        doc = self.submit(
            self.line(self.cpu, Operation.PURCHASE, 4, to_location=self.main),
            self.line(self.ram, Operation.ISSUE, 1, from_location=self.main, serials=["NOPE"]))
        with self.assertRaises(S.InventoryError):
            S.approve_document(doc.pk, self.manager)
        self.assertEqual(LedgerEntry.objects.count(), 0)
        self.assertEqual(self.stock(self.cpu), 0)

    def test_reject_requires_reason(self):
        doc = self.submit(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main))
        with self.assertRaises(S.InventoryError):
            S.reject_document(doc.pk, self.manager, "  ")
        S.reject_document(doc.pk, self.manager, "اشتباه")
        doc.refresh_from_db()
        self.assertEqual((doc.status, doc.reject_reason), ("rejected", "اشتباه"))
        self.assertEqual(self.stock(self.cpu), 0)

    def test_keeper_cannot_approve_and_no_self_approval(self):
        doc = self.submit(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main))
        with self.assertRaises(PermissionDenied):
            S.approve_document(doc.pk, self.keeper)
        mdoc = self.submit(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main),
                           user=self.manager) if self.manager.has_perm("inventory.submit_document") else None
        self.assertIsNotNone(mdoc)
        with self.assertRaises(S.InventoryError):
            S.approve_document(mdoc.pk, self.manager)
        cfg = InventorySettings.get(); cfg.allow_self_approval = True; cfg.save()
        S.approve_document(mdoc.pk, self.manager)

    def test_accountant_has_no_implicit_permissions(self):
        with self.assertRaises(PermissionDenied):
            S.create_document(user=self.accountant, idempotency_key="x", lines=[
                self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main)])
        doc = self.submit(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main))
        with self.assertRaises(PermissionDenied):
            S.approve_document(doc.pk, self.accountant)

    def test_approved_document_cannot_be_approved_twice(self):
        doc = self.approve(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main))
        with self.assertRaises(S.InventoryError):
            S.approve_document(doc.pk, self.manager)
        self.assertEqual(self.stock(self.cpu), 1)

    def test_idempotency(self):
        l = self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main)
        a = S.create_document(user=self.keeper, lines=[l], idempotency_key="same")
        b = S.create_document(user=self.keeper, lines=[l], idempotency_key="same")
        self.assertEqual(a.pk, b.pk)
        self.assertEqual(StockDocument.objects.count(), 1)

    def test_price_totals_use_decimal(self):
        doc = self.submit(self.line(self.cpu, Operation.PURCHASE, 3, to_location=self.main,
                                    unit_price="50000.10"))
        self.assertEqual(doc.lines.get().total_price, Decimal("150000.30"))

    def test_validation_errors(self):
        bad = [
            self.line(self.cpu, Operation.PURCHASE, 0, to_location=self.main),
            self.line(self.cpu, Operation.PURCHASE, 1),                       # no location
            self.line(self.ram, Operation.PURCHASE, 2, to_location=self.main, serials=["A"]),
            self.line(self.ram, Operation.PURCHASE, 2, to_location=self.main, serials=["A", "A"]),
            self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main, serials=["A"]),
            self.line(self.cpu, Operation.INSTALL, 1, from_location=self.main),  # no server
            self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main, unit_price="-1"),
        ]
        for b in bad:
            with self.assertRaises(S.InventoryError, msg=b):
                self.submit(b)


class ServerPartsTests(Base):
    def test_install_moves_part_out_of_free_stock_without_double_count(self):
        self.buy_server()
        self.approve(self.line(self.ram, Operation.PURCHASE, 4, to_location=self.main,
                               serials=["R1", "R2", "R3", "R4"]))
        server = Unit.objects.get(serial_number="SRV1")
        self.approve(self.line(self.ram, Operation.INSTALL, 2, from_location=self.main,
                               server=server, serials=["R1", "R2"]))
        self.assertEqual(self.stock(self.ram), 2)
        self.assertEqual(self.stock(self.ram, Bucket.INSTALLED), 2)
        self.assertEqual(self.stock(self.ram, Bucket.INSTALLED, server=server), 2)
        self.assertEqual(self.stock(self.server_t), 1)  # the server itself counted once
        self.assertEqual(sum(S.stock_summary(self.ram).values()), 4)
        r1 = Unit.objects.get(serial_number="R1")
        self.assertEqual((r1.status, r1.installed_in_id, r1.location_id), ("installed", server.pk, None))
        self.assertEqual(S.find_inconsistencies(), [])

    def test_uninstall_returns_to_free(self):
        self.buy_server()
        self.approve(self.line(self.ram, Operation.PURCHASE, 1, to_location=self.main, serials=["R1"]))
        server = Unit.objects.get(serial_number="SRV1")
        self.approve(self.line(self.ram, Operation.INSTALL, 1, from_location=self.main,
                               server=server, serials=["R1"]))
        self.approve(self.line(self.ram, Operation.UNINSTALL, 1, to_location=self.second,
                               server=server, serials=["R1"]))
        self.assertEqual(self.stock(self.ram, location=self.second), 1)
        self.assertEqual(self.stock(self.ram, Bucket.INSTALLED), 0)
        self.assertEqual(Unit.objects.get(serial_number="R1").status, "free")

    def test_quantity_parts_on_server_and_cannot_uninstall_more_than_installed(self):
        self.buy_server()
        self.approve(self.line(self.cpu, Operation.PURCHASE, 3, to_location=self.main))
        server = Unit.objects.get(serial_number="SRV1")
        self.approve(self.line(self.cpu, Operation.INSTALL, 2, from_location=self.main, server=server))
        self.assertEqual(S.components_of(server), [(self.cpu.pk, 2)])
        doc = self.submit(self.line(self.cpu, Operation.UNINSTALL, 3, to_location=self.main, server=server))
        with self.assertRaises(S.InventoryError):
            S.approve_document(doc.pk, self.manager)

    def test_cannot_install_unit_that_is_installed_elsewhere(self):
        self.buy_server("A"); self.buy_server("B")
        self.approve(self.line(self.ram, Operation.PURCHASE, 1, to_location=self.main, serials=["R1"]))
        a, b = Unit.objects.get(serial_number="A"), Unit.objects.get(serial_number="B")
        self.approve(self.line(self.ram, Operation.INSTALL, 1, from_location=self.main, server=a, serials=["R1"]))
        doc = self.submit(self.line(self.ram, Operation.INSTALL, 1, from_location=self.main, server=b, serials=["R1"]))
        with self.assertRaises(S.InventoryError):
            S.approve_document(doc.pk, self.manager)

    def test_duplicate_serial_purchase_rejected(self):
        self.approve(self.line(self.ram, Operation.PURCHASE, 1, to_location=self.main, serials=["R1"]))
        doc = self.submit(self.line(self.ram, Operation.PURCHASE, 1, to_location=self.main, serials=["R1"]))
        with self.assertRaises(S.InventoryError):
            S.approve_document(doc.pk, self.manager)

    def test_datacenter_and_repair_buckets_are_separate(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 5, to_location=self.main))
        self.approve(self.line(self.cpu, Operation.SEND_REPAIR, 2, from_location=self.main, to_location=self.second))
        self.approve(self.line(self.cpu, Operation.SEND_DC, 1, from_location=self.main, to_location=self.second))
        s = S.stock_summary(self.cpu)
        self.assertEqual((s["free"], s["in_repair"], s["at_datacenter"]), (2, 2, 1))
        self.approve(self.line(self.cpu, Operation.REPAIR_RETURN, 2, from_location=self.second, to_location=self.main))
        self.assertEqual(self.stock(self.cpu), 4)

    def test_transfer_between_locations(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 5, to_location=self.main))
        self.approve(self.line(self.cpu, Operation.TRANSFER, 2, from_location=self.main, to_location=self.second))
        self.assertEqual(self.stock(self.cpu, location=self.main), 3)
        self.assertEqual(self.stock(self.cpu, location=self.second), 2)
        self.assertEqual(self.stock(self.cpu), 5)


class VoidAndImmutabilityTests(Base):
    def test_void_purchase_keeps_history_and_restores_balance(self):
        doc = self.approve(self.line(self.ram, Operation.PURCHASE, 2, to_location=self.main, serials=["R1", "R2"]))
        before = LedgerEntry.objects.count()
        S.void_document(doc.pk, self.manager, "ثبت اشتباه")
        doc.refresh_from_db()
        self.assertEqual(doc.status, "voided")
        self.assertEqual(self.stock(self.ram), 0)
        self.assertGreater(LedgerEntry.objects.count(), before)  # append-only, nothing deleted
        self.assertEqual(Unit.objects.get(serial_number="R1").status, UnitStatus.OUT)
        self.assertTrue(doc.reversed_by.is_reversal)
        self.assertEqual(S.find_inconsistencies(), [])
        with self.assertRaises(S.InventoryError):
            S.void_document(doc.pk, self.manager, "دوباره")

    def test_void_fails_if_stock_already_consumed(self):
        doc = self.approve(self.line(self.cpu, Operation.PURCHASE, 2, to_location=self.main))
        self.approve(self.line(self.cpu, Operation.ISSUE, 2, from_location=self.main))
        with self.assertRaises(S.InventoryError):
            S.void_document(doc.pk, self.manager, "x")
        doc.refresh_from_db()
        self.assertEqual(doc.status, "approved")
        self.assertEqual(self.stock(self.cpu), 0)

    def test_void_install_restores_unit(self):
        self.buy_server()
        self.approve(self.line(self.ram, Operation.PURCHASE, 1, to_location=self.main, serials=["R1"]))
        server = Unit.objects.get(serial_number="SRV1")
        doc = self.approve(self.line(self.ram, Operation.INSTALL, 1, from_location=self.main, server=server, serials=["R1"]))
        S.void_document(doc.pk, self.manager, "نصب اشتباه")
        u = Unit.objects.get(serial_number="R1")
        self.assertEqual((u.status, u.location_id, u.installed_in_id), ("free", self.main.pk, None))
        self.assertEqual(self.stock(self.ram), 1)

    def test_void_requires_permission_and_reason(self):
        doc = self.approve(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main))
        with self.assertRaises(PermissionDenied):
            S.void_document(doc.pk, self.keeper, "x")
        with self.assertRaises(S.InventoryError):
            S.void_document(doc.pk, self.manager, "")

    def test_ledger_audit_and_lines_are_immutable(self):
        doc = self.approve(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main))
        e = LedgerEntry.objects.first()
        with self.assertRaises(ImmutableError): e.delete()
        with self.assertRaises(ImmutableError): LedgerEntry.objects.all().delete()
        with self.assertRaises(ImmutableError): LedgerEntry.objects.all().update(delta=99)
        e.delta = 99
        with self.assertRaises(ImmutableError): e.save()
        ev = AuditEvent.objects.first()
        with self.assertRaises(ImmutableError): ev.delete()
        with self.assertRaises(ImmutableError): AuditEvent.objects.all().update(reason="x")
        line = doc.lines.get(); line.quantity = 100
        with self.assertRaises(ImmutableError): line.save()
        with self.assertRaises(ImmutableError): line.delete()

    def test_audit_events_recorded(self):
        doc = self.approve(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main))
        actions = set(AuditEvent.objects.filter(document_id=doc.pk).values_list("action", flat=True))
        self.assertEqual(actions, {"document_created", "document_approved"})

    def test_tracking_mode_locked_after_history(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main))
        self.cpu.tracking_mode = "serial"
        with self.assertRaises(ValidationError):
            self.cpu.save()

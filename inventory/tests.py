from decimal import Decimal
from unittest import mock

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

    def submit_unchecked(self, *lines, user=None):
        """Register a request WITHOUT the submit-time dry run, to test approval-time failures
        (state can change between submit and approve)."""
        with mock.patch.object(S, "_dry_run"):
            return self.submit(*lines, user=user)

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
        with self.assertRaises(S.InventoryError):          # rejected already at submit time
            self.submit(self.line(self.cpu, Operation.ISSUE, 3, from_location=self.main))
        doc = self.submit_unchecked(self.line(self.cpu, Operation.ISSUE, 3, from_location=self.main))
        with self.assertRaises(S.InventoryError):          # and still re-checked at approval
            S.approve_document(doc.pk, self.manager)
        doc.refresh_from_db()
        self.assertEqual(doc.status, StockDocument.Status.PENDING)
        self.assertEqual(self.stock(self.cpu), 2)

    def test_multiline_failure_is_all_or_nothing(self):
        doc = self.submit_unchecked(
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
        with self.assertRaises(S.InventoryError):
            self.submit(self.line(self.cpu, Operation.UNINSTALL, 3, to_location=self.main, server=server))
        doc = self.submit_unchecked(self.line(self.cpu, Operation.UNINSTALL, 3, to_location=self.main, server=server))
        with self.assertRaises(S.InventoryError):
            S.approve_document(doc.pk, self.manager)

    def test_cannot_install_unit_that_is_installed_elsewhere(self):
        self.buy_server("A"); self.buy_server("B")
        self.approve(self.line(self.ram, Operation.PURCHASE, 1, to_location=self.main, serials=["R1"]))
        a, b = Unit.objects.get(serial_number="A"), Unit.objects.get(serial_number="B")
        self.approve(self.line(self.ram, Operation.INSTALL, 1, from_location=self.main, server=a, serials=["R1"]))
        with self.assertRaises(S.InventoryError):
            self.submit(self.line(self.ram, Operation.INSTALL, 1, from_location=self.main, server=b, serials=["R1"]))
        doc = self.submit_unchecked(self.line(self.ram, Operation.INSTALL, 1, from_location=self.main, server=b, serials=["R1"]))
        with self.assertRaises(S.InventoryError):
            S.approve_document(doc.pk, self.manager)

    def test_duplicate_serial_purchase_rejected(self):
        self.approve(self.line(self.ram, Operation.PURCHASE, 1, to_location=self.main, serials=["R1"]))
        with self.assertRaises(S.InventoryError):
            self.submit(self.line(self.ram, Operation.PURCHASE, 1, to_location=self.main, serials=["R1"]))
        doc = self.submit_unchecked(self.line(self.ram, Operation.PURCHASE, 1, to_location=self.main, serials=["R1"]))
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


class SubmitTimeValidationTests(Base):
    """The request must be refused when it is submitted, not only when it is approved."""

    def test_unknown_server_serial_is_refused_at_submit(self):
        self.buy_server("esfand")
        with self.assertRaises(S.InventoryError) as cm:
            self.submit(self.line(self.server_t, Operation.SEND_DC, 1, from_location=self.main,
                                  to_location=self.second, serials=["taha"]))
        self.assertIn("taha", str(cm.exception))
        self.assertEqual(StockDocument.objects.filter(status="pending").count(), 0)

    def test_server_in_wrong_state_or_place_is_refused(self):
        self.buy_server("S1")
        self.approve(self.line(self.server_t, Operation.SEND_DC, 1, from_location=self.main,
                               to_location=self.second, serials=["S1"]))
        with self.assertRaises(S.InventoryError):       # it is at the datacenter now, not free
            self.submit(self.line(self.server_t, Operation.SEND_DC, 1, from_location=self.main,
                                  to_location=self.second, serials=["S1"]))
        with self.assertRaises(S.InventoryError):       # right state, wrong place
            self.submit(self.line(self.server_t, Operation.DC_RETURN, 1, from_location=self.main,
                                  to_location=self.main, serials=["S1"]))
        self.submit(self.line(self.server_t, Operation.DC_RETURN, 1, from_location=self.second,
                              to_location=self.main, serials=["S1"]))

    def test_lines_of_one_request_see_each_other(self):
        doc = self.submit(
            self.line(self.cpu, Operation.PURCHASE, 3, to_location=self.main),
            self.line(self.cpu, Operation.ISSUE, 2, from_location=self.main))
        self.assertEqual(doc.lines.count(), 2)
        self.assertEqual(self.stock(self.cpu), 0)       # still pending: nothing booked

    def test_refused_request_leaves_nothing_behind(self):
        with self.assertRaises(S.InventoryError):
            self.submit(self.line(self.cpu, Operation.PURCHASE, 3, to_location=self.main),
                        self.line(self.ram, Operation.ISSUE, 1, from_location=self.main, serials=["X"]))
        self.assertEqual((StockDocument.objects.count(), StockLine.objects.count(), Unit.objects.count()), (0, 0, 0))

    def test_error_names_the_line(self):
        with self.assertRaises(S.InventoryError) as cm:
            self.submit(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main),
                        self.line(self.cpu, Operation.ISSUE, 5, from_location=self.main))
        self.assertIn("قلم 2", str(cm.exception))


class HardwareTravelsWithServerTests(Base):
    def setUp(self):
        self.buy_server("SRV1")
        self.approve(self.line(self.ram, Operation.PURCHASE, 4, to_location=self.main,
                               serials=["R1", "R2", "R3", "R4"]))
        self.srv = Unit.objects.get(serial_number="SRV1")

    def install_two(self):
        self.approve(self.line(self.ram, Operation.INSTALL, 2, from_location=self.main,
                               server=self.srv, serials=["R1", "R2"]))

    def test_parts_are_deducted_when_installed_not_when_server_ships(self):
        self.assertEqual(self.stock(self.ram), 4)
        self.install_two()
        self.assertEqual(self.stock(self.ram), 2)
        self.approve(self.line(self.server_t, Operation.SEND_DC, 1, from_location=self.main,
                               to_location=self.second, serials=["SRV1"]))
        self.assertEqual(self.stock(self.ram), 2)                        # unchanged by the shipment
        self.assertEqual(self.stock(self.ram, Bucket.INSTALLED), 2)
        self.assertEqual(self.stock(self.server_t), 0)
        self.assertEqual(self.stock(self.server_t, Bucket.AT_DATACENTER), 1)
        self.assertEqual(sum(S.stock_summary(self.ram).values()), 4)     # nothing counted twice
        self.approve(self.line(self.server_t, Operation.DC_RETURN, 1, from_location=self.second,
                               to_location=self.main, serials=["SRV1"]))
        self.assertEqual(S.components_of(self.srv), [(self.ram.pk, 2)])  # came back with its RAM
        self.assertEqual(S.find_inconsistencies(), [])

    def test_install_and_ship_in_one_request(self):
        self.approve(
            self.line(self.server_t, Operation.SEND_DC, 1, from_location=self.main,
                      to_location=self.second, serials=["SRV1"]),
            self.line(self.ram, Operation.INSTALL, 2, from_location=self.main,
                      server_serial="SRV1", serials=["R1", "R2"]))
        self.assertEqual(S.components_of(self.srv), [(self.ram.pk, 2)])
        self.assertEqual(self.stock(self.ram), 2)
        self.assertEqual(Unit.objects.get(serial_number="SRV1").status, UnitStatus.AT_DATACENTER)

    def test_parts_arriving_with_server_from_datacenter(self):
        self.approve(self.line(self.server_t, Operation.SEND_DC, 1, from_location=self.main,
                               to_location=self.second, serials=["SRV1"]))
        self.approve(
            self.line(self.server_t, Operation.DC_RETURN, 1, from_location=self.second,
                      to_location=self.main, serials=["SRV1"]),
            self.line(self.ram, Operation.ENTER_INSTALLED, 2, server_serial="SRV1", serials=["N1", "N2"],
                      unit_price="5"))
        self.assertEqual(self.stock(self.ram, Bucket.INSTALLED, server=self.srv), 2)
        self.assertEqual(self.stock(self.ram), 4)                        # purchased stock untouched

    def test_issue_server_takes_its_parts_with_it_in_any_typed_order(self):
        self.install_two()
        doc = self.approve(                                              # part typed AFTER the server
            self.line(self.server_t, Operation.ISSUE, 1, from_location=self.main, serials=["SRV1"]),
            self.line(self.ram, Operation.INSTALL, 1, from_location=self.main, server_serial="SRV1",
                      serials=["R3"]))
        self.assertEqual(Unit.objects.get(serial_number="SRV1").status, UnitStatus.OUT)
        for r in ("R1", "R2", "R3"):
            self.assertEqual(Unit.objects.get(serial_number=r).status, UnitStatus.OUT, r)
        self.assertEqual(self.stock(self.ram, Bucket.INSTALLED), 0)
        self.assertEqual(self.stock(self.ram), 1)                        # only R4 is left in stock
        self.assertEqual(S.find_inconsistencies(), [])
        kids = StockLine.objects.filter(document=doc, parent_line__isnull=False)
        self.assertEqual(kids.get().operation, Operation.EXIT_INSTALLED)
        self.assertEqual(sorted(kids.get().serials), ["R1", "R2", "R3"])

    def test_void_of_issue_with_parts_restores_server_and_parts(self):
        self.install_two()
        doc = self.approve(self.line(self.server_t, Operation.ISSUE, 1, from_location=self.main,
                                     serials=["SRV1"]))
        S.void_document(doc.pk, self.manager, "اشتباه")
        self.assertEqual(Unit.objects.get(serial_number="SRV1").status, UnitStatus.FREE)
        r1 = Unit.objects.get(serial_number="R1")
        self.assertEqual((r1.status, r1.installed_in_id), (UnitStatus.INSTALLED, self.srv.pk))
        self.assertEqual(S.components_of(self.srv), [(self.ram.pk, 2)])
        self.assertEqual(S.find_inconsistencies(), [])

    def test_issue_with_quantity_parts(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 3, to_location=self.main))
        self.approve(self.line(self.cpu, Operation.INSTALL, 2, from_location=self.main, server=self.srv))
        self.approve(self.line(self.server_t, Operation.ISSUE, 1, from_location=self.main, serials=["SRV1"]))
        self.assertEqual(self.stock(self.cpu, Bucket.INSTALLED), 0)
        self.assertEqual(self.stock(self.cpu), 1)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_scrap_still_requires_removing_parts_first(self):
        self.install_two()
        with self.assertRaises(S.InventoryError):
            self.submit(self.line(self.server_t, Operation.SCRAP, 1, from_location=self.main,
                                  to_location=self.main, serials=["SRV1"]))

    def test_move_part_to_another_server_and_to_free_stock(self):
        self.buy_server("SRV2")
        srv2 = Unit.objects.get(serial_number="SRV2")
        self.install_two()
        self.approve(                                                    # R1: SRV1 -> SRV2 (one request)
            self.line(self.ram, Operation.UNINSTALL, 1, to_location=self.main, server=self.srv, serials=["R1"]),
            self.line(self.ram, Operation.INSTALL, 1, from_location=self.main, server=srv2, serials=["R1"]))
        self.assertEqual(self.stock(self.ram, Bucket.INSTALLED, server=srv2), 1)
        self.assertEqual(self.stock(self.ram, Bucket.INSTALLED, server=self.srv), 1)
        self.approve(self.line(self.ram, Operation.UNINSTALL, 1, to_location=self.main, server=self.srv,
                               serials=["R2"]))                           # R2: back to free stock
        self.assertEqual(Unit.objects.get(serial_number="R2").status, UnitStatus.FREE)
        self.assertEqual(self.stock(self.ram), 3)
        self.assertEqual(S.find_inconsistencies(), [])


class EveryPieceHasIdentityTests(Base):
    def test_quantity_purchase_creates_one_identified_unit_per_piece(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 5, to_location=self.main))
        units = Unit.objects.filter(item_type=self.cpu)
        self.assertEqual(units.count(), 5)
        codes = list(units.values_list("asset_code", flat=True))
        self.assertEqual(len(set(codes)), 5)
        self.assertTrue(all(codes))
        self.assertEqual(self.stock(self.cpu), 5)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_codes_keep_running_across_purchases(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 2, to_location=self.main))
        self.approve(self.line(self.cpu, Operation.PURCHASE, 2, to_location=self.main))
        self.assertEqual(Unit.objects.filter(item_type=self.cpu).values("asset_code").distinct().count(), 4)

    def test_serial_units_get_codes_too(self):
        self.approve(self.line(self.ram, Operation.PURCHASE, 1, to_location=self.main, serials=["R1"]))
        self.assertTrue(Unit.objects.get(serial_number="R1").asset_code)

    def test_issue_by_quantity_picks_units_and_records_which(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 5, to_location=self.main))
        doc = self.approve(self.line(self.cpu, Operation.ISSUE, 2, from_location=self.main))
        out = Unit.objects.filter(item_type=self.cpu, status=UnitStatus.OUT)
        self.assertEqual(out.count(), 2)
        self.assertEqual(sorted(doc.lines.get().serials), sorted(out.values_list("asset_code", flat=True)))
        self.assertEqual(self.stock(self.cpu), 3)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_void_issue_brings_back_the_same_pieces(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 3, to_location=self.main))
        doc = self.approve(self.line(self.cpu, Operation.ISSUE, 2, from_location=self.main))
        gone = set(Unit.objects.filter(status=UnitStatus.OUT, item_type=self.cpu).values_list("pk", flat=True))
        S.void_document(doc.pk, self.manager, "x")
        self.assertEqual(Unit.objects.filter(item_type=self.cpu).count(), 3)   # no phantom new units
        self.assertEqual(Unit.objects.filter(pk__in=gone, status=UnitStatus.FREE).count(), 2)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_void_purchase_of_quantity_item(self):
        doc = self.approve(self.line(self.cpu, Operation.PURCHASE, 3, to_location=self.main))
        S.void_document(doc.pk, self.manager, "x")
        self.assertEqual(self.stock(self.cpu), 0)
        self.assertEqual(Unit.objects.filter(item_type=self.cpu, status=UnitStatus.OUT).count(), 3)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_a_specific_piece_can_be_addressed_by_asset_code(self):
        self.approve(self.line(self.cpu, Operation.PURCHASE, 3, to_location=self.main))
        target = Unit.objects.filter(item_type=self.cpu).order_by("-pk").first()
        self.approve(self.line(self.cpu, Operation.ISSUE, 1, from_location=self.main, serials=[target.asset_code]))
        target.refresh_from_db()
        self.assertEqual(target.status, UnitStatus.OUT)

    def test_quantity_item_cannot_be_bought_with_typed_serials(self):
        with self.assertRaises(S.InventoryError):
            self.submit(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main, serials=["X"]))

    def test_transfer_and_install_move_the_same_pieces(self):
        self.buy_server("S1")
        srv = Unit.objects.get(serial_number="S1")
        self.approve(self.line(self.cpu, Operation.PURCHASE, 3, to_location=self.main))
        self.approve(self.line(self.cpu, Operation.TRANSFER, 2, from_location=self.main, to_location=self.second))
        self.assertEqual(Unit.objects.filter(item_type=self.cpu, location=self.second).count(), 2)
        self.approve(self.line(self.cpu, Operation.INSTALL, 2, from_location=self.second, server=srv))
        self.assertEqual(Unit.objects.filter(item_type=self.cpu, installed_in=srv).count(), 2)
        self.assertEqual(S.find_inconsistencies(), [])

    def test_legacy_unitless_stock_is_still_usable(self):
        """Stock booked before per-piece identity existed (ledger rows without a unit)."""
        self.approve(self.line(self.cpu, Operation.PURCHASE, 2, to_location=self.main))
        # simulate legacy: 3 extra pieces recorded with no unit
        doc = self.approve(self.line(self.cpu, Operation.PURCHASE, 1, to_location=self.main))
        line = doc.lines.get()
        LedgerEntry.objects.bulk_create([LedgerEntry(
            document=doc, line=line, item_type=self.cpu, bucket=Bucket.FREE, location=self.main,
            unit=None, delta=3, occurred_at=doc.occurred_at)])
        self.assertEqual(self.stock(self.cpu), 6)
        self.approve(self.line(self.cpu, Operation.ISSUE, 5, from_location=self.main))   # 3 identified + 2 legacy
        self.assertEqual(self.stock(self.cpu), 1)

from django.core.management.base import BaseCommand
from accounts.apps import create_roles


class Command(BaseCommand):
    help = "Create default roles (مدیر کل / انباردار / حسابدار). --reset re-applies default permissions."

    def add_arguments(self, parser):
        parser.add_argument("--reset", action="store_true")

    def handle(self, *args, **opts):
        create_roles(reset=opts["reset"])
        self.stdout.write("roles ready")

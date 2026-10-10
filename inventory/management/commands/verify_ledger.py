from django.core.management.base import BaseCommand, CommandError
from inventory.services import find_inconsistencies


class Command(BaseCommand):
    help = "Cross-check unit states against the ledger."

    def handle(self, *args, **opts):
        problems = find_inconsistencies()
        if problems:
            raise CommandError("\n".join(problems))
        self.stdout.write("ledger OK")

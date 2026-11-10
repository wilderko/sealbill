"""Clients of record (audit SEC-1).

A payment-link checkout that matched no contact used to make the organization's contact for the typed
e-mail, from whatever the visitor typed, before anyone paid. Every later invoice for that address (the
API, the staff form, the US-resident issuer's copy) was then made out to it, and name lookups offered it
as somebody who had paid.

`of_record` marks the contacts lookups may find and new invoices may reuse. A checkout's own contact (a
link buyer) is not one, so it holds its address beside a client of record, never in its place: the unique
e-mail constraints now apply among clients of record only. Every existing contact starts as one;
`links.0003` classifies the ones checkouts made, by plaintext columns alone.

Undoing this migration leaves the column and the conditional constraints in place (`KeptOnReverse`): the old
unconditional constraints cannot come back once a link buyer shares an address with a client.
"""
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class KeptOnReverse(Operation):
    """`operation`, applied as usual, and left in the database when the migration is undone (review S1, F4).

    Undoing this migration cannot restore the old unconditional unique constraints once a link buyer shares an
    address with a client of record, which happens with the first such checkout. So undoing it changes only the
    migration state: the column stays (its database default keeps older code working), and so do the
    conditional constraints, which still keep every client of record's address unique. Applying it again skips
    what is already there, so migrating forward after a reverse works."""

    reduces_to_sql = False
    reversible = True

    def __init__(self, operation):
        self.operation = operation

    def deconstruct(self):
        return (self.__class__.__qualname__, [self.operation], {})

    def state_forwards(self, app_label, state):
        self.operation.state_forwards(app_label, state)

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if not self._in_place(app_label, schema_editor, from_state):
            self.operation.database_forwards(app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        pass

    def describe(self):
        return f"{self.operation.describe()} (kept when undone)"

    def _in_place(self, app_label, schema_editor, from_state) -> bool:
        op = self.operation
        table = from_state.apps.get_model(app_label, op.model_name)._meta.db_table
        connection = schema_editor.connection
        with connection.cursor() as cursor:
            if isinstance(op, migrations.AddField):
                columns = {c.name for c in connection.introspection.get_table_description(cursor, table)}
                return op.name in columns
            names = set(connection.introspection.get_constraints(cursor, table))
        if isinstance(op, migrations.RemoveConstraint):
            return op.name not in names
        return op.constraint.name in names  # AddConstraint


class Migration(migrations.Migration):

    dependencies = [
        ('contacts', '0002_contact_copy_for_org'),
    ]

    operations = [
        KeptOnReverse(migrations.AddField(
            model_name='contact',
            name='of_record',
            field=models.BooleanField(db_default=True, default=True, editable=False),
        )),
        KeptOnReverse(migrations.AddField(
            model_name='contactemail',
            name='of_record',
            field=models.BooleanField(db_default=True, default=True, editable=False),
        )),
        KeptOnReverse(migrations.RemoveConstraint(
            model_name='contactemail',
            name='unique_native_email_per_org',
        )),
        KeptOnReverse(migrations.RemoveConstraint(
            model_name='contactemail',
            name='unique_copy_email_per_requester',
        )),
        KeptOnReverse(migrations.AddConstraint(
            model_name='contactemail',
            constraint=models.UniqueConstraint(condition=models.Q(('copy_for_org__isnull', True), ('of_record', True)),
                                               fields=('org', 'address_index'), name='unique_native_email_per_org'),
        )),
        KeptOnReverse(migrations.AddConstraint(
            model_name='contactemail',
            constraint=models.UniqueConstraint(condition=models.Q(('copy_for_org__isnull', False), ('of_record', True)),
                                               fields=('org', 'address_index', 'copy_for_org'),
                                               name='unique_copy_email_per_requester'),
        )),
    ]

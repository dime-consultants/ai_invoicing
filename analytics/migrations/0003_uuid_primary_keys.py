import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("analytics", "0002_report_organization"),
        ("organizations", "0002_uuid_primary_key"),
        ("users", "0007_uuid_primary_keys_and_email_otp"),
    ]

    operations = [
        migrations.AddField(
            model_name="report",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            lambda apps, schema_editor: [
                (setattr(obj, "public_id", uuid.uuid4()), obj.save(update_fields=["public_id"]))
                for obj in apps.get_model("analytics", "Report").objects.filter(public_id__isnull=True)
            ],
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="report",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
    ]

import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="organization",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            lambda apps, schema_editor: [
                (setattr(obj, "public_id", uuid.uuid4()), obj.save(update_fields=["public_id"]))
                for obj in apps.get_model("organizations", "Organization").objects.filter(public_id__isnull=True)
            ],
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="organization",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
    ]

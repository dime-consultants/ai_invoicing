import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0002_uuid_primary_key"),
        ("tools", "0003_toolcall_organization_tooldefinition_organization_and_more"),
        ("users", "0007_uuid_primary_keys_and_email_otp"),
    ]

    operations = [
        migrations.AddField(
            model_name="tooldefinition",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="toolcall",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="usertoolconfig",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            lambda apps, schema_editor: [
                (setattr(obj, "public_id", uuid.uuid4()), obj.save(update_fields=["public_id"]))
                for model_name in ("ToolDefinition", "ToolCall", "UserToolConfig")
                for obj in apps.get_model("tools", model_name).objects.filter(public_id__isnull=True)
            ],
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="tooldefinition",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="toolcall",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="usertoolconfig",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
    ]

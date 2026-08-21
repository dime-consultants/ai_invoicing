import uuid

from django.db import migrations, models
import chat.models


class Migration(migrations.Migration):

    dependencies = [
        ("ai_engine", "0004_uuid_primary_keys"),
        ("chat", "0002_alter_workflow_workflow_type"),
        ("uploads", "0005_uuid_primary_keys_and_file_paths"),
        ("users", "0007_uuid_primary_keys_and_email_otp"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatconversation",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="chatmessage",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="chatmessageattachment",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="workflow",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            lambda apps, schema_editor: [
                (setattr(obj, "public_id", uuid.uuid4()), obj.save(update_fields=["public_id"]))
                for model_name in ("ChatConversation", "ChatMessage", "ChatMessageAttachment", "Workflow")
                for obj in apps.get_model("chat", model_name).objects.filter(public_id__isnull=True)
            ],
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="chatconversation",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="chatmessage",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="chatmessageattachment",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="workflow",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="chatmessageattachment",
            name="file",
            field=models.FileField(upload_to=chat.models.chat_attachment_upload_path),
        ),
    ]

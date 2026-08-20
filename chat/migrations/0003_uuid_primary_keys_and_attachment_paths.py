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
        migrations.AlterField(
            model_name="chatconversation",
            name="id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
        ),
        migrations.AlterField(
            model_name="chatmessage",
            name="id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
        ),
        migrations.AlterField(
            model_name="chatmessageattachment",
            name="id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
        ),
        migrations.AlterField(
            model_name="workflow",
            name="id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
        ),
        migrations.AlterField(
            model_name="chatmessageattachment",
            name="file",
            field=models.FileField(upload_to=chat.models.chat_attachment_upload_path),
        ),
    ]

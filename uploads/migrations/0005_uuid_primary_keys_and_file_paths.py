import uuid

from django.db import migrations, models
import uploads.models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0002_uuid_primary_key"),
        ("users", "0007_uuid_primary_keys_and_email_otp"),
        ("uploads", "0004_uploadbatch_organization"),
    ]

    operations = [
        migrations.AlterField(
            model_name="uploadbatch",
            name="id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
        ),
        migrations.AlterField(
            model_name="uploadedfile",
            name="id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
        ),
        migrations.AlterField(
            model_name="uploadedfile",
            name="file",
            field=models.FileField(upload_to=uploads.models._batch_upload_path),
        ),
        migrations.AddField(
            model_name="uploadedfile",
            name="checksum_sha256",
            field=models.CharField(blank=True, db_index=True, help_text="SHA-256 checksum of the uploaded binary for audit and deduplication.", max_length=64),
        ),
    ]

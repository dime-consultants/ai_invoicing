import uuid

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("ai_engine", "0003_aianalysisjob_organization"),
        ("organizations", "0002_uuid_primary_key"),
        ("uploads", "0005_uuid_primary_keys_and_file_paths"),
        ("users", "0007_uuid_primary_keys_and_email_otp"),
    ]

    operations = [
        migrations.AlterField(
            model_name="aianalysisjob",
            name="id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
        ),
        migrations.AlterField(
            model_name="aiinsight",
            name="id",
            field=models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False),
        ),
    ]

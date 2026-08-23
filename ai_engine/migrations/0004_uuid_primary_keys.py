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
        migrations.AddField(
            model_name="aianalysisjob",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="aiinsight",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            lambda apps, schema_editor: [
                (setattr(obj, "public_id", uuid.uuid4()), obj.save(update_fields=["public_id"]))
                for model_name in ("AIAnalysisJob", "AIInsight")
                for obj in apps.get_model("ai_engine", model_name).objects.filter(public_id__isnull=True)
            ],
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="aianalysisjob",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AlterField(
            model_name="aiinsight",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
    ]

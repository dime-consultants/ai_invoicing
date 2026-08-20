import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("organizations", "0002_uuid_primary_key"),
        ("users", "0006_organization_backfill"),
    ]

    operations = [
        migrations.AddField(
            model_name="user",
            name="public_id",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(
            lambda apps, schema_editor: [
                (setattr(obj, "public_id", uuid.uuid4()), obj.save(update_fields=["public_id"]))
                for obj in apps.get_model("users", "User").objects.filter(public_id__isnull=True)
            ],
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="user",
            name="public_id",
            field=models.UUIDField(db_index=True, default=uuid.uuid4, editable=False, unique=True),
        ),
        migrations.AddField(
            model_name="user",
            name="email_verified",
            field=models.BooleanField(default=False, help_text="True after the user confirms an email OTP."),
        ),
        migrations.CreateModel(
            name="EmailOTP",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("purpose", models.CharField(choices=[("email_verification", "Email Verification"), ("password_reset", "Password Reset")], max_length=30)),
                ("code_hash", models.CharField(max_length=128)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=5)),
                ("expires_at", models.DateTimeField()),
                ("consumed_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="email_otps", to="users.user")),
            ],
            options={
                "ordering": ["-created_at"],
                "indexes": [
                    models.Index(fields=["user", "purpose", "consumed_at"], name="users_email_user_id_9d8614_idx"),
                    models.Index(fields=["expires_at"], name="users_email_expires_8d6581_idx"),
                ],
            },
        ),
    ]

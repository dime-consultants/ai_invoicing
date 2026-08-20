from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0007_uuid_primary_keys_and_email_otp"),
    ]

    operations = [
        migrations.AlterField(
            model_name="emailotp",
            name="purpose",
            field=models.CharField(
                choices=[
                    ("login", "Login"),
                    ("email_verification", "Email Verification"),
                    ("password_reset", "Password Reset"),
                ],
                max_length=30,
            ),
        ),
    ]

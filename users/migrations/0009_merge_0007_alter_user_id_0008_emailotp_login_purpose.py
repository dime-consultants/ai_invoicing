from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ("users", "0007_alter_user_id"),
        ("users", "0008_emailotp_login_purpose"),
    ]

    operations = []

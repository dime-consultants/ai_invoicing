# users/serializers.py
from django.contrib.auth import authenticate
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from .models import User


class RegisterSerializer(serializers.ModelSerializer):
    """Used for POST /api/users/register/"""
    password  = serializers.CharField(write_only=True, validators=[validate_password])
    password2 = serializers.CharField(write_only=True, label="Confirm password")

    class Meta:
        model  = User
        fields = [
            "username", "email", "first_name", "last_name",
            "password", "password2",
            "department", "phone",
        ]

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password2"):
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs

    def create(self, validated_data):
        # New accounts are always viewers until an admin promotes them
        return User.objects.create_user(role="viewer", **validated_data)


class LoginSerializer(serializers.Serializer):
    """Used for POST /api/users/login/"""
    username = serializers.CharField()
    password = serializers.CharField(write_only=True)

    def validate(self, attrs):
        user = authenticate(
            username=attrs["username"],
            password=attrs["password"],
        )
        if not user:
            raise serializers.ValidationError("Invalid credentials.")
        if not user.is_active:
            raise serializers.ValidationError("This account has been deactivated.")
        attrs["user"] = user
        return attrs


class UserSerializer(serializers.ModelSerializer):
    """Read-only public representation of a user."""
    role_display = serializers.CharField(source="get_role_display", read_only=True)

    class Meta:
        model  = User
        fields = [
            "id", "username", "email",
            "first_name", "last_name",
            "role", "role_display",
            "department", "phone",
            "is_active",
            "date_joined", "created_at",
        ]
        read_only_fields = fields


class UserProfileSerializer(serializers.ModelSerializer):
    """Editable profile fields — excludes role and auth fields."""
    class Meta:
        model  = User
        fields = [
            "id", "username", "email",
            "first_name", "last_name",
            "department", "phone",
        ]
        read_only_fields = ["id", "username"]


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(write_only=True)
    new_password = serializers.CharField(write_only=True, validators=[validate_password])

    def validate_old_password(self, value):
        user = self.context["request"].user
        if not user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def save(self):
        user = self.context["request"].user
        user.set_password(self.validated_data["new_password"])
        user.save(update_fields=["password"])
        return user
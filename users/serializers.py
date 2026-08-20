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
            "email",
            "password", "password2",
            "first_name", "last_name",
            "department", "phone",
        ]

    def validate(self, attrs):
        if attrs["password"] != attrs.pop("password2"):
            raise serializers.ValidationError({"password": "Passwords do not match."})
        return attrs

    def create(self, validated_data):
        email = validated_data["email"]
        user = User.objects.create_user(
            email=email,
            username=email,
            password=validated_data["password"],
            first_name=validated_data.get("first_name", ""),
            last_name=validated_data.get("last_name", ""),
            department=validated_data.get("department", ""),
            phone=validated_data.get("phone", ""),
            role="viewer",
        )
        return user


      

class LoginSerializer(serializers.Serializer):
    """Used for POST /api/users/login/ — accepts email or username"""
    email    = serializers.EmailField()
    password = serializers.CharField(write_only=True)
   

    def validate(self, attrs):
        email    = attrs.get("email", "").strip()
        password = attrs.get("password", "")

        user = authenticate(username=email, password=password)
        if not user:
            raise serializers.ValidationError(
                {"detail": "Invalid email or password."}
            )
        if not user.is_active:
            raise serializers.ValidationError(
                {"detail": "Account is inactive."}
            )

        attrs["user"] = user
        return attrs
        

       


class UserSerializer(serializers.ModelSerializer):
    """Read-only public representation of a user — matches the API contract shape."""
    role_display = serializers.CharField(source="get_role_display", read_only=True)
    # Contract fields
    name       = serializers.SerializerMethodField()
    full_name  = serializers.SerializerMethodField()
    status     = serializers.SerializerMethodField()
    lastActive = serializers.DateTimeField(source="updated_at", read_only=True)
    organization_name = serializers.CharField(source="organization.name", read_only=True, default=None)

    class Meta:
        model  = User
        fields = [
            "id", "public_id", "username", "email",
            "first_name", "last_name", "name", "full_name",
            "role", "role_display",
            "status", "lastActive",
            "department", "phone",
            "organization", "organization_name",
            "is_active", "email_verified",
            "created_at",
        ]
        read_only_fields = fields

    def get_name(self, obj):
        parts = [obj.first_name or "", obj.last_name or ""]
        full = " ".join(p for p in parts if p).strip()
        return full or obj.username

    def get_full_name(self, obj):
        return self.get_name(obj)

    def get_status(self, obj):
        return "active" if obj.is_active else "inactive"


class UserProfileSerializer(serializers.ModelSerializer):
    """Editable profile fields — excludes role and auth fields."""
    class Meta:
        model  = User
        fields = [
            "id", "public_id", "username", "email",
            "first_name", "last_name",
            "department", "phone", "email_verified",
        ]
        read_only_fields = ["id", "public_id", "username", "email_verified"]


class UserAdminSerializer(serializers.ModelSerializer):
    """Writable admin CRUD serializer — backs POST/PUT/PATCH /api/users/.

    UserSerializer marks every field read-only (it's the read-contract
    shape), so it can't back writes — this is the real writable
    counterpart. `organization` is accepted here for shape-completeness
    but the view always overrides it server-side to the requesting
    admin's own organization (see UserListView/UserDetailView).
    """
    password = serializers.CharField(write_only=True, required=False, allow_blank=True)

    class Meta:
        model = User
        fields = [
            "id", "public_id", "username", "email", "first_name", "last_name",
            "role", "department", "phone", "is_active",
            "organization", "password", "created_at",
        ]
        read_only_fields = ["id", "public_id", "created_at"]

    def create(self, validated_data):
        password = validated_data.pop("password", None)
        validated_data.setdefault("username", validated_data.get("email"))
        user = User(**validated_data)
        if password:
            user.set_password(password)
        else:
            user.set_unusable_password()
        user.save()
        return user

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if password:
            instance.set_password(password)
        instance.save()
        return instance


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


class EmailOTPRequestSerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)


class EmailOTPVerifySerializer(serializers.Serializer):
    email = serializers.EmailField(required=False, allow_blank=True)
    code = serializers.CharField(min_length=6, max_length=6)


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    email = serializers.EmailField()
    code = serializers.CharField(min_length=6, max_length=6)
    password = serializers.CharField(write_only=True, validators=[validate_password])

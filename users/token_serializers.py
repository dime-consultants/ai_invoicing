# users/token_serializers.py
from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView
from .models import User
from .serializers import UserSerializer


class KNTokenObtainPairSerializer(TokenObtainPairSerializer):
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # SimpleJWT hardcodes username as required — make it optional
        # so email-only login doesn't fail field validation before validate()
        self.fields[self.username_field].required = False
        self.fields[self.username_field].allow_blank = True
        # Add email field
        self.fields["email"] = serializers.EmailField(required=False, allow_blank=True)

    def validate(self, attrs):
        email    = attrs.get("email", "").strip()
        username = attrs.get(self.username_field, "").strip()
        password = attrs.get("password", "")

        if not email and not username:
            raise serializers.ValidationError(
                {"detail": "Provide email or username."}
            )

        # Resolve email → username before handing off to parent
        if email and not username:
            try:
                user_obj = User.objects.get(email__iexact=email)
                attrs[self.username_field] = user_obj.username
            except User.DoesNotExist:
                raise serializers.ValidationError(
                    {"detail": "No account found with that email address."}
                )

        data = super().validate(attrs)

        # Rename access → token to match frontend contract
        data["token"] = data.pop("access")
        data["user"]  = UserSerializer(self.user).data
        return data


class KNTokenObtainPairView(TokenObtainPairView):
    serializer_class = KNTokenObtainPairSerializer
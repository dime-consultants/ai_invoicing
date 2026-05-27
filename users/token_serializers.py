# users/token_serializers.py
"""
Custom JWT serializer that:
  - Accepts email OR username for login (contract uses email)
  - Returns { "token": "...", "refresh": "...", "user": { ... } }
"""
from django.contrib.auth import authenticate
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.views import TokenObtainPairView
from rest_framework_simplejwt.tokens import RefreshToken
from .models import User
from .serializers import UserSerializer


class KNTokenObtainPairSerializer(TokenObtainPairSerializer):
    """
    Override to accept `email` in addition to `username`.
    The contract sends: { "email": "...", "password": "..." }
    """
    # Add email field; username becomes optional
    email    = serializers.EmailField(required=False, allow_blank=True)
    username = serializers.CharField(required=False, allow_blank=True)

    def validate(self, attrs):
        email    = attrs.get("email", "").strip()
        username = attrs.get("username", "").strip()
        password = attrs.get("password", "")

        # Resolve email → username so the parent validator can work
        if email and not username:
            try:
                user_obj = User.objects.get(email=email)
                attrs["username"] = user_obj.username
            except User.DoesNotExist:
                raise serializers.ValidationError(
                    {"detail": "No account found with that email address."}
                )

        # Let the parent handle authentication + token generation
        data = super().validate(attrs)

        # Rename 'access' → 'token' to match the frontend contract
        data["token"]   = data.pop("access")
        data["user"]    = UserSerializer(self.user).data
        return data


class KNTokenObtainPairView(TokenObtainPairView):
    serializer_class = KNTokenObtainPairSerializer

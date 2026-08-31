from rest_framework import serializers

from .models import Organization, Department


class OrganizationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = ["public_id", "name", "slug", "is_active", "created_at", "updated_at"]
        read_only_fields = fields


class OrganizationCreateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150)
    is_active = serializers.BooleanField(required=False, default=True)


class OrganizationUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=150, required=False)
    is_active = serializers.BooleanField(required=False)


class DepartmentSerializer(serializers.ModelSerializer):
    organization = OrganizationSerializer(read_only=True)

    class Meta:
        model = Department
        fields = ["public_id", "name", "slug", "is_active", "organization", "created_at", "updated_at"]
        read_only_fields = fields


class DepartmentCreateSerializer(serializers.Serializer):
    # Keyed on public_id (not pk) — resolves the incoming UUID straight to an
    # Organization instance in validated_data, matching what
    # DepartmentListCreateView.post already does with validated_data.pop("organization").
    organization = serializers.SlugRelatedField(
        slug_field="public_id", queryset=Organization.objects.all()
    )
    name = serializers.CharField(max_length=100)
    is_active = serializers.BooleanField(required=False, default=True)


class DepartmentUpdateSerializer(serializers.Serializer):
    name = serializers.CharField(max_length=100, required=False)
    is_active = serializers.BooleanField(required=False)


class OrganizationTokenRequestSerializer(serializers.Serializer):
    """Used for POST /api/organizations/token/ — exchanges a consumer
    key/secret pair for a short-lived M2M access token."""
    consumer_key = serializers.CharField()
    consumer_secret = serializers.CharField(trim_whitespace=False)

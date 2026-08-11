# tools/serializers.py
from rest_framework import serializers
from .models import ToolDefinition, ToolCall, UserToolConfig


# ── Existing serializers (unchanged) ─────────────────────────────────────────

class ToolDefinitionSerializer(serializers.ModelSerializer):
    category_display  = serializers.CharField(source="get_category_display", read_only=True)
    tool_type_display = serializers.CharField(source="get_tool_type_display", read_only=True)
    grok_schema       = serializers.SerializerMethodField()
    created_by_username = serializers.CharField(
        source="created_by.username", read_only=True, default=None
    )

    class Meta:
        model  = ToolDefinition
        fields = [
            "id", "name", "display_name", "description",
            "category", "category_display",
            "tool_type", "tool_type_display",
            "parameters_schema", "handler",
            "version", "enabled", "is_safe",
            "created_by_username",
            "grok_schema",
            "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_grok_schema(self, obj):
        return obj.to_grok_schema()


class ToolCallSerializer(serializers.ModelSerializer):
    tool_name      = serializers.CharField(source="tool.name",         read_only=True)
    tool_display   = serializers.CharField(source="tool.display_name", read_only=True)
    duration_ms    = serializers.IntegerField(read_only=True)
    status_display = serializers.CharField(source="get_status_display", read_only=True)

    class Meta:
        model  = ToolCall
        fields = [
            "id", "tool", "tool_name", "tool_display",
            "job",
            "arguments", "result", "error_message",
            "status", "status_display",
            "duration_ms",
            "started_at", "finished_at", "created_at",
        ]
        read_only_fields = fields


# ── UserToolConfig serializer ─────────────────────────────────────────────────

class UserToolConfigSerializer(serializers.ModelSerializer):
    class Meta:
        model  = UserToolConfig
        fields = [
            "webhook_url", "webhook_method", "webhook_headers",
            "webhook_timeout_seconds",
            "system_prompt", "output_schema",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


# ── Custom tool create serializer ─────────────────────────────────────────────

class CustomToolCreateSerializer(serializers.Serializer):
    """
    Validates the full payload for creating a user-defined tool.
    Handles both the ToolDefinition fields and the nested UserToolConfig.
    """

    # ── ToolDefinition fields ─────────────────────────────────────────────────
    name         = serializers.RegexField(
        r"^[a-z][a-z0-9_]{1,79}$",
        help_text=(
            "Snake_case name sent to the LLM. "
            "Must start with a letter, lowercase only, max 80 chars."
        ),
    )
    display_name = serializers.CharField(max_length=120)
    description  = serializers.CharField(
        help_text=(
            "Plain-English description the LLM reads to decide whether to call this tool. "
            "Be specific about what it accepts and returns."
        ),
    )
    category = serializers.ChoiceField(
        choices=[c[0] for c in ToolDefinition.CATEGORY_CHOICES],
        default="utility",
    )
    tool_type = serializers.ChoiceField(
        choices=["webhook", "prompt_transform"],
        help_text="User-defined tools can be 'webhook' or 'prompt_transform' only.",
    )
    parameters_schema = serializers.JSONField(
        default=dict,
        help_text=(
            'JSON Schema for tool inputs. '
            'Example: {"type":"object","properties":{"file_id":{"type":"integer"}},'
            '"required":["file_id"]}'
        ),
    )
    is_safe = serializers.BooleanField(default=True)

    # ── UserToolConfig fields (nested flat for simplicity) ────────────────────
    # Webhook
    webhook_url             = serializers.URLField(required=False, allow_blank=True, default="")
    webhook_method          = serializers.ChoiceField(
        choices=["POST", "GET"], default="POST", required=False,
    )
    webhook_headers         = serializers.JSONField(required=False, default=dict)
    webhook_timeout_seconds = serializers.IntegerField(
        min_value=1, max_value=120, default=30, required=False,
    )

    # Prompt transform
    system_prompt = serializers.CharField(required=False, allow_blank=True, default="")
    output_schema = serializers.JSONField(required=False, allow_null=True, default=None)

    def validate(self, data):
        tool_type = data.get("tool_type")

        if tool_type == "webhook":
            if not data.get("webhook_url"):
                raise serializers.ValidationError(
                    {"webhook_url": "webhook_url is required for webhook tools."}
                )

        if tool_type == "prompt_transform":
            if not data.get("system_prompt", "").strip():
                raise serializers.ValidationError(
                    {"system_prompt": "system_prompt is required for prompt_transform tools."}
                )

        return data

    def validate_name(self, value):
        if ToolDefinition.objects.filter(name=value).exists():
            raise serializers.ValidationError(
                f"A tool named '{value}' already exists. Choose a different name."
            )
        return value

    def validate_parameters_schema(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("parameters_schema must be a JSON object.")
        if value and value.get("type") != "object":
            raise serializers.ValidationError(
                'parameters_schema must have "type": "object" at the top level.'
            )
        return value


class CustomToolUpdateSerializer(serializers.Serializer):
    """
    Validates partial updates to a user-defined tool.
    All fields optional — only provided fields are updated.
    """
    display_name            = serializers.CharField(max_length=120, required=False)
    description             = serializers.CharField(required=False)
    category                = serializers.ChoiceField(
        choices=[c[0] for c in ToolDefinition.CATEGORY_CHOICES], required=False,
    )
    parameters_schema       = serializers.JSONField(required=False)
    is_safe                 = serializers.BooleanField(required=False)
    enabled                 = serializers.BooleanField(required=False)

    # Webhook
    webhook_url             = serializers.URLField(required=False, allow_blank=True)
    webhook_method          = serializers.ChoiceField(
        choices=["POST", "GET"], required=False,
    )
    webhook_headers         = serializers.JSONField(required=False)
    webhook_timeout_seconds = serializers.IntegerField(
        min_value=1, max_value=120, required=False,
    )

    # Prompt transform
    system_prompt = serializers.CharField(required=False, allow_blank=True)
    output_schema = serializers.JSONField(required=False, allow_null=True)


# ── Read serializer for custom tool detail (includes config) ──────────────────

class CustomToolDetailSerializer(serializers.ModelSerializer):
    """
    Full detail of a user-defined tool, including its UserToolConfig.
    Used for GET /api/tools/custom/ and GET /api/tools/custom/<id>/.
    """
    category_display  = serializers.CharField(source="get_category_display", read_only=True)
    tool_type_display = serializers.CharField(source="get_tool_type_display", read_only=True)
    grok_schema       = serializers.SerializerMethodField()
    config            = serializers.SerializerMethodField()

    class Meta:
        model  = ToolDefinition
        fields = [
            "id", "name", "display_name", "description",
            "category", "category_display",
            "tool_type", "tool_type_display",
            "parameters_schema",
            "version", "enabled", "is_safe",
            "grok_schema",
            "config",
            "created_at", "updated_at",
        ]
        read_only_fields = fields

    def get_grok_schema(self, obj):
        return obj.to_grok_schema()

    def get_config(self, obj):
        try:
            return UserToolConfigSerializer(obj.user_config).data
        except UserToolConfig.DoesNotExist:
            return None
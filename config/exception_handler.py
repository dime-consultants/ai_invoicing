# config/exception_handler.py
"""
Custom DRF exception handler that wraps all errors in the contract shape:
    { "error": { "code": "...", "message": "...", "details": null } }
"""
from rest_framework.views import exception_handler
from rest_framework.response import Response
from rest_framework import status


def custom_exception_handler(exc, context):
    response = exception_handler(exc, context)

    if response is not None:
        code = getattr(exc, "default_code", "error")
        # Flatten DRF's nested validation errors into a readable message
        if isinstance(response.data, dict):
            if "detail" in response.data:
                message = str(response.data["detail"])
                details = None
            else:
                message = "Validation error."
                details = response.data
        elif isinstance(response.data, list):
            message = "Validation error."
            details = response.data
        else:
            message = str(response.data)
            details = None

        response.data = {
            "error": {
                "code":    code,
                "message": message,
                "details": details,
            }
        }

    return response

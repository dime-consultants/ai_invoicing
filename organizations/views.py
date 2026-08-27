import logging

from django.core.exceptions import ObjectDoesNotExist

from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from users.decorators import org_admin_required

from .serializers import (
    OrganizationSerializer,
    OrganizationCreateSerializer,
    OrganizationUpdateSerializer,
    DepartmentSerializer,
    DepartmentCreateSerializer,
    DepartmentUpdateSerializer,
)

from .services import (
    OrganizationService,
    DepartmentService,
)


logger = logging.getLogger(__name__)


@api_view(["GET", "POST"])
@org_admin_required
def organization_list_create(request):
    """
    GET  /api/organizations/
    POST /api/organizations/
    """
    if request.method == "GET":
        active_only = (
            request.query_params.get("active_only", "false").lower() == "true"
        )

        organizations = OrganizationService.list_organizations(
            active_only=active_only,
        )

        serializer = OrganizationSerializer(organizations, many=True)

        return Response(serializer.data, status=status.HTTP_200_OK)

    serializer = OrganizationCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    organization = OrganizationService.create_organization(
        **serializer.validated_data
    )

    response_serializer = OrganizationSerializer(organization)

    return Response(response_serializer.data, status=status.HTTP_201_CREATED)


@api_view(["GET", "PATCH", "DELETE"])
@org_admin_required
def organization_detail(request, public_id):
    """
    GET    /api/organizations/<uuid:public_id>/
    PATCH  /api/organizations/<uuid:public_id>/
    DELETE /api/organizations/<uuid:public_id>/

    DELETE performs a soft delete by setting is_active=False.
    """
    try:
        organization = OrganizationService.get_organization(public_id=public_id)
    except ObjectDoesNotExist:
        return Response(
            {"detail": "Organization not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == "GET":
        return Response(OrganizationSerializer(organization).data)

    if request.method == "PATCH":
        serializer = OrganizationUpdateSerializer(
            organization,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)

        try:
            organization = OrganizationService.update_organization(
                organization=organization,
                **serializer.validated_data,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(OrganizationSerializer(organization).data)

    # DELETE
    OrganizationService.deactivate_organization(organization=organization)

    return Response(
        {"detail": "Organization deactivated successfully."},
        status=status.HTTP_200_OK,
    )


@api_view(["GET", "POST"])
@org_admin_required
def department_list_create(request):
    """
    GET  /api/departments/
    POST /api/departments/
    """
    if request.method == "GET":
        organization_public_id = request.query_params.get("organization")

        active_only = (
            request.query_params.get("active_only", "false").lower() == "true"
        )

        organization = None

        if organization_public_id:
            try:
                organization = OrganizationService.get_organization(
                    public_id=organization_public_id
                )
            except ObjectDoesNotExist:
                return Response(
                    {"detail": "Organization not found."},
                    status=status.HTTP_404_NOT_FOUND,
                )

        departments = DepartmentService.list_departments(
            organization=organization,
            active_only=active_only,
        )

        serializer = DepartmentSerializer(departments, many=True)

        return Response(serializer.data, status=status.HTTP_200_OK)

    serializer = DepartmentCreateSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    validated_data = serializer.validated_data

    organization = validated_data.pop("organization")

    # Remove the UUID because validation already
    # converted it to the Organization instance.
    validated_data.pop("organization_public_id", None)

    try:
        department = DepartmentService.create_department(
            organization=organization,
            **validated_data,
        )
    except ValueError as exc:
        return Response(
            {"detail": str(exc)},
            status=status.HTTP_400_BAD_REQUEST,
        )

    return Response(
        DepartmentSerializer(department).data,
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET", "PATCH", "DELETE"])
@org_admin_required
def department_detail(request, public_id):
    """
    GET    /api/departments/<uuid:public_id>/
    PATCH  /api/departments/<uuid:public_id>/
    DELETE /api/departments/<uuid:public_id>/
    """
    try:
        department = DepartmentService.get_department(public_id=public_id)
    except ObjectDoesNotExist:
        return Response(
            {"detail": "Department not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == "GET":
        return Response(DepartmentSerializer(department).data)

    if request.method == "PATCH":
        serializer = DepartmentUpdateSerializer(
            department,
            data=request.data,
            partial=True,
        )
        serializer.is_valid(raise_exception=True)

        try:
            department = DepartmentService.update_department(
                department=department,
                **serializer.validated_data,
            )
        except ValueError as exc:
            return Response(
                {"detail": str(exc)},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(DepartmentSerializer(department).data)

    # DELETE
    DepartmentService.deactivate_department(department=department)

    return Response(
        {"detail": "Department deactivated successfully."},
        status=status.HTTP_200_OK,
    )

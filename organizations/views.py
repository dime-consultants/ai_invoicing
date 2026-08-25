import logging

from django.core.exceptions import ObjectDoesNotExist

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

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

class OrganizationListCreateView(APIView):
    """
    GET  /api/organizations/
    POST /api/organizations/
    """

    permission_classes = [
        IsAuthenticated,
    ]

    def get(self, request):
        active_only = (
            request.query_params.get(
                "active_only",
                "false",
            ).lower()
            == "true"
        )

        organizations = (
            OrganizationService
            .list_organizations(
                active_only=active_only,
            )
        )

        serializer = OrganizationSerializer(
            organizations,
            many=True,
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    def post(self, request):
        serializer = OrganizationCreateSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        organization = (
            OrganizationService
            .create_organization(
                **serializer.validated_data
            )
        )

        response_serializer = OrganizationSerializer(
            organization
        )

        return Response(
            response_serializer.data,
            status=status.HTTP_201_CREATED,
        )

class OrganizationDetailView(APIView):
    """
    GET    /api/organizations/<uuid:public_id>/
    PATCH  /api/organizations/<uuid:public_id>/
    DELETE /api/organizations/<uuid:public_id>/

    DELETE performs a soft delete by setting is_active=False.
    """

    permission_classes = [
        IsAuthenticated,
    ]

    def get_object(self, public_id):

        try:
            return (
                OrganizationService
                .get_organization(
                    public_id=public_id
                )
            )

        except ObjectDoesNotExist:
            return None

    def get(self, request, public_id):

        organization = self.get_object(
            public_id
        )

        if organization is None:
            return Response(
                {
                    "detail":
                    "Organization not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = OrganizationSerializer(
            organization
        )

        return Response(
            serializer.data
        )

    def patch(self, request, public_id):

        organization = self.get_object(
            public_id
        )

        if organization is None:
            return Response(
                {
                    "detail":
                    "Organization not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = OrganizationUpdateSerializer(
            organization,
            data=request.data,
            partial=True,
        )

        serializer.is_valid(
            raise_exception=True
        )

        try:
            organization = (
                OrganizationService
                .update_organization(
                    organization=organization,
                    **serializer.validated_data
                )
            )

        except ValueError as exc:

            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            OrganizationSerializer(
                organization
            ).data
        )

    def delete(self, request, public_id):

        organization = self.get_object(
            public_id
        )

        if organization is None:
            return Response(
                {
                    "detail":
                    "Organization not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        OrganizationService.deactivate_organization(
            organization=organization
        )

        return Response(
            {
                "detail":
                "Organization deactivated successfully."
            },
            status=status.HTTP_200_OK,
        )

class DepartmentListCreateView(APIView):

    permission_classes = [
        IsAuthenticated,
    ]

    def get(self, request):

        organization_public_id = (
            request.query_params.get(
                "organization"
            )
        )

        active_only = (
            request.query_params.get(
                "active_only",
                "false",
            ).lower()
            == "true"
        )

        organization = None

        if organization_public_id:

            try:
                organization = (
                    OrganizationService
                    .get_organization(
                        public_id=organization_public_id
                    )
                )

            except ObjectDoesNotExist:

                return Response(
                    {
                        "detail":
                        "Organization not found."
                    },
                    status=status.HTTP_404_NOT_FOUND,
                )

        departments = (
            DepartmentService
            .list_departments(
                organization=organization,
                active_only=active_only,
            )
        )

        serializer = DepartmentSerializer(
            departments,
            many=True,
        )

        return Response(
            serializer.data,
            status=status.HTTP_200_OK,
        )

    def post(self, request):

        serializer = DepartmentCreateSerializer(
            data=request.data
        )

        serializer.is_valid(
            raise_exception=True
        )

        validated_data = serializer.validated_data

        organization = validated_data.pop(
            "organization"
        )

        # Remove the UUID because validation already
        # converted it to the Organization instance.
        validated_data.pop(
            "organization_public_id",
            None,
        )

        try:

            department = (
                DepartmentService
                .create_department(
                    organization=organization,
                    **validated_data
                )
            )

        except ValueError as exc:

            return Response(
                {
                    "detail": str(exc)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            DepartmentSerializer(
                department
            ).data,
            status=status.HTTP_201_CREATED,
        )
    

class DepartmentDetailView(APIView):
    """
    GET    /api/departments/<uuid:public_id>/
    PATCH  /api/departments/<uuid:public_id>/
    DELETE /api/departments/<uuid:public_id>/
    """

    permission_classes = [
        IsAuthenticated,
    ]

    def get_object(self, public_id):

        try:

            return (
                DepartmentService
                .get_department(
                    public_id=public_id
                )
            )

        except ObjectDoesNotExist:

            return None

    def get(self, request, public_id):

        department = self.get_object(
            public_id
        )

        if department is None:

            return Response(
                {
                    "detail":
                    "Department not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        return Response(
            DepartmentSerializer(
                department
            ).data
        )

    def patch(self, request, public_id):

        department = self.get_object(
            public_id
        )

        if department is None:

            return Response(
                {
                    "detail":
                    "Department not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        serializer = DepartmentUpdateSerializer(
            department,
            data=request.data,
            partial=True,
        )

        serializer.is_valid(
            raise_exception=True
        )

        try:

            department = (
                DepartmentService
                .update_department(
                    department=department,
                    **serializer.validated_data
                )
            )

        except ValueError as exc:

            return Response(
                {
                    "detail":
                    str(exc)
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            DepartmentSerializer(
                department
            ).data
        )

    def delete(self, request, public_id):

        department = self.get_object(
            public_id
        )

        if department is None:

            return Response(
                {
                    "detail":
                    "Department not found."
                },
                status=status.HTTP_404_NOT_FOUND,
            )

        DepartmentService.deactivate_department(
            department=department
        )

        return Response(
            {
                "detail":
                "Department deactivated successfully."
            },
            status=status.HTTP_200_OK,
        )
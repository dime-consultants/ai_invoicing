import logging

from django.db import IntegrityError, transaction
from django.db.models import Prefetch

from .models import Organization, Department


logger = logging.getLogger(__name__)


class OrganizationService:
    """
    Service layer for Organization operations.
    """

    @staticmethod
    def list_organizations(
        *,
        active_only: bool = False,
    ):
        """
        Return organizations.

        Parameters
        ----------
        active_only:
            When True, only active organizations are returned.
        """

        queryset = Organization.objects.all()

        if active_only:
            queryset = queryset.filter(
                is_active=True
            )

        return queryset.order_by("name")

    @staticmethod
    def get_organization(
        *,
        public_id,
        active_only: bool = False,
    ) -> Organization:
        """
        Retrieve an organization using its public UUID.
        """

        queryset = Organization.objects.all()

        if active_only:
            queryset = queryset.filter(
                is_active=True
            )

        return queryset.get(
            public_id=public_id
        )

    @staticmethod
    @transaction.atomic
    def create_organization(
        *,
        name: str,
        is_active: bool = True,
    ) -> Organization:
        """
        Create a new organization.
        """

        organization = Organization.objects.create(
            name=name.strip(),
            is_active=is_active,
        )

        logger.info(
            "Organization created: id=%s public_id=%s name=%s",
            organization.pk,
            organization.public_id,
            organization.name,
        )

        return organization

    @staticmethod
    @transaction.atomic
    def update_organization(
        *,
        organization: Organization,
        **data,
    ) -> Organization:
        """
        Update an existing organization.
        """

        allowed_fields = {
            "name",
            "is_active",
        }

        for field, value in data.items():

            if field not in allowed_fields:
                continue

            if field == "name" and value:
                value = value.strip()

            setattr(
                organization,
                field,
                value,
            )

        organization.save()

        logger.info(
            "Organization updated: id=%s public_id=%s",
            organization.pk,
            organization.public_id,
        )

        return organization

    @staticmethod
    @transaction.atomic
    def deactivate_organization(
        *,
        organization: Organization,
    ) -> Organization:
        """
        Soft deactivate an organization.

        Departments are also deactivated because an inactive
        organization should not expose active departments.
        """

        organization.is_active = False
        organization.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

        organization.departments.update(
            is_active=False
        )

        logger.info(
            "Organization deactivated: id=%s",
            organization.pk,
        )

        return organization


class DepartmentService:
    """
    Service layer for Department operations.
    """

    @staticmethod
    def list_departments(
        *,
        organization=None,
        active_only: bool = False,
    ):
        """
        Return departments.

        Can optionally filter by organization.
        """

        queryset = (
            Department.objects
            .select_related("organization")
            .all()
        )

        if organization is not None:
            queryset = queryset.filter(
                organization=organization
            )

        if active_only:
            queryset = queryset.filter(
                is_active=True,
                organization__is_active=True,
            )

        return queryset.order_by(
            "organization__name",
            "name",
        )

    @staticmethod
    def get_department(
        *,
        public_id,
        active_only: bool = False,
    ) -> Department:
        """
        Retrieve a department using its public UUID.
        """

        queryset = (
            Department.objects
            .select_related("organization")
            .all()
        )

        if active_only:
            queryset = queryset.filter(
                is_active=True,
                organization__is_active=True,
            )

        return queryset.get(
            public_id=public_id
        )

    @staticmethod
    @transaction.atomic
    def create_department(
        *,
        organization: Organization,
        name: str,
        is_active: bool = True,
    ) -> Department:
        """
        Create a department inside an organization.
        """

        if not organization.is_active:
            raise ValueError(
                "Cannot create a department in an inactive organization."
            )

        name = name.strip()

        existing = Department.objects.filter(
            organization=organization,
            name__iexact=name,
        ).first()

        if existing:
            raise ValueError(
                "A department with this name already exists "
                "in this organization."
            )

        try:
            department = Department.objects.create(
                organization=organization,
                name=name,
                is_active=is_active,
            )

        except IntegrityError:
            logger.exception(
                "Failed to create department: "
                "organization=%s name=%s",
                organization.pk,
                name,
            )

            raise

        logger.info(
            "Department created: id=%s organization=%s name=%s",
            department.pk,
            organization.pk,
            department.name,
        )

        return department

    @staticmethod
    @transaction.atomic
    def update_department(
        *,
        department: Department,
        **data,
    ) -> Department:
        """
        Update a department.
        """

        allowed_fields = {
            "name",
            "is_active",
        }

        for field, value in data.items():

            if field not in allowed_fields:
                continue

            if field == "name" and value:

                value = value.strip()

                exists = (
                    Department.objects
                    .filter(
                        organization=department.organization,
                        name__iexact=value,
                    )
                    .exclude(
                        pk=department.pk
                    )
                    .exists()
                )

                if exists:
                    raise ValueError(
                        "A department with this name already exists "
                        "in this organization."
                    )

            setattr(
                department,
                field,
                value,
            )

        department.save()

        logger.info(
            "Department updated: id=%s",
            department.pk,
        )

        return department

    @staticmethod
    @transaction.atomic
    def deactivate_department(
        *,
        department: Department,
    ) -> Department:
        """
        Soft deactivate a department.
        """

        department.is_active = False

        department.save(
            update_fields=[
                "is_active",
                "updated_at",
            ]
        )

        logger.info(
            "Department deactivated: id=%s",
            department.pk,
        )

        return department
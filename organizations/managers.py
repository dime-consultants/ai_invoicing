from django.db import models


class OrganizationQuerySet(models.QuerySet):
    """
    Reusable Organization queryset methods.
    """

    def active(self):
        return self.filter(
            is_active=True
        )

    def inactive(self):
        return self.filter(
            is_active=False
        )


class OrganizationManager(models.Manager):
    """
    Manager for Organization.
    """

    def get_queryset(self):
        return OrganizationQuerySet(
            self.model,
            using=self._db,
        )

    def active(self):
        return self.get_queryset().active()

    def inactive(self):
        return self.get_queryset().inactive()


class DepartmentQuerySet(models.QuerySet):
    """
    Reusable Department queryset methods.
    """

    def active(self):
        return self.filter(
            is_active=True,
            organization__is_active=True,
        )

    def inactive(self):
        return self.filter(
            is_active=False
        )

    def for_organization(self, organization):
        return self.filter(
            organization=organization
        )

    def with_organization(self):
        return self.select_related(
            "organization"
        )


class DepartmentManager(models.Manager):
    """
    Manager for Department.
    """

    def get_queryset(self):
        return DepartmentQuerySet(
            self.model,
            using=self._db,
        )

    def active(self):
        return (
            self.get_queryset()
            .active()
        )

    def inactive(self):
        return (
            self.get_queryset()
            .inactive()
        )

    def for_organization(self, organization):
        return (
            self.get_queryset()
            .for_organization(organization)
        )

    def with_organization(self):
        return (
            self.get_queryset()
            .with_organization()
        )
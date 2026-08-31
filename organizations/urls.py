from django.urls import path

from .views import (
    organization_token,
    organization_list_create,
    organization_detail,
    department_list_create,
    department_detail,
)


urlpatterns = [

    # M2M token exchange — consumer key/secret in, short-lived X-Org-Token out
    path(
        "organizations/token/",
        organization_token,
        name="organization-token",
    ),

    # Organizations
    path(
        "organizations/",
        organization_list_create,
        name="organization-list-create",
    ),

    path(
        "organizations/<uuid:public_id>/",
        organization_detail,
        name="organization-detail",
    ),

    # Departments
    path(
        "departments/",
        department_list_create,
        name="department-list-create",
    ),

    path(
        "departments/<uuid:public_id>/",
        department_detail,
        name="department-detail",
    ),
]

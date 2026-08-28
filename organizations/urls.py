from django.urls import path

from .views import (
    organization_list_create,
    organization_detail,
    department_list_create,
    department_detail,
)


urlpatterns = [

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

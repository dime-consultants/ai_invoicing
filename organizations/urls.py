from django.urls import path

from .views import (
    OrganizationListCreateView,
    OrganizationDetailView,
    DepartmentListCreateView,
    DepartmentDetailView,
)


urlpatterns = [

    # Organizations
    path(
        "organizations/",
        OrganizationListCreateView.as_view(),
        name="organization-list-create",
    ),

    path(
        "organizations/<uuid:public_id>/",
        OrganizationDetailView.as_view(),
        name="organization-detail",
    ),

    # Departments
    path(
        "departments/",
        DepartmentListCreateView.as_view(),
        name="department-list-create",
    ),

    path(
        "departments/<uuid:public_id>/",
        DepartmentDetailView.as_view(),
        name="department-detail",
    ),
]
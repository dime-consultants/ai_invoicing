# analytics/dashboard_urls.py — mounted at /api/dashboard/
from django.urls import path
from .views import (
    DashboardStatsView,
    DashboardActivityView,
    DashboardRecentActivityView,
)

urlpatterns = [
    # GET /api/dashboard/stats
    path("stats/",           DashboardStatsView.as_view(),         name="dashboard-stats"),

    # GET /api/dashboard/activity?period=week
    path("activity/",        DashboardActivityView.as_view(),      name="dashboard-activity"),

    # GET /api/dashboard/recent-activity
    path("recent-activity/", DashboardRecentActivityView.as_view(), name="dashboard-recent-activity"),
]

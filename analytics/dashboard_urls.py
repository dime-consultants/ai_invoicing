# analytics/dashboard_urls.py — mounted at /api/dashboard/
from django.urls import path
from .views import (
    dashboard_stats,
    dashboard_activity,
    dashboard_recent_activity,
    dashboard_processing,
)

urlpatterns = [
    # GET /api/dashboard/stats
    path("stats/",           dashboard_stats,          name="dashboard-stats"),

    # GET /api/dashboard/activity?period=week
    path("activity/",        dashboard_activity,       name="dashboard-activity"),

    # GET /api/dashboard/recent-activity
    path("recent-activity/", dashboard_recent_activity, name="dashboard-recent-activity"),

    # GET /api/dashboard/processing
    path("processing/",      dashboard_processing,      name="dashboard-processing"),
]

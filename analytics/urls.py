# analytics/urls.py — mounted at /api/analytics/
from django.urls import path
from .views import analytics_overview, analytics_chart

urlpatterns = [
    # GET /api/analytics/overview?period=month
    path("overview/", analytics_overview, name="analytics-overview"),

    # GET /api/analytics/charts/<chartType>
    # chartType: processing-trend | accuracy-trend | category-breakdown | user-activity
    path("charts/<str:chart_type>/", analytics_chart, name="analytics-chart"),
]

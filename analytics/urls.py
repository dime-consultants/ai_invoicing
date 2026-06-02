# analytics/urls.py — mounted at /api/analytics/
from django.urls import path
from .views import AnalyticsOverviewView, AnalyticsChartView

urlpatterns = [
    # GET /api/analytics/overview?period=month
    path("overview/", AnalyticsOverviewView.as_view(), name="analytics-overview"),

    # GET /api/analytics/charts/<chartType>
    # chartType: processing-trend | accuracy-trend | category-breakdown | user-activity
    path("charts/<str:chart_type>/", AnalyticsChartView.as_view(), name="analytics-chart"),
]

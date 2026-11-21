from django.urls import path
from .views import (
    ReportUnifiedView,
    ReportOverviewView,
    ReportSalesView,
    ReportCollectionsView,
    ReportOutstandingView,
    ReportProductsView,
    ReportCustomersView,
    ReportDeliveriesView,
    ReportDiscountsView,
    ReportProfitLossView,
    ReportExportPdfView,
    ReportExportExcelView,
)

urlpatterns = [
    path('unified/', ReportUnifiedView.as_view(), name='report-unified'),
    path('overview/', ReportOverviewView.as_view(), name='report-overview'),
    path('sales/', ReportSalesView.as_view(), name='report-sales'),
    path('collections/', ReportCollectionsView.as_view(), name='report-collections'),
    path('outstanding/', ReportOutstandingView.as_view(), name='report-outstanding'),
    path('products/', ReportProductsView.as_view(), name='report-products'),
    path('customers/', ReportCustomersView.as_view(), name='report-customers'),
    path('deliveries/', ReportDeliveriesView.as_view(), name='report-deliveries'),
    path('discounts/', ReportDiscountsView.as_view(), name='report-discounts'),
    path('profit-loss/', ReportProfitLossView.as_view(), name='report-profit-loss'),
    path('export/pdf/', ReportExportPdfView.as_view(), name='report-export-pdf'),
    path('export/excel/', ReportExportExcelView.as_view(), name='report-export-excel'),
]

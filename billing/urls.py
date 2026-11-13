from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CustomerViewSet, InvoiceViewSet, CompanyViewSet

router = DefaultRouter()
router.register(r'companies', CompanyViewSet, basename='billing-company')
router.register(r'customers', CustomerViewSet, basename='billing-customer')
router.register(r'invoices', InvoiceViewSet, basename='billing-invoice')

urlpatterns = [
    path('', include(router.urls)),
]

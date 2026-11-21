from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import (
    CustomerViewSet,
    InvoiceViewSet,
    CompanyViewSet,
    DraftInvoiceViewSet,
    ExpenseViewSet,
    PaymentViewSet,
)

router = DefaultRouter()
router.register(r'companies', CompanyViewSet, basename='billing-company')
router.register(r'customers', CustomerViewSet, basename='billing-customer')
router.register(r'drafts', DraftInvoiceViewSet, basename='billing-draft')
router.register(r'invoices', InvoiceViewSet, basename='billing-invoice')
router.register(r'expenses', ExpenseViewSet, basename='billing-expense')
router.register(r'payments', PaymentViewSet, basename='billing-payment')

urlpatterns = [
    path('', include(router.urls)),
]


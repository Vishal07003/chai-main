from django.urls import path, include
from rest_framework.routers import DefaultRouter
from .views import CategoryViewSet, ProductViewSet, ProductImageUploadView

router = DefaultRouter()
router.register(r'categories', CategoryViewSet)
router.register(r'items', ProductViewSet)

urlpatterns = [
    path('upload-image/', ProductImageUploadView.as_view(), name='product_image_upload'),
    path('', include(router.urls)),
]


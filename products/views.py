from rest_framework import viewsets
from .models import Category, Product
from .serializers import CategorySerializer, ProductSerializer
from accounts.permissions import IsOwnerOrReadOnly

class CategoryViewSet(viewsets.ModelViewSet):
    """
    List & Retrieve open to anyone. Create, Update, Delete restricted to Owner / Admin.
    """
    queryset = Category.objects.all().order_by('name')
    serializer_class = CategorySerializer
    permission_classes = [IsOwnerOrReadOnly]

class ProductViewSet(viewsets.ModelViewSet):
    """
    List & Retrieve open to anyone. Create, Update, Delete restricted to Owner / Admin.
    Supports filtering by category and availability.
    """
    queryset = Product.objects.select_related('category').all().order_by('-created_at')
    serializer_class = ProductSerializer
    permission_classes = [IsOwnerOrReadOnly]

    def get_queryset(self):
        queryset = super().get_queryset()
        category_id = self.request.query_params.get('category')
        is_available = self.request.query_params.get('available')

        if category_id:
            queryset = queryset.filter(category_id=category_id)
        if is_available is not None:
            queryset = queryset.filter(is_available=is_available.lower() in ['true', '1'])
        return queryset

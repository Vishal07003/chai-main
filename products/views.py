import os
import uuid
from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import IsAuthenticated
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


    def perform_destroy(self, instance):
        """
        Safe deletion/deactivation:
        Inspect order and invoice relationships.
        If the product has ever been referenced in historical OrderItem or InvoiceItem records,
        do NOT hard delete it. Soft-deactivate (is_available = False) to safeguard historical integrity.
        Hard delete is only performed if never used in any transaction.
        """
        from orders.models import OrderItem
        from billing.models import InvoiceItem

        has_orders = OrderItem.objects.filter(product=instance).exists()
        has_invoices = InvoiceItem.objects.filter(product=instance).exists()

        if has_orders or has_invoices:
            instance.is_available = False
            instance.save(update_fields=['is_available'])
        else:
            instance.delete()


class ProductImageUploadView(APIView):
    """
    Uploads a product image file to backend media storage and returns the authoritative URL.
    Restricted to Owner/Admin accounts.
    """
    parser_classes = [MultiPartParser, FormParser]
    permission_classes = [IsAuthenticated, IsOwnerOrReadOnly]

    def post(self, request, *args, **kwargs):
        image_file = request.FILES.get('image') or request.FILES.get('file')
        if not image_file:
            return Response({'error': 'No image file provided in request.'}, status=status.HTTP_400_BAD_REQUEST)

        # Validate file size (max 10MB)
        if image_file.size > 10 * 1024 * 1024:
            return Response({'error': 'Image file size exceeds 10MB limit.'}, status=status.HTTP_400_BAD_REQUEST)

        # Create products media subdirectory if needed
        products_media_dir = os.path.join(settings.MEDIA_ROOT, 'products')
        os.makedirs(products_media_dir, exist_ok=True)

        # Generate unique safe filename
        ext = os.path.splitext(image_file.name)[1].lower()
        if not ext or ext not in ['.jpg', '.jpeg', '.png', '.webp', '.gif']:
            ext = '.jpg'

        filename = f"prod_{uuid.uuid4().hex[:12]}{ext}"
        filepath = os.path.join(products_media_dir, filename)

        with open(filepath, 'wb+') as destination:
            for chunk in image_file.chunks():
                destination.write(chunk)

        # Build absolute or relative URL
        media_url_path = f"{settings.MEDIA_URL.rstrip('/')}/products/{filename}"
        full_url = request.build_absolute_uri(media_url_path)

        return Response({
            'success': True,
            'image_url': full_url,
            'filename': filename,
        }, status=status.HTTP_201_CREATED)


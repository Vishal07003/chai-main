from django.urls import path
from .views import RegisterView, CurrentUserProfileView

urlpatterns = [
    path('register/', RegisterView.as_view(), name='register'),
    path('profile/', CurrentUserProfileView.as_view(), name='current-user-profile'),
]

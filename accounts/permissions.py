from rest_framework import permissions

class IsOwnerUser(permissions.BasePermission):
    """
    Allows access only to users with the 'owner' role or staff/superuser.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and (
                request.user.is_staff or
                request.user.is_superuser or
                (hasattr(request.user, 'profile') and request.user.profile.role == 'owner')
            )
        )

class IsDeliveryUser(permissions.BasePermission):
    """
    Allows access only to users with the 'delivery' role or owner/staff.
    """
    def has_permission(self, request, view):
        return bool(
            request.user and request.user.is_authenticated and (
                request.user.is_staff or
                (hasattr(request.user, 'profile') and request.user.profile.role in ['delivery', 'owner'])
            )
        )

class IsOwnerOrReadOnly(permissions.BasePermission):
    """
    Read-only for anyone, write permissions only for owner/staff.
    """
    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return bool(
            request.user and request.user.is_authenticated and (
                request.user.is_staff or
                request.user.is_superuser or
                (hasattr(request.user, 'profile') and request.user.profile.role == 'owner')
            )
        )


# Chai Flow Backend (Django REST Framework)

This backend serves the **Chai-Flow-app** (React Native / Expo frontend).

---

## 🚀 Quick Start

### 1. Run migrations (already applied)
```bash
python manage.py migrate
```

### 2. Create a superuser (for Django admin)
```bash
python manage.py createsuperuser
```

### 3. Run the development server
```bash
python manage.py runserver 0.0.0.0:8000
```

> **Note for React Native / Physical Devices / Emulators:**
> Running on `0.0.0.0:8000` allows access from:
> - Android Emulator: `http://10.0.2.2:8000/api/v1/`
> - iOS Simulator / Local PC: `http://localhost:8000/api/v1/`
> - Physical Phone (on same Wi-Fi): `http://<YOUR_PC_LOCAL_IP>:8000/api/v1/`

---

## 📡 API Endpoints

### 🔐 Authentication (`accounts/`)
- `POST /api/v1/auth/token/` - Get JWT tokens (`access` & `refresh`)
- `POST /api/v1/auth/token/refresh/` - Refresh JWT token
- `GET /api/v1/accounts/profile/` - Current user profile

### ☕ Products & Menu (`products/`)
- `GET /api/v1/products/categories/` - List categories
- `GET /api/v1/products/items/` - List all chai & snack items
- `POST /api/v1/products/items/` - Add new product (Owner)

### 📦 Orders (`orders/`)
- `GET /api/v1/orders/` - List orders
- `POST /api/v1/orders/` - Place new order
- `GET /api/v1/orders/{id}/` - Order details
- `PATCH /api/v1/orders/{id}/` - Update order status (Pending -> Accepted -> Delivered)

### 🚴 Deliveries (`deliveries/`)
- `GET /api/v1/deliveries/` - List delivery tasks
- `PATCH /api/v1/deliveries/{id}/` - Update delivery status / GPS coordinates


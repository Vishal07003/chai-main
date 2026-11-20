from django.test import TestCase
from django.contrib.auth.models import User
from rest_framework.test import APIClient
from rest_framework import status
from accounts.models import UserProfile
from products.models import Category, Product
from orders.models import Order

class OrdersTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.owner = User.objects.create_user(username='chaiowner', password='Password123!')
        UserProfile.objects.create(user=self.owner, phone='1111111111', role='owner')

        self.customer1 = User.objects.create_user(username='customer1', password='Password123!')
        UserProfile.objects.create(user=self.customer1, phone='2222222222', role='customer')

        self.customer2 = User.objects.create_user(username='customer2', password='Password123!')
        UserProfile.objects.create(user=self.customer2, phone='3333333333', role='customer')

        self.category = Category.objects.create(name='Chai')
        self.product1 = Product.objects.create(
            name='Cutting Chai', category=self.category, price='15.00', is_available=True
        )
        self.product2 = Product.objects.create(
            name='Bun Maska', category=self.category, price='30.00', is_available=True
        )

    def test_customer_can_create_order_with_calculated_total(self):
        self.client.force_authenticate(user=self.customer1)
        data = {
            'delivery_address': 'Flat 402, Green Avenue',
            'notes': 'Extra hot please',
            'items': [
                {'product_id': self.product1.id, 'quantity': 2},  # 15 * 2 = 30
                {'product_id': self.product2.id, 'quantity': 1},  # 30 * 1 = 30 -> Total 60.00
            ]
        }
        res = self.client.post('/api/v1/orders/', data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        order = Order.objects.get(id=res.data['id'])
        self.assertEqual(order.total_amount, 60.00)
        self.assertEqual(order.items.count(), 2)
        self.assertEqual(order.customer, self.customer1)
        self.assertEqual(order.delivery.status, 'assigned')

    def test_customer_cannot_view_other_customer_orders(self):
        order1 = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00)

        self.client.force_authenticate(user=self.customer2)
        res = self.client.get('/api/v1/orders/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(len(res.data), 0)

        detail_res = self.client.get(f'/api/v1/orders/{order1.id}/')
        self.assertEqual(detail_res.status_code, status.HTTP_404_NOT_FOUND)

    def test_customer_can_cancel_own_pending_order(self):
        """Customers are permitted to cancel their own order only while status is pending."""
        order = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00, status='pending')
        self.client.force_authenticate(user=self.customer1)

        res = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'cancelled'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        order.refresh_from_db()
        self.assertEqual(order.status, 'cancelled')

    def test_customer_cannot_cancel_already_accepted_order(self):
        """Security: customers cannot cancel once the order has been accepted/preparing."""
        order = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00, status='accepted')
        self.client.force_authenticate(user=self.customer1)

        res = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'cancelled'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        order.refresh_from_db()
        self.assertEqual(order.status, 'accepted')

    def test_customer_cannot_force_order_to_delivered(self):
        """Security: customers cannot advance order statuses (e.g. to delivered)."""
        order = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00, status='pending')
        self.client.force_authenticate(user=self.customer1)

        res = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'delivered'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        order.refresh_from_db()
        self.assertEqual(order.status, 'pending')

    def test_customer_cannot_reassign_order_customer(self):
        """Security: customer cannot reassign the owner of the order to someone else."""
        order = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00, status='pending')
        self.client.force_authenticate(user=self.customer1)

        res = self.client.patch(f'/api/v1/orders/{order.id}/', {'customer': self.customer2.id}, format='json')
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_status_transitions_workflow(self):
        """Owner must follow valid state machine transitions: pending -> accepted -> preparing -> out_for_delivery -> delivered."""
        order = Order.objects.create(customer=self.customer1, delivery_address='Address 1', total_amount=50.00, status='pending')
        self.client.force_authenticate(user=self.owner)

        # 1. pending -> accepted: Valid
        res = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'accepted'}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        # 2. accepted -> delivered: Invalid jump (must be preparing or cancelled)
        res_invalid = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'delivered'}, format='json')
        self.assertEqual(res_invalid.status_code, status.HTTP_400_BAD_REQUEST)

        # 3. accepted -> preparing: Valid
        res2 = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'preparing'}, format='json')
        self.assertEqual(res2.status_code, status.HTTP_200_OK)

        # 4. preparing -> out_for_delivery: Valid
        res3 = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'out_for_delivery'}, format='json')
        self.assertEqual(res3.status_code, status.HTTP_200_OK)

        # 5. out_for_delivery -> delivered: Valid
        res4 = self.client.patch(f'/api/v1/orders/{order.id}/', {'status': 'delivered'}, format='json')
        self.assertEqual(res4.status_code, status.HTTP_200_OK)

    def test_recurring_order_full_lifecycle_and_duplicate_prevention(self):
        """Test recurring order creation, pause/resume, skip date, and duplicate prevention."""
        from billing.models import Customer
        from deliveries.models import DeliveryPartner
        from orders.models import RecurringOrder, RecurringOrderExecution

        # 1. Setup Customer and Driver
        cust = Customer.objects.create(name='Recurring Ramesh', phone='9988776655', address='Tech Park Tower A')
        driver_user = User.objects.create_user(username='driver_boy', password='Password123!')
        UserProfile.objects.create(user=driver_user, phone='9988776611', role='delivery')
        partner = DeliveryPartner.objects.create(name='Driver Boy', phone='9988776611', user=driver_user)

        self.client.force_authenticate(user=self.owner)

        # 2. Create Recurring Order
        create_data = {
            'customer_id': cust.id,
            'delivery_partner_id': partner.id,
            'delivery_address': 'Tech Park Tower A, Floor 3',
            'frequency': 'daily',
            'items': [
                {'product_id': self.product1.id, 'quantity': 5},  # 15 * 5 = 75
                {'product_id': self.product2.id, 'quantity': 2},  # 30 * 2 = 60 -> Total 135.00
            ],
            'notes': 'Daily 10 AM Chai',
        }
        res = self.client.post('/api/v1/orders/recurring/', create_data, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        rec_id = res.data['id']

        # 3. Verify detail
        rec_obj = RecurringOrder.objects.get(id=rec_id)
        self.assertEqual(rec_obj.items.count(), 2)
        self.assertEqual(rec_obj.delivery_partner, partner)
        self.assertFalse(rec_obj.is_paused)

        # 4. Generate Order for Today (First time -> 201 Created)
        gen_res = self.client.post(f'/api/v1/orders/recurring/{rec_id}/generate-order/', {}, format='json')
        self.assertEqual(gen_res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(gen_res.data['success'])
        order_id = gen_res.data['order']['id']

        order = Order.objects.get(id=order_id)
        self.assertEqual(order.total_amount, 135.00)
        self.assertEqual(order.delivery.driver, driver_user)
        self.assertFalse(order.has_invoice)  # Business rule: Never automatically create invoice

        # 5. Duplicate Prevention: Attempt to generate for the same date again -> returns existing order
        dup_res = self.client.post(f'/api/v1/orders/recurring/{rec_id}/generate-order/', {}, format='json')
        self.assertEqual(dup_res.status_code, status.HTTP_200_OK)
        self.assertTrue(dup_res.data.get('already_generated'))
        self.assertEqual(dup_res.data['order']['id'], order_id)
        self.assertEqual(Order.objects.filter(customer_profile=cust).count(), 1)

        # 6. Pause & Resume
        pause_res = self.client.post(f'/api/v1/orders/recurring/{rec_id}/pause/')
        self.assertEqual(pause_res.status_code, status.HTTP_200_OK)
        rec_obj.refresh_from_db()
        self.assertTrue(rec_obj.is_paused)

        resume_res = self.client.post(f'/api/v1/orders/recurring/{rec_id}/resume/')
        self.assertEqual(resume_res.status_code, status.HTTP_200_OK)
        rec_obj.refresh_from_db()
        self.assertFalse(rec_obj.is_paused)

        # 7. Skip Date
        skip_res = self.client.post(f'/api/v1/orders/recurring/{rec_id}/skip-date/', {'date': '2026-11-20'}, format='json')
        self.assertEqual(skip_res.status_code, status.HTTP_200_OK)
        rec_obj.refresh_from_db()
        self.assertIn('2026-11-20', rec_obj.skipped_dates)

        # 8. Edit Template Future Quantities (Does not alter historical order #1)
        update_data = {
            'items': [
                {'product_id': self.product1.id, 'quantity': 10},
            ]
        }
        patch_res = self.client.patch(f'/api/v1/orders/recurring/{rec_id}/', update_data, format='json')
        self.assertEqual(patch_res.status_code, status.HTTP_200_OK)

        # Historical order #1 must still have 135.00 and 2 items
        order.refresh_from_db()
        self.assertEqual(order.total_amount, 135.00)
        self.assertEqual(order.items.count(), 2)

    def test_global_and_per_template_notification_settings_phase_2a(self):
        """
        Phase 2A Notification Settings Test:
        - Owner can fetch and patch global recurring notification settings.
        - Unauthorized users (customer) cannot view or patch global settings.
        - Global time hierarchy validation: notification time cannot precede order generation.
        - RecurringOrder supports use_global_notification_settings and custom timings.
        - Updating global settings does not overwrite custom template timings.
        """
        # 1. Non-owner / Customer cannot access global settings
        self.client.force_authenticate(user=self.customer1)
        unauth_get = self.client.get('/api/v1/orders/recurring-settings/')
        self.assertEqual(unauth_get.status_code, status.HTTP_403_FORBIDDEN)

        # 2. Owner can fetch global settings
        self.client.force_authenticate(user=self.owner)
        settings_res = self.client.get('/api/v1/orders/recurring-settings/')
        self.assertEqual(settings_res.status_code, status.HTTP_200_OK)
        self.assertEqual(settings_res.data['order_generation_time'], '06:00:00')
        self.assertEqual(settings_res.data['owner_notification_time'], '06:05:00')
        self.assertEqual(settings_res.data['partner_notification_time'], '06:10:00')
        self.assertTrue(settings_res.data['owner_notification_enabled'])
        self.assertTrue(settings_res.data['partner_notification_enabled'])
        self.assertEqual(settings_res.data['timezone'], 'Asia/Kolkata')

        # 3. Time validation on Global Settings: notification earlier than generation fails
        invalid_patch = self.client.patch('/api/v1/orders/recurring-settings/1/', {
            'order_generation_time': '07:00:00',
            'owner_notification_time': '06:30:00',
        }, format='json')
        self.assertEqual(invalid_patch.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('owner_notification_time', invalid_patch.data)

        # 4. Valid update on Global Settings
        valid_patch = self.client.patch('/api/v1/orders/recurring-settings/1/', {
            'order_generation_time': '05:30:00',
            'owner_notification_time': '05:35:00',
            'partner_notification_time': '05:40:00',
            'owner_notification_enabled': True,
            'partner_notification_enabled': False,
        }, format='json')
        self.assertEqual(valid_patch.status_code, status.HTTP_200_OK)
        self.assertEqual(valid_patch.data['order_generation_time'], '05:30:00')
        self.assertFalse(valid_patch.data['partner_notification_enabled'])

        # 5. Create Template with Custom Notification Settings
        from billing.models import Customer
        cust = Customer.objects.create(name='Global Corp', phone='8888888888', address='Tech Park')
        template_payload = {
            'customer_id': cust.id,
            'delivery_address': 'Tech Park Suite 101',
            'frequency': 'daily',
            'use_global_notification_settings': False,
            'custom_order_generation_time': '08:00:00',
            'custom_owner_notification_time': '08:15:00',
            'custom_partner_notification_time': '08:20:00',
            'owner_notification_enabled': True,
            'partner_notification_enabled': True,
            'items': [
                {'product_id': self.product1.id, 'quantity': 5, 'unit_price': 15.00}
            ]
        }
        create_res = self.client.post('/api/v1/orders/recurring/', template_payload, format='json')
        self.assertEqual(create_res.status_code, status.HTTP_201_CREATED)
        rec_id = create_res.data['id']

        # Verify custom schedule in serializer output
        detail_res = self.client.get(f'/api/v1/orders/recurring/{rec_id}/')
        self.assertEqual(detail_res.status_code, status.HTTP_200_OK)
        self.assertFalse(detail_res.data['use_global_notification_settings'])
        self.assertEqual(detail_res.data['custom_order_generation_time'], '08:00:00')
        eff_schedule = detail_res.data['effective_notification_schedule']
        self.assertTrue(eff_schedule['is_custom'])
        self.assertEqual(eff_schedule['order_generation_time'], '08:00:00')

        # 6. Verify custom schedule time validation (earlier notification rejected)
        invalid_custom = self.client.patch(f'/api/v1/orders/recurring/{rec_id}/', {
            'custom_order_generation_time': '09:00:00',
            'custom_owner_notification_time': '08:30:00',
        }, format='json')
        self.assertEqual(invalid_custom.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('custom_owner_notification_time', invalid_custom.data)

        # 7. Updating Global Settings DOES NOT overwrite custom template timings
        self.client.patch('/api/v1/orders/recurring-settings/1/', {
            'order_generation_time': '04:00:00',
            'owner_notification_time': '04:10:00',
        }, format='json')

        detail_res2 = self.client.get(f'/api/v1/orders/recurring/{rec_id}/')
        self.assertEqual(detail_res2.data['custom_order_generation_time'], '08:00:00')
        self.assertEqual(detail_res2.data['custom_owner_notification_time'], '08:15:00')


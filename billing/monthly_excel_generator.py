from io import BytesIO
from decimal import Decimal
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from django.utils import timezone


def generate_monthly_invoice_excel(invoice) -> bytes:
    """
    Renders professional, formatted Excel (.xlsx) workbook for a Monthly Invoice Breakdown.
    Hierarchy:
      Customer -> Billing Period -> Delivery Date -> Individual Order -> Product Item -> Daily Subtotal
    Summary:
      Subtotal, Discount, Grand Total, Amount Received, Outstanding Balance, Payment Status.
    """
    from .monthly_service import MonthlyBillingService

    breakdown = MonthlyBillingService.get_monthly_invoice_breakdown(invoice)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"Monthly Bill {breakdown['billing_period']}"[:31]

    # Theme styles
    header_fill = PatternFill(start_color="8C331A", end_color="8C331A", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=14, bold=True, color="8C331A")
    meta_font = Font(name="Calibri", size=10, italic=True, color="554A42")
    date_header_fill = PatternFill(start_color="FDF2EC", end_color="FDF2EC", fill_type="solid")
    date_header_font = Font(name="Calibri", size=11, bold=True, color="8C331A")
    daily_sub_fill = PatternFill(start_color="FAF5F0", end_color="FAF5F0", fill_type="solid")
    daily_sub_font = Font(name="Calibri", size=10, bold=True, color="2C201A")
    summary_label_font = Font(name="Calibri", size=10, bold=True, color="4A3E38")
    grand_total_font = Font(name="Calibri", size=12, bold=True, color="8C331A")

    border_thin = Border(
        left=Side(style='thin', color='EFEAE4'),
        right=Side(style='thin', color='EFEAE4'),
        top=Side(style='thin', color='EFEAE4'),
        bottom=Side(style='thin', color='EFEAE4')
    )
    border_double_bottom = Border(
        left=Side(style='thin', color='EFEAE4'),
        right=Side(style='thin', color='EFEAE4'),
        top=Side(style='thin', color='EFEAE4'),
        bottom=Side(style='double', color='8C331A')
    )

    # 1. Company & Invoice Header
    ws['A1'] = "Chai Flow — Monthly Consolidated Invoice"
    ws['A1'].font = title_font

    inv_num = breakdown['invoice_number'] or f"INV-{invoice.id}"
    cust_name = breakdown['customer_name']
    cust_phone = breakdown['customer_phone'] or 'N/A'
    period_str = breakdown['billing_period']
    status_str = breakdown['payment_status']

    ws['A2'] = f"Invoice #: {inv_num}   |   Customer: {cust_name} ({cust_phone})   |   Period: {period_str}   |   Status: {status_str}"
    ws['A2'].font = meta_font

    cur_row = 4
    headers = ["Delivery Date", "Order #", "Product", "Quantity", "Rate (₹)", "Line Amount (₹)"]
    for col_idx, h in enumerate(headers, start=1):
        cell = ws.cell(row=cur_row, column=col_idx, value=h)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal='center' if h in ["Delivery Date", "Order #", "Quantity"] else ('right' if '₹' in h else 'left'))

    cur_row += 1

    # 2. Date-wise and Order-wise rows
    for date_block in breakdown['dates']:
        # Date section header row
        date_lbl = date_block['date_str']
        ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=len(headers))
        d_cell = ws.cell(row=cur_row, column=1, value=f"Delivery Date: {date_lbl}")
        d_cell.fill = date_header_fill
        d_cell.font = date_header_font
        d_cell.alignment = Alignment(horizontal='left')
        for col_idx in range(1, len(headers) + 1):
            ws.cell(row=cur_row, column=col_idx).border = border_thin
        cur_row += 1

        for ord_data in date_block['orders']:
            ord_num = ord_data['order_number']
            if ord_data.get('time_str'):
                ord_num += f" ({ord_data['time_str']})"

            for itm in ord_data['items']:
                ws.cell(row=cur_row, column=1, value=date_lbl).alignment = Alignment(horizontal='center')
                ws.cell(row=cur_row, column=2, value=ord_num).alignment = Alignment(horizontal='center')
                ws.cell(row=cur_row, column=3, value=itm['product_name']).alignment = Alignment(horizontal='left')

                q_cell = ws.cell(row=cur_row, column=4, value=itm['quantity'])
                q_cell.alignment = Alignment(horizontal='center')
                q_cell.number_format = '#,##0'

                r_cell = ws.cell(row=cur_row, column=5, value=float(itm['unit_price']))
                r_cell.alignment = Alignment(horizontal='right')
                r_cell.number_format = '#,##0.00'

                a_cell = ws.cell(row=cur_row, column=6, value=float(itm['line_amount']))
                a_cell.alignment = Alignment(horizontal='right')
                a_cell.number_format = '#,##0.00'

                for col_idx in range(1, len(headers) + 1):
                    ws.cell(row=cur_row, column=col_idx).border = border_thin
                    ws.cell(row=cur_row, column=col_idx).font = Font(name="Calibri", size=10)

                cur_row += 1

        # Daily Subtotal Row
        ws.merge_cells(start_row=cur_row, start_column=1, end_row=cur_row, end_column=3)
        sub_lbl_cell = ws.cell(row=cur_row, column=1, value=f"Daily Subtotal ({date_lbl})")
        sub_lbl_cell.font = daily_sub_font
        sub_lbl_cell.alignment = Alignment(horizontal='right')

        sub_qty_cell = ws.cell(row=cur_row, column=4, value=date_block['daily_total_quantity'])
        sub_qty_cell.font = daily_sub_font
        sub_qty_cell.alignment = Alignment(horizontal='center')
        sub_qty_cell.number_format = '#,##0'

        ws.cell(row=cur_row, column=5, value="")

        sub_amt_cell = ws.cell(row=cur_row, column=6, value=float(date_block['daily_subtotal']))
        sub_amt_cell.font = daily_sub_font
        sub_amt_cell.alignment = Alignment(horizontal='right')
        sub_amt_cell.number_format = '#,##0.00'

        for col_idx in range(1, len(headers) + 1):
            cell = ws.cell(row=cur_row, column=col_idx)
            cell.fill = daily_sub_fill
            cell.border = border_thin

        cur_row += 1

    # 3. Monthly Financial Summary Block
    cur_row += 1
    summary_data = [
        ("Monthly Subtotal (₹)", float(breakdown['subtotal'])),
        ("Discounts (₹)", float(breakdown['discount_amount'])),
        ("Final Invoice Amount (₹)", float(breakdown['grand_total'])),
        ("Amount Received (₹)", float(breakdown['total_paid'])),
        ("Outstanding Balance (₹)", float(breakdown['pending_amount'])),
        ("Payment Status", breakdown['payment_status']),
    ]

    for label, val in summary_data:
        ws.merge_cells(start_row=cur_row, start_column=4, end_row=cur_row, end_column=5)
        lbl_cell = ws.cell(row=cur_row, column=4, value=label)
        lbl_cell.alignment = Alignment(horizontal='right')
        lbl_cell.font = grand_total_font if "Final" in label or "Outstanding" in label else summary_label_font

        val_cell = ws.cell(row=cur_row, column=6, value=val)
        val_cell.alignment = Alignment(horizontal='right')
        if isinstance(val, (int, float)):
            val_cell.number_format = '#,##0.00'
            val_cell.font = grand_total_font if "Final" in label or "Outstanding" in label else summary_label_font
        else:
            val_cell.font = summary_label_font

        lbl_cell.border = border_double_bottom if "Outstanding" in label else border_thin
        val_cell.border = border_double_bottom if "Outstanding" in label else border_thin
        ws.cell(row=cur_row, column=5).border = border_double_bottom if "Outstanding" in label else border_thin

        cur_row += 1

    # Adjust column widths
    for col in ws.columns:
        max_len = 0
        for cell in col:
            val_str = str(cell.value or '')
            if len(val_str) > max_len and not cell.coordinate in ws.merged_cells:
                max_len = len(val_str)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 4, 14)

    output = BytesIO()
    wb.save(output)
    return output.getvalue()

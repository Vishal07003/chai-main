from io import BytesIO
from decimal import Decimal
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from django.utils import timezone
from .utils import KOLKATA_TZ

def generate_report_excel(report_type: str, data: dict) -> bytes:
    """
    Renders professional, formatted Excel (.xlsx) workbook for any Chai Flow report.
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{report_type.replace('_', ' ').replace('-', ' ').title()[:30]}"

    # Color definitions
    header_fill = PatternFill(start_color="8C331A", end_color="8C331A", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=14, bold=True, color="8C331A")
    meta_font = Font(name="Calibri", size=10, italic=True, color="554A42")
    summary_fill = PatternFill(start_color="FAF6F2", end_color="FAF6F2", fill_type="solid")
    summary_bold = Font(name="Calibri", size=10, bold=True, color="2C201A")
    border_thin = Border(
        left=Side(style='thin', color='EFEAE4'),
        right=Side(style='thin', color='EFEAE4'),
        top=Side(style='thin', color='EFEAE4'),
        bottom=Side(style='thin', color='EFEAE4')
    )

    # 1. Title & Meta
    report_title = f"Chai Flow — {report_type.replace('_', ' ').replace('-', ' ').title()} Report"
    date_label = data.get('date_filter', {}).get('label', '')
    now_str = timezone.now().astimezone(KOLKATA_TZ).strftime('%d %b %Y, %I:%M %p')

    ws['A1'] = report_title
    ws['A1'].font = title_font
    ws['A2'] = f"Filter Period: {date_label}  |  Generated At: {now_str}"
    ws['A2'].font = meta_font

    current_row = 4

    # 2. Summary KPI Metrics
    if report_type == 'overview':
        summary = {
            'net_sales': data.get('net_sales', '0'),
            'invoices_count': data.get('invoices_count', 0),
            'total_collections': data.get('total_collections', '0'),
            'total_outstanding': data.get('total_outstanding', '0'),
            'overdue_outstanding': data.get('overdue_outstanding', '0'),
            'defaulters_count': data.get('defaulters_count', 0),
            'gross_profit': data.get('gross_profit', '0'),
            'operating_expenses': data.get('operating_expenses', '0'),
            'net_profit': data.get('net_profit', '0'),
            'profit_margin_pct': f"{data.get('profit_margin_pct', '0')}%",
            'cost_coverage': data.get('cost_coverage_status', 'N/A').title(),
        }
    else:
        summary = data.get('summary', {})

    if summary:
        ws.cell(row=current_row, column=1, value="Summary Metrics").font = Font(name="Calibri", size=11, bold=True, color="8C331A")
        current_row += 1

        for k, v in summary.items():
            if isinstance(v, (str, int, float, Decimal)):
                c1 = ws.cell(row=current_row, column=1, value=k.replace('_', ' ').title())
                c2 = ws.cell(row=current_row, column=2, value=str(v))
                c1.fill = summary_fill
                c2.fill = summary_fill
                c1.font = summary_bold
                c1.border = border_thin
                c2.border = border_thin
                current_row += 1

        current_row += 1

    # 3. Main Data Table
    headers = []
    rows = []

    normalized_type = report_type.lower().replace('_', '-')

    if normalized_type == 'overview':
        headers = ["Metric Category", "Key Indicator", "Amount / Count", "Business Context"]
        ov_metrics = [
            ["Sales & Revenue", "Net Realized Sales", f"₹{data.get('net_sales', '0')}", f"{data.get('invoices_count', 0)} finalized invoices"],
            ["Cash Collections", "Total Payments Received", f"₹{data.get('total_collections', '0')}", "All cash, UPI and bank allocations"],
            ["Receivables", "Total Outstanding Dues", f"₹{data.get('total_outstanding', '0')}", f"Overdue: ₹{data.get('overdue_outstanding', '0')} ({data.get('defaulters_count', 0)} defaulters)"],
            ["Profitability", "Gross Profit", f"₹{data.get('gross_profit', '0')}", f"Operating Expenses: ₹{data.get('operating_expenses', '0')}"],
            ["Net Performance", "Net Profit (P&L)", f"₹{data.get('net_profit', '0')}", f"Net Margin: {data.get('profit_margin_pct', '0')}% ({data.get('cost_coverage_status', 'N/A').title()} cost coverage)"],
        ]
        for row in ov_metrics:
            rows.append(row)

    elif normalized_type == 'sales' and 'invoices' in data:
        headers = ["Invoice #", "Date", "Customer Name", "Customer Phone", "Billing Source", "Status", "Subtotal (₹)", "Discount (₹)", "Grand Total (₹)"]
        for inv in data['invoices']:
            rows.append([
                inv['invoice_number'], inv['created_at'], inv['customer_name'], inv['customer_phone'],
                inv['source'], inv['status'].upper(), float(inv['subtotal']), float(inv['discount_amount']), float(inv['grand_total'])
            ])

    elif normalized_type == 'collections' and 'payments' in data:
        headers = ["Payment ID", "Received At", "Customer / Account", "Method", "Reference Number", "Is Advance Credit?", "Advance Balance (₹)", "Amount Collected (₹)"]
        for p in data['payments']:
            rows.append([
                p['id'], p['received_at'], p['customer_name'], p['method'], p['reference_number'] or '-',
                "Yes" if p['is_advance'] else "No", float(p['unallocated_advance']), float(p['amount'])
            ])

    elif normalized_type == 'outstanding' and 'invoices' in data:
        headers = ["Invoice #", "Date", "Customer Name", "Customer Phone", "Due Date", "Is Overdue?", "Days Overdue", "Total Billed (₹)", "Amount Paid (₹)", "Pending Due (₹)"]
        for inv in data['invoices']:
            rows.append([
                inv['invoice_number'], inv['created_at'], inv['customer_name'], inv['customer_phone'],
                inv['due_date'] or 'Not Set', "Yes" if inv['is_overdue'] else "No", inv['days_overdue'] if inv['is_overdue'] else 0,
                float(inv['grand_total']), float(inv['total_paid']), float(inv['pending_amount'])
            ])

    elif normalized_type == 'products' and 'products' in data:
        headers = ["Product ID", "Product Name", "Units Sold", "Sales Value (₹)", "Total Cost (₹)", "Gross Margin (₹)", "Margin %", "Cost Available"]
        for prod in data['products']:
            rows.append([
                prod['product_id'], prod['product_name'], prod['units_sold'], float(prod['sales_value']),
                float(prod['cost_value']) if prod['cost_available'] else "Unavailable",
                float(prod['gross_margin']) if prod['cost_available'] else "-",
                f"{prod['margin_pct']}%" if prod['margin_pct'] is not None else "-",
                "Yes" if prod['cost_available'] else "No"
            ])

    elif normalized_type in ['profit-loss', 'profit_loss'] and 'expense_categories' in data:
        headers = ["Expense Category", "Category Key", "Transactions Count", "Total Amount (₹)"]
        for cat in data['expense_categories']:
            rows.append([
                cat['category_display'], cat['category'], cat['count'], float(cat['total'])
            ])

    elif normalized_type == 'deliveries' and 'partners' in data:
        headers = ["Delivery Partner", "Total Assigned", "Delivered", "Failed", "Completion Rate %"]
        for prt in data['partners']:
            total = prt['total_assigned']
            deliv = prt['delivered']
            rate = round((deliv / total * 100), 1) if total > 0 else 0.0
            rows.append([
                prt['partner_name'], total, deliv, prt['failed'], f"{rate}%"
            ])

    elif normalized_type == 'discounts' and 'invoices' in data:
        headers = ["Invoice #", "Date", "Customer Name", "Subtotal (₹)", "Discount Amount (₹)", "Grand Total (₹)", "Discount %"]
        for inv in data['invoices']:
            rows.append([
                inv['invoice_number'], inv['created_at'], inv['customer_name'],
                float(inv['subtotal']), float(inv['discount_amount']), float(inv['grand_total']), f"{inv['discount_pct']}%"
            ])

    elif normalized_type == 'customers' and 'customers' in data:
        headers = ["Customer ID", "Customer Name", "Phone", "Period Sales (₹)", "Period Collections (₹)", "Current Outstanding (₹)", "Invoices Count", "Last Purchase Date"]
        for c in data['customers']:
            rows.append([
                c['customer_id'], c['customer_name'], c['customer_phone'],
                float(c['period_sales']), float(c['period_collections']), float(c['current_outstanding']),
                c['period_invoices_count'], c['last_purchase_date'] or '-'
            ])

    # Write headers
    if headers:
        for col_idx, h in enumerate(headers, start=1):
            cell = ws.cell(row=current_row, column=col_idx, value=h)
            cell.fill = header_fill
            cell.font = header_font
            cell.alignment = Alignment(horizontal='center' if 'Date' in h or 'ID' in h or '#' in h else 'left')
        current_row += 1

        # Write data rows
        for row_data in rows:
            for col_idx, val in enumerate(row_data, start=1):
                cell = ws.cell(row=current_row, column=col_idx, value=val)
                cell.border = border_thin
                cell.font = Font(name="Calibri", size=10)
                if isinstance(val, float):
                    cell.number_format = '#,##0.00'
            current_row += 1

    # Auto-fit column widths
    for col in ws.columns:
        max_len = max(len(str(cell.value or '')) for cell in col)
        col_letter = get_column_letter(col[0].column)
        ws.column_dimensions[col_letter].width = max(max_len + 3, 12)

    buffer = BytesIO()
    wb.save(buffer)
    return buffer.getvalue()

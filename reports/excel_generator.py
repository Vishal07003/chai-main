from io import BytesIO
from decimal import Decimal
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from django.utils import timezone
from .utils import KOLKATA_TZ

def generate_report_excel(report_type: str, data: dict) -> bytes:
    """
    Renders professional, formatted Excel (.xlsx) workbook for any Chai Flow report
    or multi-sheet Unified Business Report (Phase R5).
    """
    normalized_type = report_type.lower().replace('_', '-')

    # Style definitions
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

    date_label = data.get('date_filter', {}).get('label', '')
    now_str = timezone.now().astimezone(KOLKATA_TZ).strftime('%d %b %Y, %I:%M %p')

    # UNIFIED BUSINESS REPORT (Phase R5): Multi-sheet Workbook
    if normalized_type in ['unified', 'unified-report', 'business-report']:
        wb = openpyxl.Workbook()
        # Remove default sheet
        wb.remove(wb.active)

        def add_sheet_with_table(sheet_title, report_title, headers, rows):
            ws = wb.create_sheet(title=sheet_title[:31])
            ws['A1'] = f"Chai Flow — {report_title}"
            ws['A1'].font = title_font
            ws['A2'] = f"Period: {date_label}  |  Generated At: {now_str}"
            ws['A2'].font = meta_font

            cur_row = 4
            for col_idx, h in enumerate(headers, start=1):
                cell = ws.cell(row=cur_row, column=col_idx, value=h)
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = Alignment(horizontal='center' if any(s in h for s in ['Date', 'ID', '#', 'Status']) else 'left')

            cur_row += 1
            for row_data in rows:
                for col_idx, val in enumerate(row_data, start=1):
                    cell = ws.cell(row=cur_row, column=col_idx, value=val)
                    cell.border = border_thin
                    cell.font = Font(name="Calibri", size=10)
                    if isinstance(val, (float, Decimal)):
                        cell.number_format = '#,##0.00'
                cur_row += 1

            for col in ws.columns:
                max_len = max(len(str(cell.value or '')) for cell in col)
                col_letter = get_column_letter(col[0].column)
                ws.column_dimensions[col_letter].width = max(max_len + 3, 12)
            return ws

        summary = data.get('summary', {})

        # 1. Summary Worksheet
        ws_sum = wb.create_sheet(title="Summary")
        ws_sum['A1'] = "Chai Flow — Business Summary & Executive KPIs"
        ws_sum['A1'].font = title_font
        ws_sum['A2'] = f"Period: {date_label}  |  Generated At: {now_str}"
        ws_sum['A2'].font = meta_font

        ws_sum.cell(row=4, column=1, value="Metric Category").fill = header_fill
        ws_sum.cell(row=4, column=1).font = header_font
        ws_sum.cell(row=4, column=2, value="Key Indicator").fill = header_fill
        ws_sum.cell(row=4, column=2).font = header_font
        ws_sum.cell(row=4, column=3, value="Amount / Value").fill = header_fill
        ws_sum.cell(row=4, column=3).font = header_font
        ws_sum.cell(row=4, column=4, value="Business Context").fill = header_fill
        ws_sum.cell(row=4, column=4).font = header_font

        kpi_list = [
            ["Sales & Revenue", "Gross Sales / Subtotal", float(summary.get('gross_sales', 0)), "Total order/bill value before discount"],
            ["Sales & Revenue", "Total Discounts", float(summary.get('total_discounts', 0)), "Discounts granted on invoices"],
            ["Sales & Revenue", "Net Realized Sales", float(summary.get('net_sales', 0)), f"Across {summary.get('total_invoices', 0)} finalized invoices"],
            ["Cash Collections", "Total Collections Received", float(summary.get('total_collections', 0)), f"Across {summary.get('total_payment_transactions', 0)} payments"],
            ["Receivables", "Portfolio Outstanding", float(summary.get('portfolio_outstanding', 0)), "Current unpaid dues portfolio"],
            ["Receivables", "Overdue Outstanding", float(summary.get('overdue_outstanding', 0)), "Invoices past configured due date"],
            ["Advances", "Customer Advance Credits", float(summary.get('total_advance_credits', 0)), "Unallocated customer balance credits"],
            ["Operations", "Total Orders", summary.get('total_orders', 0), "Orders in period"],
            ["Operations", "Completed Deliveries", summary.get('completed_deliveries', 0), "Successfully delivered runs"],
            ["Expenses & Cost", "Operating Expenses", float(summary.get('operating_expenses', 0)), "Rent, utilities, transport, staff, etc."],
            ["Expenses & Cost", "COGS (Captured)", float(summary.get('cogs', 0)), f"Cost coverage: {summary.get('cost_coverage_status', 'N/A').title()}"],
            ["Profitability", "Gross Profit", float(summary.get('gross_profit', 0)), "Net Sales minus Captured COGS"],
            ["Profitability", "Net Profit", float(summary.get('net_profit', 0)), f"Net Margin: {summary.get('profit_margin_pct', 0)}%"],
        ]
        r_idx = 5
        for row in kpi_list:
            for c_idx, val in enumerate(row, start=1):
                c = ws_sum.cell(row=r_idx, column=c_idx, value=val)
                c.border = border_thin
                c.font = Font(name="Calibri", size=10)
                if isinstance(val, float):
                    c.number_format = '#,##0.00'
            r_idx += 1

        for col in ws_sum.columns:
            max_len = max(len(str(cell.value or '')) for cell in col)
            col_letter = get_column_letter(col[0].column)
            ws_sum.column_dimensions[col_letter].width = max(max_len + 3, 14)

        # 2. Sales & Invoices
        sales_invoices = data.get('sales_invoices', [])
        s_headers = ["Invoice #", "Date", "Customer Name", "Customer Phone", "Billing Source", "Status", "Gross Subtotal (₹)", "Discount (₹)", "Net Amount (₹)", "Amount Paid (₹)", "Pending Due (₹)", "Due Date"]
        s_rows = [
            [
                inv['invoice_number'], inv['invoice_date'], inv['customer_name'], inv['customer_phone'],
                inv['source'], inv['status'].upper(), float(inv['gross_amount']), float(inv['discount_amount']),
                float(inv['net_invoice_amount']), float(inv['amount_paid']), float(inv['pending_amount']),
                inv['due_date'] or 'Not Set'
            ]
            for inv in sales_invoices
        ]
        add_sheet_with_table("Sales & Invoices", "Sales & Invoice Details", s_headers, s_rows)

        # 3. Collections
        coll_txns = data.get('collection_transactions', [])
        c_headers = ["Payment ID", "Received Date", "Customer / Account", "Method", "Reference Number", "Amount Received (₹)", "Allocated Amount (₹)", "Unallocated Advance (₹)", "Allocated Invoices"]
        c_rows = [
            [
                p['id'], p['payment_date'], p['customer_name'], p['method'], p['reference_number'] or '-',
                float(p['amount_received']), float(p['allocated_amount']), float(p['unallocated_advance']),
                p['allocated_invoices']
            ]
            for p in coll_txns
        ]
        add_sheet_with_table("Collections", "Collection & Payment Transactions", c_headers, c_rows)

        # 4. Outstanding
        cust_dues = data.get('customer_outstanding', [])
        o_headers = ["Customer Name", "Phone", "Total Billed (₹)", "Total Collected (₹)", "Current Outstanding (₹)", "Overdue Balance (₹)", "Advance Credit (₹)", "Last Payment Date"]
        o_rows = [
            [
                c['customer_name'], c['phone'], float(c['total_billed']), float(c['total_collected']),
                float(c['current_outstanding']), float(c['overdue_balance']), float(c['advance_credit']),
                c['last_payment_date']
            ]
            for c in cust_dues
        ]
        add_sheet_with_table("Outstanding", "Customer-wise Outstanding (As of Today)", o_headers, o_rows)

        # 5. Customer Ledger (Payment History)
        pay_hist = data.get('customer_payment_history', [])
        l_headers = ["Customer", "Date & Time", "Payment Amount (₹)", "Method", "Reference", "Invoice Allocation", "Remaining Invoice Balance (₹)"]
        l_rows = [
            [
                h['customer_name'], h['payment_date'], float(h['payment_amount']), h['payment_method'],
                h['reference'], h['invoice_allocation'], float(h['remaining_balance'])
            ]
            for h in pay_hist
        ]
        add_sheet_with_table("Customer Ledger", "Customer Payment History & Allocations", l_headers, l_rows)

        # 6. Discounts & Expenses
        exp_items = data.get('expenses', {}).get('items', [])
        e_headers = ["Expense ID", "Expense Title", "Category", "Date", "Payment Method", "Amount (₹)"]
        e_rows = [
            [
                e['id'], e['title'], e['category_display'], e['expense_date'], e['payment_method'], float(e['amount'])
            ]
            for e in exp_items
        ]
        add_sheet_with_table("Discounts & Expenses", "Operating Expenses & Cost Details", e_headers, e_rows)

        # 7. Deliveries
        deliv_partners = data.get('deliveries', {}).get('partners', [])
        d_headers = ["Delivery Partner", "Total Assigned", "Delivered", "Failed", "Completion Rate %"]
        d_rows = [
            [
                prt['partner_name'], prt['assigned'], prt['delivered'], prt['failed'],
                round((prt['delivered']/prt['assigned']*100), 1) if prt['assigned'] > 0 else 0.0
            ]
            for prt in deliv_partners
        ]
        add_sheet_with_table("Deliveries", "Delivery Fulfillment & Partner Performance", d_headers, d_rows)

        # 8. Profit & Loss
        pnl = data.get('profit_loss', {})
        pnl_headers = ["P&L Component", "Amount (₹)", "Notes / Coverage"]
        pnl_rows = [
            ["Net Realized Sales", float(pnl.get('net_sales', 0)), "Finalized non-void invoices"],
            ["Less: Cost of Goods Sold (COGS)", -float(pnl.get('cogs', 0)), f"Coverage: {pnl.get('cost_coverage_status', 'N/A').title()}"],
            ["Gross Profit", float(pnl.get('gross_profit', 0)), "Net Sales minus Captured COGS"],
            ["Less: Operating Expenses", -float(pnl.get('operating_expenses', 0)), "Operational overheads (Rent, utilities, staff)"],
            ["Net Profit", float(pnl.get('net_profit', 0)), f"Net Profit Margin: {pnl.get('profit_margin_pct', 0)}%"],
        ]
        add_sheet_with_table("Profit & Loss", "Profit & Loss Statement", pnl_headers, pnl_rows)

        buffer = BytesIO()
        wb.save(buffer)
        return buffer.getvalue()

    # LEGACY / INDIVIDUAL REPORT GENERATION FALLBACK
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

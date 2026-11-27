from io import BytesIO
from decimal import Decimal
import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter


def generate_customer_ledger_excel(customer, period_data: dict) -> bytes:
    """
    Renders structured, styled Excel (.xlsx) workbook for Customer Ledger Statement.
    Tabs:
      1. Ledger Summary
      2. Invoices & Breakdown
      3. Payments & Collections
    """
    wb = openpyxl.Workbook()

    # Themes
    brand_fill = PatternFill(start_color="8C331A", end_color="8C331A", fill_type="solid")
    brand_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    title_font = Font(name="Calibri", size=14, bold=True, color="8C331A")
    sub_title_font = Font(name="Calibri", size=10, italic=True, color="554A42")
    header_fill = PatternFill(start_color="FDF2EC", end_color="FDF2EC", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="8C331A")
    bold_font = Font(name="Calibri", size=10, bold=True, color="2C201A")
    regular_font = Font(name="Calibri", size=10, color="2C201A")

    border_thin = Border(
        left=Side(style='thin', color='EFEAE4'),
        right=Side(style='thin', color='EFEAE4'),
        top=Side(style='thin', color='EFEAE4'),
        bottom=Side(style='thin', color='EFEAE4')
    )

    # -------------------------------------------------------------
    # Sheet 1: Ledger Statement & Summary
    # -------------------------------------------------------------
    ws1 = wb.active
    ws1.title = "Ledger Summary"

    ws1['A1'] = "Chai Flow — Customer Ledger Statement"
    ws1['A1'].font = title_font

    period_lbl = period_data.get('period_label', 'Billing Period')
    ws1['A2'] = f"Customer: {customer.name} ({customer.phone or 'N/A'})   |   Period: {period_lbl}"
    ws1['A2'].font = sub_title_font

    ws1['A4'] = "Financial Summary"
    ws1['A4'].font = header_font
    ws1['A4'].fill = header_fill

    summary_items = [
        ("Opening Balance (Prior Periods)", float(period_data.get('opening_balance', 0))),
        ("Total Invoiced (This Period)", float(period_data.get('total_invoiced', 0))),
        ("Total Collections Received", float(period_data.get('total_collections', 0))),
        ("Outstanding Balance Due", float(period_data.get('outstanding_balance', 0))),
        ("Available Credit Balance", float(period_data.get('available_credit', 0))),
        ("Pending Refund Due", float(period_data.get('refund_due', 0))),
    ]

    cur_row = 5
    for label, val in summary_items:
        ws1.cell(row=cur_row, column=1, value=label).font = bold_font
        c = ws1.cell(row=cur_row, column=2, value=val)
        c.font = bold_font
        c.number_format = '₹#,##0.00'
        c.alignment = Alignment(horizontal='right')
        ws1.cell(row=cur_row, column=1).border = border_thin
        ws1.cell(row=cur_row, column=2).border = border_thin
        cur_row += 1

    # -------------------------------------------------------------
    # Sheet 2: Invoices
    # -------------------------------------------------------------
    ws2 = wb.create_sheet(title="Invoices")
    ws2['A1'] = f"Invoices Recorded for {customer.name} ({period_lbl})"
    ws2['A1'].font = title_font

    inv_headers = ["Invoice Date", "Invoice #", "Billing Type", "Grand Total (₹)", "Received (₹)", "Remaining (₹)", "Status"]
    for col_idx, h in enumerate(inv_headers, start=1):
        cell = ws2.cell(row=3, column=col_idx, value=h)
        cell.fill = brand_fill
        cell.font = brand_font
        cell.alignment = Alignment(horizontal='center' if col_idx in [1, 2, 3, 7] else 'right')

    cur_row = 4
    for inv in period_data.get('invoices', []):
        ws2.cell(row=cur_row, column=1, value=inv.get('date', '')).alignment = Alignment(horizontal='center')
        ws2.cell(row=cur_row, column=2, value=inv.get('invoice_number', '')).alignment = Alignment(horizontal='center')
        ws2.cell(row=cur_row, column=3, value=(inv.get('billing_type') or 'daily').capitalize()).alignment = Alignment(horizontal='center')

        c_tot = ws2.cell(row=cur_row, column=4, value=float(inv.get('total_bill', 0)))
        c_tot.number_format = '₹#,##0.00'
        c_rec = ws2.cell(row=cur_row, column=5, value=float(inv.get('amount_received', 0)))
        c_rec.number_format = '₹#,##0.00'
        c_rem = ws2.cell(row=cur_row, column=6, value=float(inv.get('remaining_balance', 0)))
        c_rem.number_format = '₹#,##0.00'

        ws2.cell(row=cur_row, column=7, value=inv.get('status', 'PENDING')).alignment = Alignment(horizontal='center')

        for ci in range(1, len(inv_headers) + 1):
            ws2.cell(row=cur_row, column=ci).border = border_thin
        cur_row += 1

    # -------------------------------------------------------------
    # Sheet 3: Payments
    # -------------------------------------------------------------
    ws3 = wb.create_sheet(title="Payments & Collections")
    ws3['A1'] = f"Payments & Collections for {customer.name} ({period_lbl})"
    ws3['A1'].font = title_font

    pm_headers = ["Date & Time", "Payment ID", "Method", "Amount (₹)", "Allocated (₹)", "Is Reversed", "Reversal Reason / Notes"]
    for col_idx, h in enumerate(pm_headers, start=1):
        cell = ws3.cell(row=3, column=col_idx, value=h)
        cell.fill = brand_fill
        cell.font = brand_font
        cell.alignment = Alignment(horizontal='center' if col_idx in [1, 2, 3, 6] else ('right' if '₹' in h else 'left'))

    cur_row = 4
    for p in period_data.get('payments', []):
        d_time = p.get('received_at', '')[:16].replace('T', ' ')
        ws3.cell(row=cur_row, column=1, value=d_time).alignment = Alignment(horizontal='center')
        ws3.cell(row=cur_row, column=2, value=f"PAY-{p.get('payment_id', '')}").alignment = Alignment(horizontal='center')
        ws3.cell(row=cur_row, column=3, value=(p.get('method') or 'cash').upper()).alignment = Alignment(horizontal='center')

        c_amt = ws3.cell(row=cur_row, column=4, value=float(p.get('amount', 0)))
        c_amt.number_format = '₹#,##0.00'
        c_alloc = ws3.cell(row=cur_row, column=5, value=float(p.get('total_allocated', 0)))
        c_alloc.number_format = '₹#,##0.00'

        is_rev = "YES (Reversed)" if p.get('is_reversed') else "No"
        ws3.cell(row=cur_row, column=6, value=is_rev).alignment = Alignment(horizontal='center')

        notes = p.get('reversal_reason') if p.get('is_reversed') else p.get('notes')
        ws3.cell(row=cur_row, column=7, value=notes or '—')

        for ci in range(1, len(pm_headers) + 1):
            ws3.cell(row=cur_row, column=ci).border = border_thin
        cur_row += 1

    # Auto-fit column widths across sheets
    for ws in [ws1, ws2, ws3]:
        for col in ws.columns:
            max_len = 0
            col_letter = get_column_letter(col[0].column)
            for cell in col:
                val_str = str(cell.value or '')
                if '\n' in val_str:
                    val_str = max(val_str.split('\n'), key=len)
                if len(val_str) > max_len:
                    max_len = len(val_str)
            ws.column_dimensions[col_letter].width = max(max_len + 3, 14)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()

import os
from io import BytesIO
from decimal import Decimal
import datetime

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    KeepTogether,
    PageBreak,
)
from reportlab.pdfgen import canvas
from django.utils import timezone
from .utils import KOLKATA_TZ

_REPORT_FONT_INITIALIZED = False
_REPORT_FONT_NAME = "Helvetica"
_REPORT_FONT_BOLD = "Helvetica-Bold"
_REPORT_CURRENCY_SYMBOL = "₹"

def _init_report_fonts():
    global _REPORT_FONT_INITIALIZED, _REPORT_FONT_NAME, _REPORT_FONT_BOLD, _REPORT_CURRENCY_SYMBOL
    if _REPORT_FONT_INITIALIZED:
        return

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    candidates = [
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
        (r"C:\Windows\Fonts\calibri.ttf", r"C:\Windows\Fonts\calibrib.ttf"),
    ]

    for reg_path, bold_path in candidates:
        if os.path.exists(reg_path) and os.path.exists(bold_path):
            try:
                pdfmetrics.registerFont(TTFont("ReportUnicodeFont", reg_path))
                pdfmetrics.registerFont(TTFont("ReportUnicodeFont-Bold", bold_path))
                _REPORT_FONT_NAME = "ReportUnicodeFont"
                _REPORT_FONT_BOLD = "ReportUnicodeFont-Bold"
                _REPORT_CURRENCY_SYMBOL = "₹"
                _REPORT_FONT_INITIALIZED = True
                return
            except Exception:
                pass

    _REPORT_FONT_NAME = "Helvetica"
    _REPORT_FONT_BOLD = "Helvetica-Bold"
    _REPORT_CURRENCY_SYMBOL = "Rs. "
    _REPORT_FONT_INITIALIZED = True


class ReportNumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved_page_states = []

    def showPage(self):
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_decorations(num_pages)
            super().showPage()
        super().save()

    def draw_page_decorations(self, page_count):
        self.saveState()
        self.setFont("Helvetica", 8)
        self.setFillColor(colors.HexColor("#8E8278"))
        self.setStrokeColor(colors.HexColor("#EFEAE4"))
        self.setLineWidth(0.75)

        # Footer divider line
        self.line(15 * mm, 14 * mm, 282 * mm if self._pagesize == landscape(A4) else 195 * mm, 14 * mm)

        # Brand note
        self.drawString(15 * mm, 9 * mm, "Chai Flow • Confidential Financial & Business Report")
        page_str = f"Page {self._pageNumber} of {page_count}"
        right_margin = 282 * mm if self._pagesize == landscape(A4) else 195 * mm
        self.drawRightString(right_margin, 9 * mm, page_str)
        self.restoreState()


def generate_report_pdf(report_type: str, data: dict) -> bytes:
    """
    Renders clean, branded A4 landscape PDF for any Chai Flow report or Unified Business Report.
    """
    _init_report_fonts()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        'RepTitle',
        parent=styles['Normal'],
        fontName=_REPORT_FONT_BOLD,
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#8C331A'),
    )
    meta_style = ParagraphStyle(
        'RepMeta',
        parent=styles['Normal'],
        fontName=_REPORT_FONT_NAME,
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#6B5E55'),
    )
    sec_title_style = ParagraphStyle(
        'RepSecTitle',
        parent=styles['Normal'],
        fontName=_REPORT_FONT_BOLD,
        fontSize=13,
        leading=17,
        textColor=colors.HexColor('#8C331A'),
        spaceBefore=8,
        spaceAfter=4,
    )
    cell_head_style = ParagraphStyle(
        'CellHead',
        parent=styles['Normal'],
        fontName=_REPORT_FONT_BOLD,
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#FFFFFF'),
    )
    cell_body_style = ParagraphStyle(
        'CellBody',
        parent=styles['Normal'],
        fontName=_REPORT_FONT_NAME,
        fontSize=8,
        leading=11,
        textColor=colors.HexColor('#2C201A'),
    )

    sym = _REPORT_CURRENCY_SYMBOL
    elements = []
    now_str = timezone.now().astimezone(KOLKATA_TZ).strftime('%d %b %Y, %I:%M %p')
    date_label = data.get('date_filter', {}).get('label', '')

    normalized_type = report_type.lower().replace('_', '-')

    # UNIFIED BUSINESS REPORT GENERATION (Phase R5)
    if normalized_type in ['unified', 'unified-report', 'business-report']:
        # 1. Title Header Table
        header_data = [
            [
                Paragraph("<b>Chai Flow</b> — Unified Business Report", title_style),
                Paragraph(f"<b>Reporting Period:</b> {date_label}<br/><b>Generated:</b> {now_str}", meta_style)
            ]
        ]
        header_table = Table(header_data, colWidths=[180 * mm, 87 * mm])
        header_table.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(header_table)
        elements.append(Spacer(1, 4 * mm))

        summary = data.get('summary', {})

        # A. Business Summary Deck
        elements.append(Paragraph("1. BUSINESS SUMMARY & EXECUTIVE KPIS", sec_title_style))
        kpi_metrics = [
            ("Gross Sales / Subtotal", f"{sym}{summary.get('gross_sales', '0')}"),
            ("Total Discounts", f"{sym}{summary.get('total_discounts', '0')}"),
            ("Net Realized Sales", f"{sym}{summary.get('net_sales', '0')}"),
            ("Total Collections", f"{sym}{summary.get('total_collections', '0')}"),
            ("Portfolio Outstanding", f"{sym}{summary.get('portfolio_outstanding', '0')}"),
            ("Overdue Outstanding", f"{sym}{summary.get('overdue_outstanding', '0')}"),
            ("Customer Advance Credits", f"{sym}{summary.get('total_advance_credits', '0')}"),
            ("Total Invoices", str(summary.get('total_invoices', 0))),
            ("Payment Transactions", str(summary.get('total_payment_transactions', 0))),
            ("Total Orders", str(summary.get('total_orders', 0))),
            ("Completed Deliveries", str(summary.get('completed_deliveries', 0))),
            ("Operating Expenses", f"{sym}{summary.get('operating_expenses', '0')}"),
            ("COGS (Captured)", f"{sym}{summary.get('cogs', '0')}"),
            ("Gross Profit", f"{sym}{summary.get('gross_profit', '0')}"),
            ("Net Profit", f"{sym}{summary.get('net_profit', '0')} ({summary.get('profit_margin_pct', 0)}%)"),
            ("Cost Coverage Status", summary.get('cost_coverage_status', 'N/A').upper()),
        ]
        kpi_cells = [Paragraph(f"<b>{k}</b><br/>{v}", meta_style) for k, v in kpi_metrics]
        kpi_rows = [kpi_cells[i:i+4] for i in range(0, len(kpi_cells), 4)]
        kpi_table = Table(kpi_rows, colWidths=[66.75 * mm] * 4)
        kpi_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FAF6F2')),
            ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#EFEAE4')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#EFEAE4')),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(kpi_table)
        elements.append(Spacer(1, 6 * mm))

        # Helper table generator
        def build_styled_table(headers, rows, widths):
            tbl_data = [[Paragraph(h, cell_head_style) for h in headers]]
            for r in rows:
                tbl_data.append([Paragraph(str(c), cell_body_style) for c in r])
            tbl = Table(tbl_data, colWidths=widths, repeatRows=1)
            tbl.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8C331A')),
                ('ALIGN', (0, 0), (-1, 0), 'LEFT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#FFFFFF'), colors.HexColor('#FDFBF7')]),
                ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#EFEAE4')),
                ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#EFEAE4')),
                ('TOPPADDING', (0, 0), (-1, -1), 4),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                ('LEFTPADDING', (0, 0), (-1, -1), 5),
                ('RIGHTPADDING', (0, 0), (-1, -1), 5),
            ]))
            return tbl

        # B. Sales & Invoice Details
        elements.append(Paragraph("2. SALES & INVOICE DETAILS", sec_title_style))
        sales_invs = data.get('sales_invoices', [])
        if sales_invs:
            s_headers = ["Invoice #", "Date", "Customer Name", "Source", f"Gross ({sym.strip()})", f"Disc ({sym.strip()})", f"Net ({sym.strip()})", f"Paid ({sym.strip()})", f"Due ({sym.strip()})", "Status"]
            s_rows = [
                [
                    inv['invoice_number'], inv['invoice_date'], inv['customer_name'], inv['source'],
                    inv['gross_amount'], inv['discount_amount'], inv['net_invoice_amount'],
                    inv['amount_paid'], inv['pending_amount'], inv['status'].upper()
                ]
                for inv in sales_invs[:200]
            ]
            elements.append(build_styled_table(s_headers, s_rows, [28*mm, 26*mm, 45*mm, 25*mm, 22*mm, 20*mm, 25*mm, 25*mm, 25*mm, 26*mm]))
        else:
            elements.append(Paragraph("<i>No finalized invoices found in this period.</i>", meta_style))
        elements.append(Spacer(1, 6 * mm))

        # C. Collection & Payment Transactions
        elements.append(Paragraph("3. COLLECTION & PAYMENT TRANSACTIONS", sec_title_style))
        coll_txns = data.get('collection_transactions', [])
        if coll_txns:
            c_headers = ["ID", "Date", "Customer / Account", f"Received ({sym.strip()})", "Method", "Reference", f"Allocated ({sym.strip()})", f"Advance ({sym.strip()})", "Invoices Applied"]
            c_rows = [
                [
                    str(p['id']), p['payment_date'], p['customer_name'], p['amount_received'],
                    p['method'], p['reference_number'], p['allocated_amount'], p['unallocated_advance'],
                    p['allocated_invoices']
                ]
                for p in coll_txns[:200]
            ]
            elements.append(build_styled_table(c_headers, c_rows, [16*mm, 26*mm, 42*mm, 25*mm, 22*mm, 25*mm, 24*mm, 24*mm, 63*mm]))
        else:
            elements.append(Paragraph("<i>No payment transactions recorded in this period.</i>", meta_style))
        elements.append(Spacer(1, 6 * mm))

        # D. Customer-wise Outstanding
        elements.append(Paragraph("4. CUSTOMER-WISE OUTSTANDING & RECEIVABLES (AS OF TODAY)", sec_title_style))
        cust_dues = data.get('customer_outstanding', [])
        if cust_dues:
            d_headers = ["Customer Name", "Phone", f"Total Billed ({sym.strip()})", f"Total Paid ({sym.strip()})", f"Current Outstanding ({sym.strip()})", f"Overdue ({sym.strip()})", f"Advance Credit ({sym.strip()})", "Last Payment"]
            d_rows = [
                [
                    c['customer_name'], c['phone'], c['total_billed'], c['total_collected'],
                    c['current_outstanding'], c['overdue_balance'], c['advance_credit'], c['last_payment_date']
                ]
                for c in cust_dues[:150]
            ]
            elements.append(build_styled_table(d_headers, d_rows, [52*mm, 28*mm, 32*mm, 32*mm, 35*mm, 30*mm, 30*mm, 28*mm]))
        else:
            elements.append(Paragraph("<i>No customer outstanding balances found.</i>", meta_style))
        elements.append(Spacer(1, 6 * mm))

        # E. Customer Payment History
        elements.append(Paragraph("5. CUSTOMER PAYMENT HISTORY & ALLOCATIONS", sec_title_style))
        pay_hist = data.get('customer_payment_history', [])
        if pay_hist:
            h_headers = ["Customer", "Date", f"Payment ({sym.strip()})", "Method", "Reference", "Invoice Allocation", f"Remaining Due ({sym.strip()})"]
            h_rows = [
                [
                    h['customer_name'], h['payment_date'], h['payment_amount'], h['payment_method'],
                    h['reference'], h['invoice_allocation'], h['remaining_balance']
                ]
                for h in pay_hist[:150]
            ]
            elements.append(build_styled_table(h_headers, h_rows, [45*mm, 30*mm, 28*mm, 26*mm, 30*mm, 68*mm, 40*mm]))
        else:
            elements.append(Paragraph("<i>No payment allocation history found in this period.</i>", meta_style))
        elements.append(Spacer(1, 6 * mm))

        # F. Discounts & Operating Expenses
        elements.append(Paragraph("6. DISCOUNTS & OPERATING EXPENSES", sec_title_style))
        disc_info = data.get('discounts', {})
        exp_info = data.get('expenses', {})
        disc_summary_para = Paragraph(f"Total Discounts Granted: <b>{sym}{disc_info.get('total_discount_amount', '0')}</b> across <b>{disc_info.get('discounted_invoices_count', 0)}</b> invoices.  |  Total Operating Expenses: <b>{sym}{exp_info.get('total_expenses', '0')}</b>", meta_style)
        elements.append(disc_summary_para)
        elements.append(Spacer(1, 3 * mm))

        exp_items = exp_info.get('items', [])
        if exp_items:
            e_headers = ["Expense Title", "Category", "Date", "Payment Method", f"Amount ({sym.strip()})"]
            e_rows = [
                [
                    e['title'], e['category_display'], e['expense_date'], e['payment_method'], e['amount']
                ]
                for e in exp_items[:150]
            ]
            elements.append(build_styled_table(e_headers, e_rows, [87*mm, 60*mm, 35*mm, 45*mm, 40*mm]))
        else:
            elements.append(Paragraph("<i>No operating expenses recorded in this period.</i>", meta_style))
        elements.append(Spacer(1, 6 * mm))

        # G. Delivery Summary
        elements.append(Paragraph("7. DELIVERY & FULFILLMENT SUMMARY", sec_title_style))
        deliv_info = data.get('deliveries', {})
        deliv_summary_para = Paragraph(f"Total Deliveries: <b>{deliv_info.get('total_deliveries', 0)}</b>  |  Delivered: <b>{deliv_info.get('delivered', 0)} ({deliv_info.get('completion_rate_pct', 0)}%)</b>  |  Pending: <b>{deliv_info.get('pending', 0)}</b>  |  In Progress: <b>{deliv_info.get('in_progress', 0)}</b>  |  Failed: <b>{deliv_info.get('failed', 0)}</b>", meta_style)
        elements.append(deliv_summary_para)
        elements.append(Spacer(1, 3 * mm))

        deliv_partners = deliv_info.get('partners', [])
        if deliv_partners:
            p_headers = ["Delivery Partner", "Total Assigned", "Delivered", "Failed", "Completion Rate %"]
            p_rows = [
                [
                    prt['partner_name'], str(prt['assigned']), str(prt['delivered']), str(prt['failed']),
                    f"{round((prt['delivered']/prt['assigned']*100), 1) if prt['assigned'] > 0 else 0.0}%"
                ]
                for prt in deliv_partners
            ]
            elements.append(build_styled_table(p_headers, p_rows, [87*mm, 45*mm, 45*mm, 45*mm, 45*mm]))

        discrepancies = deliv_info.get('discrepancies', [])
        if discrepancies:
            elements.append(Spacer(1, 3 * mm))
            elements.append(Paragraph("<b>Quantity Discrepancies Log:</b>", meta_style))
            disc_headers = ["Delivery #", "Order #", "Partner", "Product", "Ordered", "Delivered", "Notes"]
            disc_rows = [
                [
                    str(d['delivery_id']), str(d['order_id']), d['partner_name'], d['product_name'],
                    str(d['ordered_qty']), str(d['delivered_qty']), d['notes'] or '-'
                ]
                for d in discrepancies[:50]
            ]
            elements.append(build_styled_table(disc_headers, disc_rows, [25*mm, 25*mm, 45*mm, 52*mm, 25*mm, 25*mm, 70*mm]))
        elements.append(Spacer(1, 6 * mm))

        # H. Profit & Loss
        elements.append(Paragraph("8. PROFIT & LOSS STATEMENT", sec_title_style))
        pnl = data.get('profit_loss', {})
        pnl_rows = [
            ["Net Realized Sales", f"{sym}{pnl.get('net_sales', '0')}"],
            ["Less: Cost of Goods Sold (COGS)", f"-{sym}{pnl.get('cogs', '0')}"],
            ["Gross Profit", f"{sym}{pnl.get('gross_profit', '0')}"],
            ["Less: Operating Expenses", f"-{sym}{pnl.get('operating_expenses', '0')}"],
            ["Net Profit", f"{sym}{pnl.get('net_profit', '0')} ({pnl.get('profit_margin_pct', 0)}%)"],
            ["Cost Coverage Status", pnl.get('cost_coverage_status', 'N/A').upper()],
        ]
        pnl_table_data = [[Paragraph(f"<b>{r[0]}</b>", cell_body_style), Paragraph(f"<b>{r[1]}</b>", cell_body_style)] for r in pnl_rows]
        pnl_table = Table(pnl_table_data, colWidths=[160 * mm, 107 * mm])
        pnl_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FAF6F2')),
            ('BACKGROUND', (0, 2), (-1, 2), colors.HexColor('#F2EBE5')),
            ('BACKGROUND', (0, 4), (-1, 4), colors.HexColor('#E8DFD8')),
            ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#EFEAE4')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#EFEAE4')),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(pnl_table)

        doc.build(elements, canvasmaker=ReportNumberedCanvas)
        return buffer.getvalue()

    # LEGACY / SUB-REPORT FALLBACKS
    elements = []

    # 1. Header
    now_str = timezone.now().astimezone(KOLKATA_TZ).strftime('%d %b %Y, %I:%M %p')
    clean_type = report_type.replace('_', ' ').replace('-', ' ').title()
    report_title = f"{clean_type} Report" if "Report" not in clean_type else clean_type
    date_label = data.get('date_filter', {}).get('label', '')

    header_data = [
        [
            Paragraph(f"<b>Chai Flow</b> — {report_title}", title_style),
            Paragraph(f"<b>Period:</b> {date_label}<br/><b>Generated:</b> {now_str}", meta_style)
        ]
    ]
    header_table = Table(header_data, colWidths=[180 * mm, 87 * mm])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('ALIGN', (1, 0), (1, 0), 'RIGHT'),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    elements.append(header_table)
    elements.append(Spacer(1, 6 * mm))

    # 2. Key Metrics Summary Box
    # If report_type is 'overview', summary metrics are top-level keys in data
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

    summary_cells = []
    for k, v in summary.items():
        if isinstance(v, (str, int, float, Decimal)):
            clean_k = k.replace('_', ' ').title()
            val_str = f"Rs. {v}" if any(c in k for c in ['sales', 'subtotal', 'discount', 'cogs', 'profit', 'expenses', 'collections', 'outstanding']) and not str(v).endswith('%') else str(v)
            summary_cells.append(Paragraph(f"<b>{clean_k}</b><br/>{val_str}", meta_style))

    if summary_cells:
        # Group into rows of 4
        rows = [summary_cells[i:i+4] for i in range(0, len(summary_cells), 4)]
        # Pad last row
        while rows and len(rows[-1]) < 4:
            rows[-1].append(Paragraph("", meta_style))

        sum_table = Table(rows, colWidths=[66.7 * mm] * 4)
        sum_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FAF6F2')),
            ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#EFEAE4')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#EFEAE4')),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 8),
            ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ]))
        elements.append(sum_table)
        elements.append(Spacer(1, 8 * mm))

    # 3. Data Tables according to report_type
    table_rows = []
    col_widths = []

    normalized_type = report_type.lower().replace('_', '-')

    if normalized_type == 'overview':
        # Overview Executive Breakdown Table
        table_rows.append([
            Paragraph("Metric Category", cell_head_style),
            Paragraph("Key Indicator", cell_head_style),
            Paragraph("Recorded Amount / Count", cell_head_style),
            Paragraph("Business Context", cell_head_style),
        ])
        col_widths = [60 * mm, 65 * mm, 60 * mm, 82 * mm]
        ov_metrics = [
            ("Sales & Revenue", "Net Realized Sales", f"Rs. {data.get('net_sales', '0')}", f"{data.get('invoices_count', 0)} finalized invoices"),
            ("Cash Collections", "Total Payments Received", f"Rs. {data.get('total_collections', '0')}", "All cash, UPI and bank allocations"),
            ("Receivables", "Total Outstanding Dues", f"Rs. {data.get('total_outstanding', '0')}", f"Overdue: Rs. {data.get('overdue_outstanding', '0')} ({data.get('defaulters_count', 0)} defaulters)"),
            ("Profitability", "Gross Profit", f"Rs. {data.get('gross_profit', '0')}", f"Operating Expenses: Rs. {data.get('operating_expenses', '0')}"),
            ("Net Performance", "Net Profit (P&L)", f"Rs. {data.get('net_profit', '0')}", f"Net Margin: {data.get('profit_margin_pct', '0')}% ({data.get('cost_coverage_status', 'N/A').title()} cost coverage)"),
        ]
        for cat, ind, amt, ctx in ov_metrics:
            table_rows.append([
                Paragraph(f"<b>{cat}</b>", cell_body_style),
                Paragraph(ind, cell_body_style),
                Paragraph(f"<b>{amt}</b>", cell_body_style),
                Paragraph(ctx, cell_body_style),
            ])

    elif normalized_type == 'sales' and 'invoices' in data:
        table_rows.append([
            Paragraph("Invoice #", cell_head_style),
            Paragraph("Date", cell_head_style),
            Paragraph("Customer", cell_head_style),
            Paragraph("Billing Source", cell_head_style),
            Paragraph("Status", cell_head_style),
            Paragraph("Discount", cell_head_style),
            Paragraph("Grand Total", cell_head_style),
        ])
        col_widths = [40 * mm, 35 * mm, 65 * mm, 38 * mm, 26 * mm, 28 * mm, 35 * mm]
        for inv in data['invoices'][:250]:
            table_rows.append([
                Paragraph(inv['invoice_number'], cell_body_style),
                Paragraph(inv['created_at'], cell_body_style),
                Paragraph(inv['customer_name'], cell_body_style),
                Paragraph(inv['source'], cell_body_style),
                Paragraph(inv['status'].upper(), cell_body_style),
                Paragraph(f"Rs. {inv['discount_amount']}", cell_body_style),
                Paragraph(f"Rs. {inv['grand_total']}", cell_body_style),
            ])

    elif normalized_type == 'collections' and 'payments' in data:
        table_rows.append([
            Paragraph("ID", cell_head_style),
            Paragraph("Received At", cell_head_style),
            Paragraph("Customer", cell_head_style),
            Paragraph("Method", cell_head_style),
            Paragraph("Reference #", cell_head_style),
            Paragraph("Advance Credit", cell_head_style),
            Paragraph("Amount Paid", cell_head_style),
        ])
        col_widths = [20 * mm, 40 * mm, 70 * mm, 35 * mm, 40 * mm, 30 * mm, 32 * mm]
        for p in data['payments'][:250]:
            table_rows.append([
                Paragraph(str(p['id']), cell_body_style),
                Paragraph(p['received_at'], cell_body_style),
                Paragraph(p['customer_name'], cell_body_style),
                Paragraph(p['method'], cell_body_style),
                Paragraph(p['reference_number'] or '-', cell_body_style),
                Paragraph(f"Rs. {p['unallocated_advance']}" if p['is_advance'] else "-", cell_body_style),
                Paragraph(f"Rs. {p['amount']}", cell_body_style),
            ])

    elif normalized_type == 'outstanding' and 'invoices' in data:
        table_rows.append([
            Paragraph("Invoice #", cell_head_style),
            Paragraph("Date", cell_head_style),
            Paragraph("Customer", cell_head_style),
            Paragraph("Due Date", cell_head_style),
            Paragraph("Days Overdue", cell_head_style),
            Paragraph("Total Billed", cell_head_style),
            Paragraph("Pending Amount", cell_head_style),
        ])
        col_widths = [40 * mm, 30 * mm, 65 * mm, 32 * mm, 30 * mm, 35 * mm, 35 * mm]
        for inv in data['invoices'][:250]:
            table_rows.append([
                Paragraph(inv['invoice_number'], cell_body_style),
                Paragraph(inv['created_at'], cell_body_style),
                Paragraph(inv['customer_name'], cell_body_style),
                Paragraph(inv['due_date'] or 'Not Set', cell_body_style),
                Paragraph(str(inv['days_overdue']) if inv['is_overdue'] else '-', cell_body_style),
                Paragraph(f"Rs. {inv['grand_total']}", cell_body_style),
                Paragraph(f"Rs. {inv['pending_amount']}", cell_body_style),
            ])

    elif normalized_type == 'products' and 'products' in data:
        table_rows.append([
            Paragraph("Product Name", cell_head_style),
            Paragraph("Units Sold", cell_head_style),
            Paragraph("Total Sales (Rs.)", cell_head_style),
            Paragraph("Total Cost (Rs.)", cell_head_style),
            Paragraph("Gross Margin (Rs.)", cell_head_style),
            Paragraph("Margin %", cell_head_style),
            Paragraph("Cost Availability", cell_head_style),
        ])
        col_widths = [75 * mm, 28 * mm, 36 * mm, 36 * mm, 36 * mm, 26 * mm, 30 * mm]
        for prod in data['products'][:250]:
            table_rows.append([
                Paragraph(prod['product_name'], cell_body_style),
                Paragraph(str(prod['units_sold']), cell_body_style),
                Paragraph(f"Rs. {prod['sales_value']}", cell_body_style),
                Paragraph(f"Rs. {prod['cost_value']}" if prod['cost_available'] else 'Unavailable', cell_body_style),
                Paragraph(f"Rs. {prod['gross_margin']}" if prod['cost_available'] else '-', cell_body_style),
                Paragraph(f"{prod['margin_pct']}%" if prod['margin_pct'] is not None else '-', cell_body_style),
                Paragraph("Captured" if prod['cost_available'] else "Missing", cell_body_style),
            ])

    elif normalized_type in ['profit-loss', 'profit_loss'] and 'expense_categories' in data:
        table_rows.append([
            Paragraph("Expense Category", cell_head_style),
            Paragraph("Transactions Count", cell_head_style),
            Paragraph("Total Amount (Rs.)", cell_head_style),
        ])
        col_widths = [120 * mm, 50 * mm, 97 * mm]
        for cat in data['expense_categories']:
            table_rows.append([
                Paragraph(cat['category_display'], cell_body_style),
                Paragraph(str(cat['count']), cell_body_style),
                Paragraph(f"Rs. {cat['total']}", cell_body_style),
            ])

    elif normalized_type == 'customers' and 'customers' in data:
        table_rows.append([
            Paragraph("Customer Name", cell_head_style),
            Paragraph("Phone", cell_head_style),
            Paragraph("Period Sales", cell_head_style),
            Paragraph("Period Collections", cell_head_style),
            Paragraph("Current Dues", cell_head_style),
            Paragraph("Invoices Count", cell_head_style),
            Paragraph("Last Purchase", cell_head_style),
        ])
        col_widths = [55 * mm, 32 * mm, 36 * mm, 38 * mm, 36 * mm, 30 * mm, 40 * mm]
        for c in data['customers'][:250]:
            table_rows.append([
                Paragraph(c['customer_name'], cell_body_style),
                Paragraph(c['customer_phone'] or '-', cell_body_style),
                Paragraph(f"Rs. {c['period_sales']}", cell_body_style),
                Paragraph(f"Rs. {c['period_collections']}", cell_body_style),
                Paragraph(f"Rs. {c['current_outstanding']}", cell_body_style),
                Paragraph(str(c['period_invoices_count']), cell_body_style),
                Paragraph(c['last_purchase_date'] or '-', cell_body_style),
            ])

    elif normalized_type == 'deliveries' and 'partners' in data:
        table_rows.append([
            Paragraph("Delivery Partner", cell_head_style),
            Paragraph("Total Assigned", cell_head_style),
            Paragraph("Delivered", cell_head_style),
            Paragraph("Failed", cell_head_style),
            Paragraph("Completion Rate %", cell_head_style),
        ])
        col_widths = [87 * mm, 45 * mm, 45 * mm, 45 * mm, 45 * mm]
        for prt in data['partners']:
            total = prt['total_assigned']
            deliv = prt['delivered']
            rate = round((deliv / total * 100), 1) if total > 0 else 0.0
            table_rows.append([
                Paragraph(prt['partner_name'], cell_body_style),
                Paragraph(str(total), cell_body_style),
                Paragraph(str(deliv), cell_body_style),
                Paragraph(str(prt['failed']), cell_body_style),
                Paragraph(f"{rate}%", cell_body_style),
            ])

    elif normalized_type == 'discounts' and 'invoices' in data:
        table_rows.append([
            Paragraph("Invoice #", cell_head_style),
            Paragraph("Date", cell_head_style),
            Paragraph("Customer Name", cell_head_style),
            Paragraph("Subtotal", cell_head_style),
            Paragraph("Discount Amount", cell_head_style),
            Paragraph("Grand Total", cell_head_style),
            Paragraph("Discount %", cell_head_style),
        ])
        col_widths = [38 * mm, 32 * mm, 65 * mm, 35 * mm, 35 * mm, 35 * mm, 27 * mm]
        for inv in data['invoices'][:250]:
            table_rows.append([
                Paragraph(inv['invoice_number'], cell_body_style),
                Paragraph(inv['created_at'], cell_body_style),
                Paragraph(inv['customer_name'], cell_body_style),
                Paragraph(f"Rs. {inv['subtotal']}", cell_body_style),
                Paragraph(f"Rs. {inv['discount_amount']}", cell_body_style),
                Paragraph(f"Rs. {inv['grand_total']}", cell_body_style),
                Paragraph(f"{inv['discount_pct']}%", cell_body_style),
            ])

    if table_rows:
        data_table = Table(table_rows, colWidths=col_widths, repeatRows=1)
        data_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8C331A')),
            ('ALIGN', (0, 0), (-1, 0), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#FFFFFF'), colors.HexColor('#FDFBF7')]),
            ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#EFEAE4')),
            ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#EFEAE4')),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 5),
            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
        ]))
        elements.append(data_table)
    else:
        # Fallback empty state notice so PDF body is never blank
        no_data_para = Paragraph("<i>No data records found for this report period.</i>", meta_style)
        elements.append(no_data_para)

    doc.build(elements, canvasmaker=ReportNumberedCanvas)
    return buffer.getvalue()

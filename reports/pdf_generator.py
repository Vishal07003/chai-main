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
)
from reportlab.pdfgen import canvas
from django.utils import timezone
from .utils import KOLKATA_TZ


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
    Renders clean, branded A4 landscape PDF for any Chai Flow report.
    """
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
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#8C331A'),
    )
    meta_style = ParagraphStyle(
        'RepMeta',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#6B5E55'),
    )
    cell_head_style = ParagraphStyle(
        'CellHead',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#FFFFFF'),
    )
    cell_body_style = ParagraphStyle(
        'CellBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8,
        leading=11,
        textColor=colors.HexColor('#2C201A'),
    )

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

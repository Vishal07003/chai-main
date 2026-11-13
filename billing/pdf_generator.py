import os
from io import BytesIO
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
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


class NumberedCanvas(canvas.Canvas):
    """
    Two-pass canvas to dynamically compute and draw total page numbers and footers.
    """
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
        self.setFillColor(colors.HexColor("#7C6E65"))

        # Footer divider line
        self.setStrokeColor(colors.HexColor("#E5E0DA"))
        self.setLineWidth(0.5)
        self.line(15 * mm, 14 * mm, 195 * mm, 14 * mm)

        # Left footer: Brand & Thank you note
        self.drawString(
            15 * mm,
            9 * mm,
            "Chai Flow - Automated Tea & Beverage Operations | Thank you for your business!",
        )

        # Right footer: Page X of Y
        page_str = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(195 * mm, 9 * mm, page_str)

        self.restoreState()


def generate_invoice_pdf(invoice) -> bytes:
    """
    Generate professional A4 PDF for an Invoice.
    Returns PDF bytes.
    """
    buffer = BytesIO()

    # A4: 210mm x 297mm. Margins: 15mm left/right, 18mm top/bottom
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()

    # Custom typography styles
    brand_title_style = ParagraphStyle(
        'BrandTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        textColor=colors.HexColor('#7A2E19'),
    )

    brand_subtitle_style = ParagraphStyle(
        'BrandSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#6E6259'),
    )

    invoice_heading_style = ParagraphStyle(
        'InvoiceHeading',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=18,
        leading=22,
        alignment=2,  # Right aligned
        textColor=colors.HexColor('#1F1511'),
    )

    invoice_meta_style = ParagraphStyle(
        'InvoiceMeta',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        alignment=2,  # Right aligned
        textColor=colors.HexColor('#4A3E38'),
    )

    section_header_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#8C331A'),
        spaceAfter=4,
    )

    body_style = ParagraphStyle(
        'InvoiceBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#2C201A'),
    )

    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=11,
        textColor=colors.white,
    )

    table_header_right_style = ParagraphStyle(
        'TableHeaderRight',
        parent=table_header_style,
        alignment=2,
    )

    table_header_center_style = ParagraphStyle(
        'TableHeaderCenter',
        parent=table_header_style,
        alignment=1,
    )

    table_cell_style = ParagraphStyle(
        'TableCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#2C201A'),
    )

    table_cell_right_style = ParagraphStyle(
        'TableCellRight',
        parent=table_cell_style,
        alignment=2,
    )

    table_cell_center_style = ParagraphStyle(
        'TableCellCenter',
        parent=table_cell_style,
        alignment=1,
    )

    story = []

    # ---------------------------------------------------------
    # 1. HEADER TABLE: Brand & Business info (Left) | INVOICE Info (Right)
    # ---------------------------------------------------------
    business_paragraphs = [
        Paragraph("CHAI FLOW", brand_title_style),
        Paragraph("Fresh Brews & Fast Counter Billing", brand_subtitle_style),
    ]

    # Invoice meta on the right
    inv_number = invoice.invoice_number or f"INV-{invoice.id:04d}"
    inv_date = invoice.created_at.strftime('%d %b %Y, %I:%M %p')
    status_display = invoice.get_status_display() if hasattr(invoice, 'get_status_display') else invoice.status.replace('_', ' ').title()

    invoice_info_paragraphs = [
        Paragraph("TAX INVOICE", invoice_heading_style),
        Spacer(1, 2 * mm),
        Paragraph(f"<b>Invoice #:</b> {inv_number}", invoice_meta_style),
        Paragraph(f"<b>Date:</b> {inv_date}", invoice_meta_style),
        Paragraph(f"<b>Status:</b> <b>{status_display.upper()}</b>", invoice_meta_style),
    ]

    header_table = Table(
        [[business_paragraphs, invoice_info_paragraphs]],
        colWidths=[100 * mm, 80 * mm],
    )
    header_table.setStyle(
        TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
        ])
    )
    story.append(header_table)
    story.append(Spacer(1, 5 * mm))

    # Divider bar
    divider_table = Table([['']], colWidths=[180 * mm], rowHeights=[1.5])
    divider_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#7A2E19')),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(divider_table)
    story.append(Spacer(1, 5 * mm))

    # ---------------------------------------------------------
    # 2. BILL TO / CUSTOMER INFO SECTION
    # ---------------------------------------------------------
    cust = invoice.customer
    customer_name = cust.name if cust else (invoice.customer_name or 'Walk-in Customer')
    customer_phone = cust.phone if cust else (invoice.customer_phone or '')
    customer_address = (cust.address if cust else '') or ''

    bill_to_content = [
        Paragraph("BILL TO", section_header_style),
        Paragraph(f"<b>{customer_name}</b>", body_style),
    ]
    if customer_phone:
        bill_to_content.append(Paragraph(f"Phone: {customer_phone}", body_style))
    if customer_address:
        bill_to_content.append(Paragraph(f"Address: {customer_address}", body_style))

    bill_to_table = Table([[bill_to_content]], colWidths=[180 * mm])
    bill_to_table.setStyle(
        TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FAF6F2')),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#E8DFD7')),
        ])
    )
    story.append(bill_to_table)
    story.append(Spacer(1, 5 * mm))

    # ---------------------------------------------------------
    # 3. LINE ITEMS TABLE
    # ---------------------------------------------------------
    # Columns: Sr. (12mm), Item & Description (88mm), Unit Price (26mm), Qty (18mm), Total (36mm) = 180mm
    items_data = [[
        Paragraph("Sr.", table_header_center_style),
        Paragraph("Product Description", table_header_style),
        Paragraph("Unit Price (Rs.)", table_header_right_style),
        Paragraph("Qty", table_header_center_style),
        Paragraph("Total (Rs.)", table_header_right_style),
    ]]

    invoice_items = list(invoice.items.all().order_by('id'))
    if not invoice_items:
        # Fallback if no line items exist
        items_data.append([
            Paragraph("1", table_cell_center_style),
            Paragraph("General Beverage / Counter Order", table_cell_style),
            Paragraph(f"{invoice.grand_total:.2f}", table_cell_right_style),
            Paragraph("1", table_cell_center_style),
            Paragraph(f"{invoice.grand_total:.2f}", table_cell_right_style),
        ])
    else:
        for idx, item in enumerate(invoice_items, 1):
            items_data.append([
                Paragraph(str(idx), table_cell_center_style),
                Paragraph(item.product_name, table_cell_style),
                Paragraph(f"{item.unit_price:.2f}", table_cell_right_style),
                Paragraph(str(item.quantity), table_cell_center_style),
                Paragraph(f"{item.total_price:.2f}", table_cell_right_style),
            ])

    items_table = Table(
        items_data,
        colWidths=[12 * mm, 88 * mm, 26 * mm, 18 * mm, 36 * mm],
        repeatRows=1,
    )

    t_style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#7A2E19')),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (2, 1), (2, -1), 'RIGHT'),
        ('ALIGN', (3, 0), (3, -1), 'CENTER'),
        ('ALIGN', (4, 1), (4, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LEFTPADDING', (0, 0), (-1, -1), 6),
        ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#E5E0DA')),
    ]

    # Alternating row colors
    for r in range(1, len(items_data)):
        bg_col = colors.HexColor('#FFFFFF') if r % 2 != 0 else colors.HexColor('#FCFAF8')
        t_style.append(('BACKGROUND', (0, r), (-1, r), bg_col))

    items_table.setStyle(TableStyle(t_style))
    story.append(items_table)
    story.append(Spacer(1, 4 * mm))

    # ---------------------------------------------------------
    # 4. PAYMENT & TOTAL SUMMARY SECTION
    # ---------------------------------------------------------
    total_paid = invoice.total_paid
    pending_amount = invoice.pending_amount

    # Fetch recorded payment methods
    payments = list(invoice.payments.all())
    if payments:
        methods_list = sorted(list({p.method.upper() for p in payments}))
        methods_str = ", ".join(methods_list)
    else:
        methods_str = "None (Unpaid)"

    summary_rows = [
        [
            Paragraph("<b>Subtotal:</b>", table_cell_style),
            Paragraph(f"Rs. {invoice.subtotal:.2f}", table_cell_right_style),
        ],
        [
            Paragraph("<b>Grand Total:</b>", table_cell_style),
            Paragraph(f"<b>Rs. {invoice.grand_total:.2f}</b>", table_cell_right_style),
        ],
        [
            Paragraph("<b>Amount Received:</b>", table_cell_style),
            Paragraph(f"Rs. {total_paid:.2f}", table_cell_right_style),
        ],
        [
            Paragraph("<b>Payment Method:</b>", table_cell_style),
            Paragraph(methods_str, table_cell_right_style),
        ],
        [
            Paragraph("<b>Remaining Due:</b>", table_cell_style),
            Paragraph(
                f"<font color='{'#DC2626' if pending_amount > 0 else '#059669'}'><b>Rs. {pending_amount:.2f}</b></font>",
                table_cell_right_style,
            ),
        ],
    ]

    summary_table = Table(summary_rows, colWidths=[40 * mm, 38 * mm])
    summary_table.setStyle(
        TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
            ('LINEBELOW', (0, 0), (-1, -2), 0.5, colors.HexColor('#F0EAE4')),
            ('BACKGROUND', (0, -1), (-1, -1), colors.HexColor('#FAF6F2')),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#E8DFD7')),
        ])
    )

    # Place notes on the left and summary table on the right
    notes_text = invoice.notes or "Thank you for visiting Chai Flow! Have a refreshing day."
    notes_paragraphs = [
        Paragraph("<b>NOTES & TERMS:</b>", section_header_style),
        Paragraph(notes_text, body_style),
    ]

    bottom_block = Table(
        [[notes_paragraphs, summary_table]],
        colWidths=[102 * mm, 78 * mm],
    )
    bottom_block.setStyle(
        TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ])
    )

    story.append(KeepTogether([bottom_block]))

    # Build PDF using NumberedCanvas
    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


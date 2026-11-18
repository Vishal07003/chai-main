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
        self.setFillColor(colors.HexColor("#8E8278"))

        # Footer divider line
        self.setStrokeColor(colors.HexColor("#EFEAE4"))
        self.setLineWidth(0.75)
        self.line(15 * mm, 14 * mm, 195 * mm, 14 * mm)

        # Left footer: Brand & Thank you note
        self.drawString(
            15 * mm,
            9 * mm,
            "Chai Flow • Smart Tea Supply & Delivery Network • Thank you for your business!",
        )

        # Right footer: Page X of Y
        page_str = f"Page {self._pageNumber} of {page_count}"
        self.drawRightString(195 * mm, 9 * mm, page_str)

        self.restoreState()


def generate_invoice_pdf(invoice) -> bytes:
    """
    Generate clean, modern, premium A4 PDF for an Invoice.
    Returns PDF bytes.
    """
    buffer = BytesIO()

    # A4: 210mm x 297mm. Printable width = 180mm with 15mm left/right margins
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()

    # Custom typography & styles matching Chai Flow brand theme
    brand_title_style = ParagraphStyle(
        'BrandTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=24,
        leading=28,
        textColor=colors.HexColor('#8C331A'),
    )

    brand_subtitle_style = ParagraphStyle(
        'BrandSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor('#7A6A60'),
    )

    invoice_heading_style = ParagraphStyle(
        'InvoiceHeading',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=20,
        leading=24,
        alignment=2,  # Right aligned
        textColor=colors.HexColor('#2A2421'),
    )

    invoice_meta_label = ParagraphStyle(
        'InvoiceMetaLabel',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        alignment=2,
        textColor=colors.HexColor('#8E8278'),
    )

    invoice_meta_value = ParagraphStyle(
        'InvoiceMetaValue',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9.5,
        leading=13,
        alignment=2,
        textColor=colors.HexColor('#2A2421'),
    )

    section_header_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor('#8C331A'),
        spaceAfter=4,
    )

    body_bold_style = ParagraphStyle(
        'BodyBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=10.5,
        leading=14,
        textColor=colors.HexColor('#2A2421'),
    )

    body_style = ParagraphStyle(
        'InvoiceBody',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#4A3E38'),
    )

    table_header_style = ParagraphStyle(
        'TableHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9,
        leading=12,
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
        fontSize=9,
        leading=13,
        textColor=colors.HexColor('#2A2421'),
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

    table_cell_bold_right_style = ParagraphStyle(
        'TableCellBoldRight',
        parent=table_cell_style,
        fontName='Helvetica-Bold',
        alignment=2,
    )

    story = []

    # ---------------------------------------------------------
    # 1. HEADER: Brand (Left) | TAX INVOICE Meta (Right)
    # ---------------------------------------------------------
    business_paragraphs = [
        Paragraph("CHAI FLOW", brand_title_style),
        Spacer(1, 1 * mm),
        Paragraph("Smart Tea Supply & Delivery Network", brand_subtitle_style),
    ]

    inv_number = invoice.invoice_number or f"INV-{invoice.id:04d}"
    inv_date = invoice.created_at.strftime('%d %b %Y, %I:%M %p')

    # Determine payment status
    total_paid = Decimal(str(invoice.total_paid))
    grand_total = Decimal(str(invoice.grand_total))
    pending_amount = Decimal(str(invoice.pending_amount))

    if invoice.status == 'paid' or pending_amount <= Decimal('0.00'):
        status_text = "PAID"
        status_bg = colors.HexColor("#ECFDF5")
        status_fg = colors.HexColor("#065F46")
        status_border = colors.HexColor("#A7F3D0")
    elif total_paid > Decimal('0.00') or invoice.status == 'partially_paid':
        status_text = "PARTIALLY PAID"
        status_bg = colors.HexColor("#FFFBEB")
        status_fg = colors.HexColor("#B45309")
        status_border = colors.HexColor("#FDE68A")
    else:
        status_text = "UNPAID"
        status_bg = colors.HexColor("#FEF2F2")
        status_fg = colors.HexColor("#B91C1C")
        status_border = colors.HexColor("#FECACA")

    status_badge_table = Table(
        [[Paragraph(f"<font color='{status_fg.hexval()}'><b>{status_text}</b></font>", table_header_center_style)]],
        colWidths=[38 * mm],
        rowHeights=[6.5 * mm],
    )
    status_badge_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), status_bg),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('BOX', (0, 0), (-1, -1), 0.75, status_border),
        ('TOPPADDING', (0, 0), (-1, -1), 1),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ]))

    meta_rows = [
        [Paragraph("Invoice Number:", invoice_meta_label), Paragraph(inv_number, invoice_meta_value)],
        [Paragraph("Invoice Date:", invoice_meta_label), Paragraph(inv_date, invoice_meta_value)],
        [Paragraph("Payment Status:", invoice_meta_label), status_badge_table],
    ]
    meta_table = Table(meta_rows, colWidths=[42 * mm, 40 * mm])
    meta_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 2),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))

    invoice_info_paragraphs = [
        Paragraph("TAX INVOICE", invoice_heading_style),
        Spacer(1, 2 * mm),
        meta_table,
    ]

    header_table = Table(
        [[business_paragraphs, invoice_info_paragraphs]],
        colWidths=[98 * mm, 82 * mm],
    )
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 4 * mm))

    # Clean brand accent divider
    divider_table = Table([['']], colWidths=[180 * mm], rowHeights=[2])
    divider_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#8C331A')),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
    ]))
    story.append(divider_table)
    story.append(Spacer(1, 4 * mm))

    # ---------------------------------------------------------
    # 2. CUSTOMER DETAILS (BILL TO SECTION)
    # ---------------------------------------------------------
    cust = invoice.customer
    customer_name = cust.name if cust else (invoice.customer_name or 'Walk-in Customer')
    customer_phone = cust.phone if cust else (invoice.customer_phone or '')

    # Check for delivery address from order or customer profile
    order_delivery_addr = invoice.order.delivery_address if invoice.order and invoice.order.delivery_address else ''
    customer_address = order_delivery_addr or (cust.address if cust else '') or ''

    bill_to_content = [
        Paragraph("BILL TO", section_header_style),
        Paragraph(customer_name, body_bold_style),
    ]
    if customer_phone:
        bill_to_content.append(Paragraph(f"<b>Mobile:</b> {customer_phone}", body_style))
    if customer_address:
        bill_to_content.append(Paragraph(f"<b>Delivery Address:</b> {customer_address}", body_style))

    bill_to_table = Table([[bill_to_content]], colWidths=[180 * mm])
    bill_to_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FAF8F5')),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#EFEAE4')),
    ]))
    story.append(bill_to_table)
    story.append(Spacer(1, 5 * mm))

    # ---------------------------------------------------------
    # 3. PRODUCT TABLE
    # ---------------------------------------------------------
    # Columns: Sr. (14mm), Product Description (84mm), Unit Price (26mm), Quantity (20mm), Total Amount (36mm) = 180mm
    items_data = [[
        Paragraph("Sr.", table_header_center_style),
        Paragraph("Product Description", table_header_style),
        Paragraph("Unit Price (₹)", table_header_right_style),
        Paragraph("Quantity", table_header_center_style),
        Paragraph("Total Amount (₹)", table_header_right_style),
    ]]

    invoice_items = list(invoice.items.all().order_by('id'))
    if not invoice_items:
        items_data.append([
            Paragraph("1", table_cell_center_style),
            Paragraph("General Beverage / Counter Order", table_cell_style),
            Paragraph(f"{invoice.grand_total:.2f}", table_cell_right_style),
            Paragraph("1", table_cell_center_style),
            Paragraph(f"{invoice.grand_total:.2f}", table_cell_bold_right_style),
        ])
    else:
        for idx, item in enumerate(invoice_items, 1):
            items_data.append([
                Paragraph(str(idx), table_cell_center_style),
                Paragraph(item.product_name, table_cell_style),
                Paragraph(f"{item.unit_price:.2f}", table_cell_right_style),
                Paragraph(str(item.quantity), table_cell_center_style),
                Paragraph(f"{item.total_price:.2f}", table_cell_bold_right_style),
            ])

    items_table = Table(
        items_data,
        colWidths=[14 * mm, 84 * mm, 26 * mm, 20 * mm, 36 * mm],
        repeatRows=1,
    )

    t_style = [
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8C331A')),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('ALIGN', (2, 1), (2, -1), 'RIGHT'),
        ('ALIGN', (3, 0), (3, -1), 'CENTER'),
        ('ALIGN', (4, 1), (4, -1), 'RIGHT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6.5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6.5),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#EFEAE4')),
    ]

    # Alternating subtle row colors
    for r in range(1, len(items_data)):
        bg_col = colors.HexColor('#FFFFFF') if r % 2 != 0 else colors.HexColor('#FAF8F5')
        t_style.append(('BACKGROUND', (0, r), (-1, r), bg_col))

    items_table.setStyle(TableStyle(t_style))
    story.append(items_table)
    story.append(Spacer(1, 4 * mm))

    # ---------------------------------------------------------
    # 4. PAYMENT & TOTAL SUMMARY SECTION
    # ---------------------------------------------------------
    payments = list(invoice.payments.all())
    if payments:
        methods_list = sorted(list({p.method.upper() for p in payments}))
        methods_str = ", ".join(methods_list)
    else:
        methods_str = "None (Unpaid)"

    summary_label_style = ParagraphStyle(
        'SumLabel',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9.5,
        leading=13,
        textColor=colors.HexColor('#4A3E38'),
    )

    summary_value_style = ParagraphStyle(
        'SumValue',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=9.5,
        leading=13,
        alignment=2,
        textColor=colors.HexColor('#2A2421'),
    )

    grand_total_label_style = ParagraphStyle(
        'GrandLabel',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=15,
        textColor=colors.HexColor('#8C331A'),
    )

    grand_total_value_style = ParagraphStyle(
        'GrandValue',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        alignment=2,
        textColor=colors.HexColor('#8C331A'),
    )

    remaining_due_color = '#DC2626' if pending_amount > Decimal('0.00') else '#059669'
    remaining_due_value_style = ParagraphStyle(
        'DueValue',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=11,
        leading=15,
        alignment=2,
        textColor=colors.HexColor(remaining_due_color),
    )

    summary_rows = [
        [
            Paragraph("Subtotal", summary_label_style),
            Paragraph(f"₹ {invoice.subtotal:.2f}", summary_value_style),
        ],
    ]

    # Include discount row if applicable
    if hasattr(invoice, 'discount_amount') and invoice.discount_amount and Decimal(str(invoice.discount_amount)) > 0:
        summary_rows.append([
            Paragraph("Discount", summary_label_style),
            Paragraph(f"- ₹ {Decimal(str(invoice.discount_amount)):.2f}", summary_value_style),
        ])

    summary_rows.extend([
        [
            Paragraph("Grand Total", grand_total_label_style),
            Paragraph(f"₹ {invoice.grand_total:.2f}", grand_total_value_style),
        ],
        [
            Paragraph("Amount Received", summary_label_style),
            Paragraph(f"₹ {total_paid:.2f}", summary_value_style),
        ],
        [
            Paragraph("Payment Method", summary_label_style),
            Paragraph(methods_str, summary_value_style),
        ],
        [
            Paragraph("Remaining Due", grand_total_label_style),
            Paragraph(f"₹ {pending_amount:.2f}", remaining_due_value_style),
        ],
    ])

    summary_table = Table(summary_rows, colWidths=[44 * mm, 40 * mm])
    summary_table.setStyle(
        TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 4.5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4.5),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('LINEBELOW', (0, 0), (-1, -2), 0.5, colors.HexColor('#EFEAE4')),
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FAF8F5')),
            ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#EFEAE4')),
        ])
    )

    # Place notes on the left and summary table on the right
    notes_text = invoice.notes or "Thank you for partnering with Chai Flow! Freshly brewed and delivered with care."
    notes_paragraphs = [
        Paragraph("NOTES & TERMS", section_header_style),
        Spacer(1, 1 * mm),
        Paragraph(notes_text, body_style),
    ]

    notes_box = Table([[notes_paragraphs]], colWidths=[90 * mm])
    notes_box.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FAF8F5')),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#EFEAE4')),
    ]))

    bottom_block = Table(
        [[notes_box, summary_table]],
        colWidths=[94 * mm, 86 * mm],
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



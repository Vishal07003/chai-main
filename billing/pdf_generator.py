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
        Paragraph("Unit Price (Rs.)", table_header_right_style),
        Paragraph("Quantity", table_header_center_style),
        Paragraph("Total Amount (Rs.)", table_header_right_style),
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
            Paragraph(f"Rs. {invoice.subtotal:.2f}", summary_value_style),
        ],
    ]

    # Include discount row if applicable
    if hasattr(invoice, 'discount_amount') and invoice.discount_amount and Decimal(str(invoice.discount_amount)) > 0:
        summary_rows.append([
            Paragraph("Discount", summary_label_style),
            Paragraph(f"- Rs. {Decimal(str(invoice.discount_amount)):.2f}", summary_value_style),
        ])

    summary_rows.extend([
        [
            Paragraph("Grand Total", grand_total_label_style),
            Paragraph(f"Rs. {invoice.grand_total:.2f}", grand_total_value_style),
        ],
        [
            Paragraph("Amount Received", summary_label_style),
            Paragraph(f"Rs. {total_paid:.2f}", summary_value_style),
        ],
        [
            Paragraph("Payment Method", summary_label_style),
            Paragraph(methods_str, summary_value_style),
        ],
        [
            Paragraph("Remaining Due", grand_total_label_style),
            Paragraph(f"Rs. {pending_amount:.2f}", remaining_due_value_style),
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


# ---------------------------------------------------------------------------
# Branded Payment Receipt Generator
# ---------------------------------------------------------------------------

_RECEIPT_FONT_INITIALIZED = False
_RECEIPT_FONT_NAME = "Helvetica"
_RECEIPT_FONT_BOLD = "Helvetica-Bold"
_RECEIPT_CURRENCY_SYMBOL = "₹"

def _init_receipt_fonts():
    global _RECEIPT_FONT_INITIALIZED, _RECEIPT_FONT_NAME, _RECEIPT_FONT_BOLD, _RECEIPT_CURRENCY_SYMBOL
    if _RECEIPT_FONT_INITIALIZED:
        return

    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    # Priority font candidates that support Unicode ₹ (U+20B9)
    candidates = [
        # Linux / Docker paths
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
        ("/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf", "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf"),
        # Windows paths
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        (r"C:\Windows\Fonts\segoeui.ttf", r"C:\Windows\Fonts\segoeuib.ttf"),
        (r"C:\Windows\Fonts\calibri.ttf", r"C:\Windows\Fonts\calibrib.ttf"),
    ]

    for reg_path, bold_path in candidates:
        if os.path.exists(reg_path) and os.path.exists(bold_path):
            try:
                pdfmetrics.registerFont(TTFont("ReceiptFont", reg_path))
                pdfmetrics.registerFont(TTFont("ReceiptFont-Bold", bold_path))
                _RECEIPT_FONT_NAME = "ReceiptFont"
                _RECEIPT_FONT_BOLD = "ReceiptFont-Bold"
                _RECEIPT_CURRENCY_SYMBOL = "₹"
                _RECEIPT_FONT_INITIALIZED = True
                return
            except Exception:
                pass

    # Standard fallback
    _RECEIPT_FONT_NAME = "Helvetica"
    _RECEIPT_FONT_BOLD = "Helvetica-Bold"
    _RECEIPT_CURRENCY_SYMBOL = "Rs. "
    _RECEIPT_FONT_INITIALIZED = True


def generate_payment_receipt_pdf(payment) -> bytes:
    """
    Generate professional branded A4 PDF Payment Receipt for an individual Payment.
    Brand:
      - Primary: #8C331A
      - Background: #FFFFFF / #FAF8F5
      - Success: #16865B
    Includes:
      - Header & Brand styling
      - Payment Details (amount, method, reference, date, customer)
      - Allocations Table (invoice numbers, amounts)
      - Partial or Advance credit handling
      - Footer (Thank-you note & timestamp)
    """
    _init_receipt_fonts()

    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=20 * mm,
    )

    PRIMARY = colors.HexColor("#8C331A")
    SUCCESS = colors.HexColor("#16865B")
    NEUTRAL_DARK = colors.HexColor("#2C201A")
    NEUTRAL_MUTED = colors.HexColor("#6B5E55")
    NEUTRAL_LIGHT = colors.HexColor("#FAF8F5")
    BORDER_COLOR = colors.HexColor("#EFEAE4")

    styles = getSampleStyleSheet()

    # Custom Typography
    brand_style = ParagraphStyle(
        'ReceiptBrand',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_BOLD,
        fontSize=20,
        leading=24,
        textColor=PRIMARY,
    )
    title_style = ParagraphStyle(
        'ReceiptTitle',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_BOLD,
        fontSize=15,
        leading=18,
        textColor=PRIMARY,
        alignment=2,  # Right-aligned
    )
    title_sub_style = ParagraphStyle(
        'ReceiptTitleSub',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_NAME,
        fontSize=9,
        leading=13,
        textColor=NEUTRAL_MUTED,
        alignment=2,
    )
    badge_style = ParagraphStyle(
        'ReceiptBadge',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_BOLD,
        fontSize=9,
        leading=12,
        textColor=SUCCESS,
        alignment=2,
    )
    meta_label_style = ParagraphStyle(
        'ReceiptMetaLabel',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_BOLD,
        fontSize=8,
        leading=11,
        textColor=NEUTRAL_MUTED,
    )
    meta_value_style = ParagraphStyle(
        'ReceiptMetaValue',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_BOLD,
        fontSize=9.5,
        leading=13,
        textColor=NEUTRAL_DARK,
    )
    hero_amount_style = ParagraphStyle(
        'ReceiptHeroAmount',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_BOLD,
        fontSize=22,
        leading=26,
        textColor=SUCCESS,
    )
    hero_label_style = ParagraphStyle(
        'ReceiptHeroLabel',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_BOLD,
        fontSize=8.5,
        leading=11,
        textColor=NEUTRAL_MUTED,
    )
    section_head_style = ParagraphStyle(
        'ReceiptSectionHead',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_BOLD,
        fontSize=10,
        leading=14,
        textColor=PRIMARY,
    )
    table_head_style = ParagraphStyle(
        'ReceiptTableHead',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_BOLD,
        fontSize=8.5,
        leading=11,
        textColor=colors.white,
    )
    table_head_right = ParagraphStyle(
        'ReceiptTableHeadRight',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_BOLD,
        fontSize=8.5,
        leading=11,
        textColor=colors.white,
        alignment=2,
    )
    table_cell_style = ParagraphStyle(
        'ReceiptTableCell',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_NAME,
        fontSize=8.5,
        leading=12,
        textColor=NEUTRAL_DARK,
    )
    table_cell_right = ParagraphStyle(
        'ReceiptTableCellRight',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_NAME,
        fontSize=8.5,
        leading=12,
        textColor=NEUTRAL_DARK,
        alignment=2,
    )
    notes_style = ParagraphStyle(
        'ReceiptNotes',
        parent=styles['Normal'],
        fontName=_RECEIPT_FONT_NAME,
        fontSize=8.5,
        leading=12,
        textColor=NEUTRAL_MUTED,
    )

    story = []

    # 1. Top Header Bar
    sym = _RECEIPT_CURRENCY_SYMBOL
    receipt_no = f"RCT-{payment.id:06d}"
    formatted_date = payment.received_at.strftime("%d %b %Y, %I:%M %p")

    header_left = [
        Paragraph("<b>CHAI FLOW</b>", brand_style),
        Spacer(1, 1 * mm),
        Paragraph("Smart Tea Supply & Delivery Network", ParagraphStyle('Tag', parent=styles['Normal'], fontName=_RECEIPT_FONT_NAME, fontSize=9, textColor=NEUTRAL_MUTED)),
    ]
    header_right = [
        Paragraph("PAYMENT RECEIPT", title_style),
        Spacer(1, 1 * mm),
        Paragraph(f"Receipt No: <b>{receipt_no}</b>", title_sub_style),
        Paragraph(f"Payment ID: <b>#{payment.id}</b>", title_sub_style),
        Spacer(1, 1 * mm),
        Paragraph("● PAYMENT RECEIVED", badge_style),
    ]

    header_table = Table([[header_left, header_right]], colWidths=[100 * mm, 80 * mm])
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LINEBELOW', (0, 0), (-1, -1), 1.5, PRIMARY),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 5 * mm))

    # 2. Customer & Hero Amount Card
    cust = payment.customer
    comp = payment.company
    customer_name = cust.name if cust else (comp.name if comp else "Customer Account")
    customer_phone = cust.phone if cust else (comp.phone if comp else "N/A")
    customer_address = cust.address if cust else (comp.address if comp else "")

    customer_info = [
        Paragraph("RECEIVED FROM", meta_label_style),
        Spacer(1, 1 * mm),
        Paragraph(customer_name, ParagraphStyle('CN', parent=styles['Normal'], fontName=_RECEIPT_FONT_BOLD, fontSize=12, textColor=NEUTRAL_DARK)),
        Spacer(1, 1 * mm),
        Paragraph(f"Phone: {customer_phone}", meta_value_style),
    ]
    if customer_address:
        customer_info.append(Paragraph(f"Address: {customer_address}", notes_style))

    method_display = dict(payment.METHOD_CHOICES).get(payment.method, payment.method.title())
    payment_amount_str = f"{sym}{Decimal(str(payment.amount)):,.2f}"

    amount_box = [
        Paragraph("TOTAL AMOUNT RECEIVED", hero_label_style),
        Spacer(1, 1.5 * mm),
        Paragraph(payment_amount_str, hero_amount_style),
        Spacer(1, 2 * mm),
        Paragraph(f"Date: <b>{formatted_date}</b>", meta_value_style),
        Paragraph(f"Method: <b>{method_display}</b>", meta_value_style),
    ]
    if payment.reference_number:
        amount_box.append(Paragraph(f"Reference: <b>{payment.reference_number}</b>", meta_value_style))

    overview_table = Table([[customer_info, amount_box]], colWidths=[100 * mm, 80 * mm])
    overview_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), NEUTRAL_LIGHT),
        ('BOX', (0, 0), (-1, -1), 0.75, BORDER_COLOR),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(overview_table)
    story.append(Spacer(1, 6 * mm))

    # 3. Allocation Breakdown Section
    story.append(Paragraph("PAYMENT ALLOCATION BREAKDOWN", section_head_style))
    story.append(Spacer(1, 2.5 * mm))

    allocations = list(payment.allocations.select_related('invoice').all().order_by('id'))
    total_allocated = payment.total_allocated
    unallocated_amount = payment.unallocated_amount

    alloc_rows = [
        [
            Paragraph("Type / Reference", table_head_style),
            Paragraph("Invoice / Account Description", table_head_style),
            Paragraph("Allocated Date", table_head_style),
            Paragraph(f"Amount ({sym.strip()})", table_head_right),
        ]
    ]

    if allocations:
        for idx, alloc in enumerate(allocations, 1):
            inv = alloc.invoice
            inv_ref = inv.invoice_number or f"INV-{inv.id}"
            inv_date = inv.created_at.strftime("%d %b %Y") if inv.created_at else "-"
            alloc_date = alloc.allocated_at.strftime("%d %b %Y") if hasattr(alloc, 'allocated_at') and alloc.allocated_at else "-"
            alloc_rows.append([
                Paragraph(f"Invoice Allocation #{idx}", table_cell_style),
                Paragraph(f"<b>{inv_ref}</b> (Bill Date: {inv_date})", table_cell_style),
                Paragraph(alloc_date, table_cell_style),
                Paragraph(f"{sym}{Decimal(str(alloc.allocated_amount)):,.2f}", table_cell_right),
            ])
    elif payment.invoice:
        # Direct invoice payment without explicit allocation record
        inv = payment.invoice
        inv_ref = inv.invoice_number or f"INV-{inv.id}"
        inv_date = inv.created_at.strftime("%d %b %Y") if inv.created_at else "-"
        alloc_rows.append([
            Paragraph("Direct Invoice Settlement", table_cell_style),
            Paragraph(f"<b>{inv_ref}</b> (Bill Date: {inv_date})", table_cell_style),
            Paragraph(formatted_date.split(',')[0], table_cell_style),
            Paragraph(payment_amount_str, table_cell_right),
        ])

    # If unallocated advance credit remains
    if unallocated_amount > Decimal('0.00'):
        alloc_rows.append([
            Paragraph("Customer Advance Credit", ParagraphStyle('Adv', parent=table_cell_style, fontName=_RECEIPT_FONT_BOLD, textColor=colors.HexColor("#2563EB"))),
            Paragraph("Unallocated Balance (Held on Account)", ParagraphStyle('AdvDesc', parent=table_cell_style, textColor=colors.HexColor("#2563EB"))),
            Paragraph("Available for future bills", table_cell_style),
            Paragraph(f"{sym}{unallocated_amount:,.2f}", ParagraphStyle('AdvAmt', parent=table_cell_right, fontName=_RECEIPT_FONT_BOLD, textColor=colors.HexColor("#2563EB"))),
        ])

    # Summary Totals Row
    alloc_rows.append([
        Paragraph("<b>Total Received</b>", ParagraphStyle('Tot', parent=table_cell_style, fontName=_RECEIPT_FONT_BOLD)),
        Paragraph("", table_cell_style),
        Paragraph("", table_cell_style),
        Paragraph(f"<b>{payment_amount_str}</b>", ParagraphStyle('TotR', parent=table_cell_right, fontName=_RECEIPT_FONT_BOLD, textColor=SUCCESS)),
    ])

    alloc_table = Table(alloc_rows, colWidths=[45 * mm, 65 * mm, 35 * mm, 35 * mm])
    alloc_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), PRIMARY),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('LINEBELOW', (0, 0), (-1, -1), 0.5, BORDER_COLOR),
        ('BACKGROUND', (0, -1), (-1, -1), NEUTRAL_LIGHT),
        ('LINEABOVE', (0, -1), (-1, -1), 1.0, PRIMARY),
        ('BOX', (0, 0), (-1, -1), 0.75, BORDER_COLOR),
    ]))
    story.append(alloc_table)
    story.append(Spacer(1, 6 * mm))

    # 4. Notes & Status Banner
    notes_content = payment.notes or "Official electronic receipt acknowledging receipt of payment."
    status_text = "Fully Allocated to Invoice(s)" if unallocated_amount == Decimal('0.00') else (
        "Partially Allocated / Advance Balance Available" if total_allocated > Decimal('0.00') else "Customer Advance Payment (Unallocated Credit)"
    )

    notes_box = [
        Paragraph("NOTES & REMARKS", meta_label_style),
        Spacer(1, 1 * mm),
        Paragraph(notes_content, notes_style),
        Spacer(1, 2 * mm),
        Paragraph(f"Allocation Status: <b>{status_text}</b>", ParagraphStyle('Stat', parent=notes_style, fontName=_RECEIPT_FONT_BOLD, textColor=PRIMARY)),
    ]

    status_box = [
        Paragraph("RECEIPT VERIFICATION", meta_label_style),
        Spacer(1, 1 * mm),
        Paragraph("This receipt was digitally generated by Chai Flow.", notes_style),
        Paragraph(f"Generated at: {payment.received_at.strftime('%Y-%m-%d %H:%M:%S')}", notes_style),
        Paragraph("No physical signature is required.", notes_style),
    ]

    footer_table = Table([[notes_box, status_box]], colWidths=[100 * mm, 80 * mm])
    footer_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), NEUTRAL_LIGHT),
        ('BOX', (0, 0), (-1, -1), 0.75, BORDER_COLOR),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        ('RIGHTPADDING', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(KeepTogether([footer_table]))

    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()




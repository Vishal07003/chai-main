import os
from io import BytesIO
from decimal import Decimal
from datetime import datetime

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
from .pdf_generator import NumberedCanvas


def generate_customer_ledger_pdf(customer, period_data: dict) -> bytes:
    """
    Renders professional, formatted A4 PDF for a Customer's Ledger Statement.
    Includes:
      - Customer details (name, phone, address)
      - Billing period date range & cycle day
      - Invoices table with dates, items, amounts, paid, remaining balance, status
      - Payments table with received date, method, amount, reference number, reversals
      - Credit balance & refund due summary
      - Final ledger summary (Opening balance, Invoiced, Collections, Outstanding)
    """
    buffer = BytesIO()

    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=20 * mm,
    )

    styles = getSampleStyleSheet()

    brand_title_style = ParagraphStyle(
        'LedgerBrandTitle',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=22,
        leading=26,
        textColor=colors.HexColor('#8C331A'),
    )

    brand_subtitle_style = ParagraphStyle(
        'LedgerBrandSubtitle',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#7A6A60'),
    )

    sec_heading_style = ParagraphStyle(
        'LedgerSectionHeading',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=12,
        leading=16,
        textColor=colors.HexColor('#8C331A'),
    )

    cell_style = ParagraphStyle(
        'LedgerCell',
        parent=styles['Normal'],
        fontName='Helvetica',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#2C201A'),
    )

    cell_bold_style = ParagraphStyle(
        'LedgerCellBold',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.HexColor('#2C201A'),
    )

    cell_hdr_style = ParagraphStyle(
        'LedgerCellHeader',
        parent=styles['Normal'],
        fontName='Helvetica-Bold',
        fontSize=8.5,
        leading=11,
        textColor=colors.white,
    )

    story = []

    # 1. Header Row: Brand Info (Left) + Document Title & Period (Right)
    period_label = period_data.get('period_label', 'Billing Period')
    generated_date_str = datetime.now().strftime("%d %b %Y, %I:%M %p")

    header_left = [
        Paragraph("Chai Flow", brand_title_style),
        Paragraph("Fresh Tea Stall Supply & Delivery Operations", brand_subtitle_style),
        Spacer(1, 2 * mm),
        Paragraph(f"<b>Customer:</b> {customer.name}", cell_style),
        Paragraph(f"<b>Phone:</b> {customer.phone or 'N/A'}", cell_style),
        Paragraph(f"<b>Address:</b> {customer.address or 'Counter / Walk-in'}", cell_style),
    ]

    header_right = [
        Paragraph("<b>CUSTOMER LEDGER STATEMENT</b>", ParagraphStyle('DocTitle', parent=styles['Normal'], fontName='Helvetica-Bold', fontSize=13, alignment=2, textColor=colors.HexColor('#8C331A'))),
        Paragraph(f"Period: <b>{period_label}</b>", ParagraphStyle('R1', parent=styles['Normal'], fontName='Helvetica', fontSize=9, alignment=2, textColor=colors.HexColor('#2C201A'))),
        Paragraph(f"Generated: {generated_date_str}", ParagraphStyle('R2', parent=styles['Normal'], fontName='Helvetica', fontSize=8, alignment=2, textColor=colors.HexColor('#7A6A60'))),
        Paragraph(f"Billing Cycle Day: {getattr(customer, 'billing_cycle_day', 1)}", ParagraphStyle('R3', parent=styles['Normal'], fontName='Helvetica', fontSize=8, alignment=2, textColor=colors.HexColor('#7A6A60'))),
    ]

    header_table = Table(
        [[header_left, header_right]],
        colWidths=[100 * mm, 80 * mm]
    )
    header_table.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 0),
        ('RIGHTPADDING', (0, 0), (-1, -1), 0),
        ('TOPPADDING', (0, 0), (-1, -1), 0),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4 * mm),
    ]))
    story.append(header_table)
    story.append(Spacer(1, 4 * mm))

    # 2. Financial Summary Highlights Box
    opening_bal = period_data.get('opening_balance', '0.00')
    total_invoiced = period_data.get('total_invoiced', '0.00')
    total_collections = period_data.get('total_collections', '0.00')
    outstanding_bal = period_data.get('outstanding_balance', '0.00')
    credit_bal = period_data.get('available_credit', '0.00')
    refund_due = period_data.get('refund_due', '0.00')

    summary_cells = [
        [
            Paragraph("<b>Opening Balance</b>", cell_style),
            Paragraph("<b>Total Invoiced</b>", cell_style),
            Paragraph("<b>Total Received</b>", cell_style),
            Paragraph("<b>Outstanding Balance</b>", cell_style),
            Paragraph("<b>Credit Balance</b>", cell_style),
            Paragraph("<b>Refund Due</b>", cell_style),
        ],
        [
            Paragraph(f"₹{opening_bal}", cell_bold_style),
            Paragraph(f"₹{total_invoiced}", cell_bold_style),
            Paragraph(f"₹{total_collections}", cell_bold_style),
            Paragraph(f"<b>₹{outstanding_bal}</b>", ParagraphStyle('Out', parent=cell_bold_style, textColor=colors.HexColor('#DC2626') if Decimal(str(outstanding_bal)) > 0 else colors.HexColor('#059669'))),
            Paragraph(f"₹{credit_bal}", ParagraphStyle('Cred', parent=cell_bold_style, textColor=colors.HexColor('#059669'))),
            Paragraph(f"₹{refund_due}", ParagraphStyle('Ref', parent=cell_bold_style, textColor=colors.HexColor('#D97706') if Decimal(str(refund_due)) > 0 else colors.HexColor('#2C201A'))),
        ]
    ]

    col_w = 180 * mm / 6
    summary_table = Table(summary_cells, colWidths=[col_w] * 6)
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#FAF8F5')),
        ('BOX', (0, 0), (-1, -1), 0.75, colors.HexColor('#EFEAE4')),
        ('INNERGRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#EFEAE4')),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 2.5 * mm),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5 * mm),
    ]))
    story.append(summary_table)
    story.append(Spacer(1, 5 * mm))

    # 3. Invoices Section
    story.append(Paragraph("Invoices & Billing History", sec_heading_style))
    story.append(Spacer(1, 2 * mm))

    invoices = period_data.get('invoices', [])
    if not invoices:
        story.append(Paragraph("<i>No invoices recorded for this billing period.</i>", cell_style))
    else:
        inv_headers = ["Date", "Invoice #", "Billing Type", "Grand Total", "Received", "Pending", "Status"]
        inv_col_widths = [26 * mm, 32 * mm, 28 * mm, 25 * mm, 25 * mm, 24 * mm, 20 * mm]

        inv_rows = [[Paragraph(h, cell_hdr_style) for h in inv_headers]]

        for inv in invoices:
            dt_str = inv.get('date', '')
            inv_no = inv.get('invoice_number', f"INV-{inv.get('invoice_id', '')}")
            b_type = (inv.get('billing_type') or 'daily').capitalize()
            tot = f"₹{inv.get('total_bill', '0.00')}"
            rec = f"₹{inv.get('amount_received', '0.00')}"
            pend = f"₹{inv.get('remaining_balance', '0.00')}"
            st = inv.get('status', 'PENDING')

            inv_rows.append([
                Paragraph(dt_str, cell_style),
                Paragraph(inv_no, cell_bold_style),
                Paragraph(b_type, cell_style),
                Paragraph(tot, cell_style),
                Paragraph(rec, cell_style),
                Paragraph(pend, cell_bold_style),
                Paragraph(st, cell_style),
            ])

        inv_table = Table(inv_rows, colWidths=inv_col_widths)
        inv_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8C331A')),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FAF8F5')]),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#EFEAE4')),
            ('TOPPADDING', (0, 0), (-1, -1), 2 * mm),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2 * mm),
        ]))
        story.append(inv_table)

    story.append(Spacer(1, 5 * mm))

    # 4. Payments & Reversals Section
    story.append(Paragraph("Payment & Collection Records", sec_heading_style))
    story.append(Spacer(1, 2 * mm))

    payments = period_data.get('payments', [])
    if not payments:
        story.append(Paragraph("<i>No payments recorded for this billing period.</i>", cell_style))
    else:
        pm_headers = ["Date & Time", "Payment ID", "Method", "Amount", "Allocated", "Status / Notes"]
        pm_col_widths = [32 * mm, 26 * mm, 24 * mm, 28 * mm, 28 * mm, 42 * mm]

        pm_rows = [[Paragraph(h, cell_hdr_style) for h in pm_headers]]

        for p in payments:
            d_time = p.get('received_at', '')[:16].replace('T', ' ')
            pid = f"PAY-{p.get('payment_id', '')}"
            mth = (p.get('method') or 'cash').upper()
            amt = f"₹{p.get('amount', '0.00')}"
            alloc = f"₹{p.get('total_allocated', '0.00')}"
            is_rev = p.get('is_reversed', False)

            notes_text = p.get('notes') or ''
            if is_rev:
                notes_text = f"<font color='#DC2626'><b>REVERSED:</b> {p.get('reversal_reason', 'Reversed by owner')}</font>"

            pm_rows.append([
                Paragraph(d_time, cell_style),
                Paragraph(pid, cell_bold_style),
                Paragraph(mth, cell_style),
                Paragraph(amt, cell_bold_style),
                Paragraph(alloc, cell_style),
                Paragraph(notes_text or '—', cell_style),
            ])

        pm_table = Table(pm_rows, colWidths=pm_col_widths)
        pm_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#8C331A')),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#FAF8F5')]),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#EFEAE4')),
            ('TOPPADDING', (0, 0), (-1, -1), 2 * mm),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2 * mm),
        ]))
        story.append(pm_table)

    # Build document with 2-pass NumberedCanvas
    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()

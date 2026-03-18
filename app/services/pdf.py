"""
Generate a clean A4 invoice PDF using ReportLab canvas.
No page numbers, no browser headers/footers — pure invoice layout.
"""
import io
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas


RED   = colors.HexColor('#dc2626')
DARK  = colors.HexColor('#1f2937')
BLACK = colors.black
WHITE = colors.white


def generate_invoice_pdf(invoice):
    """Return a BytesIO containing the rendered PDF."""
    buf = io.BytesIO()
    W, H = A4          # 595.27 × 841.89 pt
    M = 15 * mm        # page margin
    CW = W - 2 * M    # content width ≈ 515 pt

    c = rl_canvas.Canvas(buf, pagesize=A4)
    c.setTitle('')     # suppress filename in PDF metadata

    y = H - M          # cursor starts at top

    # ── HEADER ──────────────────────────────────────────────────────────────
    # Left: property brand
    c.setFillColor(BLACK)
    c.setFont('Helvetica-Bold', 17)
    c.drawString(M, y - 16, 'Sheeza Manzil Guesthouse')
    c.setFont('Helvetica', 9)
    c.drawString(M, y - 28, 'Maaveyo Magu, Hdh. Hanimaadhoo, Maldives')
    c.drawString(M, y - 39, 'Tel: +960 737 5797')

    # Right: INVOICE meta
    c.setFont('Helvetica-Bold', 22)
    c.drawRightString(W - M, y - 16, 'INVOICE')
    c.setFont('Helvetica', 9)
    c.drawRightString(W - M, y - 30, f'No: {invoice.invoice_number}')
    c.drawRightString(W - M, y - 41, f'Date: {invoice.issue_date.strftime("%d %B %Y")}')
    c.drawRightString(W - M, y - 52, f'Booking: {invoice.booking.booking_ref}')

    y -= 64

    # ── THICK RULE ──────────────────────────────────────────────────────────
    c.setStrokeColor(BLACK)
    c.setLineWidth(2)
    c.line(M, y, W - M, y)
    y -= 14

    # ── BILL TO / STAY DETAILS (two columns) ────────────────────────────────
    col1_x = M
    col2_x = M + CW * 0.52

    # Section labels
    c.setFont('Helvetica-Bold', 7)
    c.setFillColor(BLACK)
    c.drawString(col1_x, y, 'BILL TO')
    c.drawString(col2_x, y, 'STAY DETAILS')
    y -= 13

    # Bill To column
    bill_y = y
    c.setFont('Helvetica-Bold', 10)
    c.setFillColor(BLACK)
    c.drawString(col1_x, bill_y, invoice.bill_to_name)
    bill_y -= 12

    c.setFont('Helvetica', 9)
    if invoice.company_name:
        c.drawString(col1_x, bill_y, invoice.company_name)
        bill_y -= 11
    if invoice.billing_address:
        for line in invoice.billing_address.replace('\r', '').split('\n'):
            line = line.strip()
            if line:
                c.drawString(col1_x, bill_y, line)
                bill_y -= 11
    if invoice.bill_to_name != invoice.booking.guest.full_name:
        c.drawString(col1_x, bill_y, f'Guest: {invoice.booking.guest.full_name}')
        bill_y -= 11
    if invoice.booking.guest.phone:
        c.drawString(col1_x, bill_y, invoice.booking.guest.phone)
        bill_y -= 11

    # Stay Details column
    stay_y = y
    nights = invoice.booking.nights
    stay_rows = [
        ('Room',      f'{invoice.booking.room.number} \u2014 {invoice.booking.room.room_type}'),
        ('Check-in',  invoice.booking.check_in_date.strftime('%d %B %Y')),
        ('Check-out', invoice.booking.check_out_date.strftime('%d %B %Y')),
        ('Nights',    f'{nights} night{"s" if nights != 1 else ""}'),
        ('Guests',    str(invoice.booking.num_guests)),
    ]
    label_w = 52
    for label, value in stay_rows:
        c.setFont('Helvetica', 9)
        c.setFillColor(BLACK)
        c.drawString(col2_x, stay_y, label)
        c.setFont('Helvetica-Bold', 9)
        c.drawString(col2_x + label_w, stay_y, value)
        stay_y -= 12

    y = min(bill_y, stay_y) - 10

    # ── THIN RULE ────────────────────────────────────────────────────────────
    c.setStrokeColor(BLACK)
    c.setLineWidth(0.5)
    c.line(M, y, W - M, y)
    y -= 15

    # ── LINE ITEMS TABLE ─────────────────────────────────────────────────────
    desc_w   = CW - 55 - 65 - 70   # Description column gets remainder
    nights_w = 55
    rate_w   = 65
    amt_w    = 70
    cx = [M, M + desc_w, M + desc_w + nights_w, M + desc_w + nights_w + rate_w]
    row_h = 18

    # Header
    c.setFillColor(DARK)
    c.rect(M, y - row_h, CW, row_h, fill=1, stroke=0)
    c.setFillColor(WHITE)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(cx[0] + 5,                  y - 12, 'Description')
    c.drawCentredString(cx[1] + nights_w / 2, y - 12, 'Nights')
    c.drawRightString(cx[2] + rate_w - 5,    y - 12, 'Rate (MVR)')
    c.drawRightString(cx[3] + amt_w - 5,     y - 12, 'Amount (MVR)')
    y -= row_h

    # Data row
    c.setFillColor(BLACK)
    c.setFont('Helvetica-Bold', 9)
    c.drawString(cx[0] + 5, y - 11,
                 f'Room {invoice.booking.room.number} \u2014 {invoice.booking.room.room_type}')
    c.setFont('Helvetica', 8)
    date_range = (f'{invoice.booking.check_in_date.strftime("%d %b %Y")}'
                  f' \u2013 {invoice.booking.check_out_date.strftime("%d %b %Y")}')
    c.drawString(cx[0] + 5, y - 21, date_range)

    c.setFont('Helvetica', 9)
    c.drawCentredString(cx[1] + nights_w / 2, y - 14, str(invoice.booking.nights))
    c.drawRightString(cx[2] + rate_w - 5,     y - 14,
                      f'{invoice.booking.room.price_per_night:.0f}')
    c.setFont('Helvetica-Bold', 9)
    c.drawRightString(cx[3] + amt_w - 5,      y - 14, f'{invoice.subtotal:.0f}')
    y -= 32

    # ── THIN RULE ────────────────────────────────────────────────────────────
    c.setLineWidth(0.5)
    c.line(M, y, W - M, y)
    y -= 15

    # ── TOTALS ───────────────────────────────────────────────────────────────
    tot_label_x = W - M - 135
    tot_value_x = W - M

    def total_row(label, value, bold=False, size=9, color=BLACK):
        nonlocal y
        c.setFillColor(color)
        c.setFont('Helvetica-Bold' if bold else 'Helvetica', size)
        c.drawString(tot_label_x, y, label)
        c.drawRightString(tot_value_x, y, value)
        y -= 13

    total_row('Subtotal', f'MVR {invoice.subtotal:.0f}')

    c.setLineWidth(0.5)
    c.line(tot_label_x, y + 10, tot_value_x, y + 10)
    y -= 2

    total_row('Total', f'MVR {invoice.total_amount:.0f}', bold=True, size=10)
    total_row('Amount Paid', f'MVR {invoice.amount_paid:.0f}')

    if invoice.payment_method and invoice.amount_paid > 0:
        c.setFont('Helvetica', 7)
        c.setFillColor(BLACK)
        c.drawString(tot_label_x, y + 11,
                     f'via {invoice.payment_method.replace("_", " ").title()}')

    c.setStrokeColor(BLACK)
    c.setLineWidth(2)
    c.line(tot_label_x, y + 8, tot_value_x, y + 8)
    y -= 4

    bal_color = RED if invoice.balance_due > 0 else BLACK
    total_row('Balance Due', f'MVR {invoice.balance_due:.0f}',
              bold=True, size=11, color=bal_color)

    y -= 10

    # ── NOTES ────────────────────────────────────────────────────────────────
    if invoice.notes:
        c.setStrokeColor(BLACK)
        c.setLineWidth(0.5)
        c.line(M, y, W - M, y)
        y -= 14
        c.setFont('Helvetica-Bold', 9)
        c.setFillColor(BLACK)
        c.drawString(M, y, 'Notes')
        y -= 12
        c.setFont('Helvetica', 9)
        for line in invoice.notes.replace('\r', '').split('\n'):
            line = line.strip()
            if line:
                c.drawString(M, y, line)
                y -= 11
        y -= 5

    # ── FOOTER ───────────────────────────────────────────────────────────────
    footer_y = M + 28
    c.setStrokeColor(BLACK)
    c.setLineWidth(0.5)
    c.line(M, footer_y + 18, W - M, footer_y + 18)
    c.setFont('Helvetica-Bold', 9)
    c.setFillColor(BLACK)
    c.drawCentredString(W / 2, footer_y + 7,
                        'Thank you for staying at Sheeza Manzil Guesthouse')
    c.setFont('Helvetica', 8)
    c.drawCentredString(W / 2, footer_y - 4,
                        'Maaveyo Magu, Hdh. Hanimaadhoo, Maldives  \u00b7  Tel: +960 737 5797')
    c.setFont('Helvetica', 7)
    c.drawCentredString(W / 2, footer_y - 15, invoice.invoice_number)

    c.save()
    buf.seek(0)
    return buf

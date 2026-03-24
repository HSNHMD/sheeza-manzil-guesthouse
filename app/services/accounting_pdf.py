"""
Generate a monthly P&L report PDF using ReportLab.
Consistent with invoice PDF style (same header, margins, fonts).
"""
import io
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas

DARK = colors.HexColor('#1f2937')
GREEN = colors.HexColor('#16a34a')
RED = colors.HexColor('#dc2626')
GRAY = colors.HexColor('#6b7280')
LIGHT_GRAY = colors.HexColor('#f3f4f6')
BLACK = colors.black


def generate_monthly_report_pdf(year, month, month_label, revenue, expenses_by_cat,
                                 total_expenses, net_profit, generated_on):
    buf = io.BytesIO()
    W, H = A4
    M = 15 * mm
    CW = W - 2 * M

    c = rl_canvas.Canvas(buf, pagesize=A4)
    c.setTitle('')
    y = H - M

    # ── HEADER ──────────────────────────────────────────────────────────────
    c.setFillColor(BLACK)
    c.setFont('Helvetica-Bold', 17)
    c.drawString(M, y - 16, 'Sheeza Manzil Guesthouse')
    c.setFont('Helvetica', 9)
    c.drawString(M, y - 28, 'Maaveyo Magu, Hdh. Hanimaadhoo, Maldives')
    c.drawString(M, y - 39, 'Tel: +960 737 5797')

    c.setFont('Helvetica-Bold', 18)
    c.drawRightString(W - M, y - 16, 'PROFIT & LOSS')
    c.setFont('Helvetica', 9)
    c.drawRightString(W - M, y - 30, f'Period: {month_label}')
    c.drawRightString(W - M, y - 41, f'Generated: {generated_on.strftime("%d %B %Y")}')

    y -= 56

    c.setStrokeColor(BLACK)
    c.setLineWidth(2)
    c.line(M, y, W - M, y)
    y -= 20

    def section_header(label):
        nonlocal y
        c.setFillColor(DARK)
        c.setFont('Helvetica-Bold', 11)
        c.drawString(M, y, label)
        y -= 14
        c.setStrokeColor(GRAY)
        c.setLineWidth(0.5)
        c.line(M, y, W - M, y)
        y -= 8

    def row(label, amount, bold=False, color=BLACK):
        nonlocal y
        font = 'Helvetica-Bold' if bold else 'Helvetica'
        c.setFont(font, 10)
        c.setFillColor(color)
        c.drawString(M + 4, y, label)
        c.drawRightString(W - M, y, f'MVR {amount:,.2f}')
        y -= 14

    def spacer(h=8):
        nonlocal y
        y -= h

    # ── REVENUE SECTION ─────────────────────────────────────────────────────
    section_header('REVENUE')
    row('Room Revenue (Paid Invoices)', revenue, bold=True, color=GREEN)
    spacer()

    # ── EXPENSES SECTION ────────────────────────────────────────────────────
    section_header('EXPENSES')
    if expenses_by_cat:
        for cat, amt in sorted(expenses_by_cat.items()):
            row(cat, amt)
    else:
        c.setFont('Helvetica', 10)
        c.setFillColor(GRAY)
        c.drawString(M + 4, y, 'No expenses recorded for this period.')
        y -= 14
    spacer()
    row('Total Expenses', total_expenses, bold=True, color=RED)
    spacer(12)

    # ── NET P&L ─────────────────────────────────────────────────────────────
    c.setStrokeColor(BLACK)
    c.setLineWidth(1.5)
    c.line(M, y, W - M, y)
    y -= 16

    net_color = GREEN if net_profit >= 0 else RED
    net_label = 'NET PROFIT' if net_profit >= 0 else 'NET LOSS'
    c.setFont('Helvetica-Bold', 13)
    c.setFillColor(net_color)
    c.drawString(M + 4, y, net_label)
    c.drawRightString(W - M, y, f'MVR {abs(net_profit):,.2f}')
    y -= 20

    c.setStrokeColor(BLACK)
    c.setLineWidth(2)
    c.line(M, y, W - M, y)

    # ── FOOTER ──────────────────────────────────────────────────────────────
    c.setFont('Helvetica', 8)
    c.setFillColor(GRAY)
    c.drawCentredString(W / 2, M, 'Sheeza Manzil Guesthouse — Confidential')

    c.save()
    buf.seek(0)
    return buf

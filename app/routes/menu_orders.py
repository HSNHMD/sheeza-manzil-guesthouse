"""Online Menu / QR Ordering V1 — public guest UI + staff queue.

Public endpoints (no login):
  GET  /menu/                        landing menu page
  GET  /menu/room/<room_number>      same menu, room prefilled
  POST /menu/order                   submit order (returns 302 to /menu/order/<token>)
  GET  /menu/order/<public_token>    guest order tracker
  GET  /menu/qr                      printable QR + URL (admin-friendly,
                                     but route itself is public so it
                                     can be opened on any device)

Admin endpoints (login_required + admin_required):
  GET  /menu/admin/                  staff order queue
  GET  /menu/admin/orders/<id>       order detail
  POST /menu/admin/orders/<id>/confirm
  POST /menu/admin/orders/<id>/deliver
  POST /menu/admin/orders/<id>/cancel
  POST /menu/admin/orders/<id>/post-to-folio
  POST /menu/admin/orders/<id>/attach-booking   (manual link)

The public menu uses /menu rather than /book/menu to keep the URL
short for QR posters. Routes are mounted under a single blueprint.
"""

from __future__ import annotations

import json as _json
from datetime import date, datetime, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash, abort,
    jsonify,
)
from flask_login import login_required, current_user

from ..models import (
    db, GuestOrder, GuestOrderItem, Booking, PosCategory, PosItem, Room,
)
from ..decorators import admin_required
from ..services.audit import log_activity
from ..services.menu_orders import (
    public_menu_payload, validate_cart_input, create_order,
    confirm_order, deliver_order, cancel_order, post_to_folio,
    match_booking, ORDER_STATUSES, MENU_FOLIO_ITEM_TYPES,
)


menu_bp = Blueprint('menu', __name__, url_prefix='/menu')


# ── Public menu landing ─────────────────────────────────────────────

@menu_bp.route('/', methods=['GET'])
def index():
    """Mobile-first menu page. Optional ?room=<n>&source=qr in query."""
    payload = public_menu_payload()
    return render_template(
        'menu/public_menu.html',
        categories=payload['categories'],
        items=payload['items'],
        items_json=_json.dumps([
            {'id': i.id, 'name': i.name, 'price': float(i.price),
             'category_id': i.category_id,
             'description': i.description or ''}
            for i in payload['items']
        ]),
        prefill={
            'room_number': (request.args.get('room') or '').strip()[:20],
            'source':      'qr_menu' if request.args.get('source') == 'qr'
                                     else 'guest_menu',
        },
    )


@menu_bp.route('/room/<room_number>', methods=['GET'])
def room_menu(room_number):
    """Tokenless room-prefilled menu for printed QR posters."""
    payload = public_menu_payload()
    return render_template(
        'menu/public_menu.html',
        categories=payload['categories'],
        items=payload['items'],
        items_json=_json.dumps([
            {'id': i.id, 'name': i.name, 'price': float(i.price),
             'category_id': i.category_id,
             'description': i.description or ''}
            for i in payload['items']
        ]),
        prefill={
            'room_number': room_number.strip()[:20],
            'source': 'qr_menu',
        },
    )


# ── Public order submission ─────────────────────────────────────────

@menu_bp.route('/order', methods=['POST'])
def submit_order():
    cart_raw = request.form.get('cart_json') or '[]'
    try:
        cart = _json.loads(cart_raw)
    except (ValueError, TypeError):
        cart = None

    room_number   = (request.form.get('room_number') or '').strip()
    guest_name    = (request.form.get('guest_name')  or '').strip()
    contact_phone = (request.form.get('contact_phone') or '').strip()
    notes         = (request.form.get('notes') or '').strip()
    source        = (request.form.get('source') or 'guest_menu').strip().lower()
    if source not in ('guest_menu', 'qr_menu'):
        source = 'guest_menu'

    errors = []
    if cart is None:
        errors.append('cart payload invalid.')
    else:
        v = validate_cart_input(cart)
        if v['errors']:
            errors.extend(v['errors'])
            cleaned = []
        else:
            cleaned = v['cleaned']

    if errors:
        for e in errors:
            flash('Order: ' + e, 'error')
        return redirect(url_for('menu.index',
                                 room=room_number or None,
                                 source=('qr' if source == 'qr_menu'
                                         else None)))

    result = create_order(
        cleaned_cart=cleaned,
        room_number=room_number or None,
        guest_name=guest_name or None,
        contact_phone=contact_phone or None,
        notes=notes or None,
        source=source,
    )
    if not result['ok']:
        flash('Order: ' + (result['error'] or 'failed'), 'error')
        return redirect(url_for('menu.index'))
    db.session.commit()

    return redirect(url_for('menu.order_status',
                             public_token=result['order'].public_token))


@menu_bp.route('/order/<public_token>', methods=['GET'])
def order_status(public_token):
    order = (GuestOrder.query
             .filter_by(public_token=public_token)
             .first_or_404())
    return render_template(
        'menu/order_status.html',
        order=order,
    )


# ── QR poster page (public; staff prints it) ────────────────────────

@menu_bp.route('/qr', methods=['GET'])
def qr_poster():
    """Renders a printable poster with the menu URL + an SVG QR.

    The QR is generated server-side as a tiny SVG (no external lib —
    a pure-Python implementation in templates/menu/qr_poster.html).
    For the poster URL we use whatever host the request came from so
    staging links to staging and prod links to prod (prod is not
    deployed in V1, but the route is environment-safe by construction).
    """
    target_url = request.host_url.rstrip('/') + url_for('menu.index')
    room = (request.args.get('room') or '').strip()
    if room:
        target_url = (request.host_url.rstrip('/')
                       + url_for('menu.room_menu', room_number=room))
    return render_template(
        'menu/qr_poster.html',
        target_url=target_url,
        room=room or None,
    )


# ── Admin order queue ───────────────────────────────────────────────

@menu_bp.route('/admin/', methods=['GET'])
@login_required
@admin_required
def admin_queue():
    """Staff queue — group by status, newest first."""
    status_filter = (request.args.get('status') or '').strip().lower()
    q = GuestOrder.query.order_by(GuestOrder.created_at.desc())
    if status_filter and status_filter in ORDER_STATUSES:
        q = q.filter(GuestOrder.status == status_filter)
    orders = q.limit(200).all()

    counts = {
        s: GuestOrder.query.filter_by(status=s).count()
        for s in ORDER_STATUSES
    }
    return render_template(
        'menu/admin_queue.html',
        orders=orders,
        counts=counts,
        status_filter=status_filter or None,
    )


@menu_bp.route('/admin/orders/<int:order_id>', methods=['GET'])
@login_required
@admin_required
def admin_order_detail(order_id):
    order = GuestOrder.query.get_or_404(order_id)
    # In-house bookings the staff might attach manually
    today = date.today()
    in_house = (Booking.query
                .filter(Booking.status.in_(
                    ('checked_in', 'confirmed', 'payment_verified',
                     'payment_uploaded')))
                .filter(Booking.check_in_date <= today)
                .filter(Booking.check_out_date >= today)
                .order_by(Booking.check_in_date.desc())
                .limit(60)
                .all())
    return render_template(
        'menu/admin_order_detail.html',
        order=order, in_house=in_house,
    )


@menu_bp.route('/admin/orders/<int:order_id>/confirm', methods=['POST'])
@login_required
@admin_required
def admin_confirm(order_id):
    order = GuestOrder.query.get_or_404(order_id)
    r = confirm_order(order, user=current_user)
    if not r['ok']:
        flash(r['error'], 'error')
    else:
        db.session.commit()
        flash(f'Order #{order.id} confirmed.', 'success')
    return redirect(url_for('menu.admin_order_detail', order_id=order.id))


@menu_bp.route('/admin/orders/<int:order_id>/deliver', methods=['POST'])
@login_required
@admin_required
def admin_deliver(order_id):
    order = GuestOrder.query.get_or_404(order_id)
    r = deliver_order(order, user=current_user)
    if not r['ok']:
        flash(r['error'], 'error')
    else:
        db.session.commit()
        flash(f'Order #{order.id} marked delivered.', 'success')
    return redirect(url_for('menu.admin_order_detail', order_id=order.id))


@menu_bp.route('/admin/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
@admin_required
def admin_cancel(order_id):
    order = GuestOrder.query.get_or_404(order_id)
    reason = (request.form.get('cancel_reason') or '').strip() or None
    r = cancel_order(order, user=current_user, reason=reason)
    if not r['ok']:
        flash(r['error'], 'error')
    else:
        db.session.commit()
        flash(f'Order #{order.id} cancelled.', 'success')
    return redirect(url_for('menu.admin_order_detail', order_id=order.id))


@menu_bp.route('/admin/orders/<int:order_id>/post-to-folio',
               methods=['POST'])
@login_required
@admin_required
def admin_post_to_folio(order_id):
    order = GuestOrder.query.get_or_404(order_id)
    r = post_to_folio(order, user=current_user)
    if not r['ok']:
        flash('Post to folio: ' + r['error'], 'error')
    else:
        db.session.commit()
        flash(
            f'Order #{order.id} posted to folio '
            f'(${r["total"]:.2f}, {len(r["folio_item_ids"])} item'
            f'{"s" if len(r["folio_item_ids"]) != 1 else ""}).',
            'success')
    return redirect(url_for('menu.admin_order_detail', order_id=order.id))


@menu_bp.route('/admin/orders/<int:order_id>/attach-booking',
               methods=['POST'])
@login_required
@admin_required
def admin_attach_booking(order_id):
    order = GuestOrder.query.get_or_404(order_id)
    raw = (request.form.get('booking_id') or '').strip()
    if order.is_posted_to_folio:
        flash('Cannot change booking after order is posted to folio.',
              'error')
        return redirect(url_for('menu.admin_order_detail', order_id=order.id))
    if not raw or raw == '0':
        order.booking_id = None
        flash(f'Order #{order.id}: booking link cleared.', 'success')
    else:
        try:
            bid = int(raw)
        except ValueError:
            flash('Invalid booking id.', 'error')
            return redirect(url_for('menu.admin_order_detail',
                                     order_id=order.id))
        b = Booking.query.get(bid)
        if b is None:
            flash('Booking not found.', 'error')
            return redirect(url_for('menu.admin_order_detail',
                                     order_id=order.id))
        order.booking_id = bid
        flash(f'Order #{order.id}: linked to {b.booking_ref}.', 'success')
    db.session.commit()
    return redirect(url_for('menu.admin_order_detail', order_id=order.id))

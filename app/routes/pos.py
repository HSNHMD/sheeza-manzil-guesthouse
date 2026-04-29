"""POS / F&B V1 — admin CRUD + POS terminal.

Endpoints:

  Admin (admin_required):
    GET  /pos/admin/                          — landing
    GET  /pos/admin/categories                — list
    GET  /pos/admin/categories/new            — form
    POST /pos/admin/categories/new            — create
    GET  /pos/admin/categories/<id>/edit      — form
    POST /pos/admin/categories/<id>/edit      — save
    POST /pos/admin/categories/<id>/toggle    — flip is_active

    GET  /pos/admin/items                     — list
    GET  /pos/admin/items/new                 — form
    POST /pos/admin/items/new                 — create
    GET  /pos/admin/items/<id>/edit           — form
    POST /pos/admin/items/<id>/edit           — save
    POST /pos/admin/items/<id>/toggle         — flip is_active

  Terminal (login_required, staff allowed):
    GET  /pos/                                — main terminal (cart UI)
    GET  /pos/api/bookings                    — JSON booking search
    POST /pos/post                            — submit cart

The terminal is mounted under /pos (NOT /pos/admin) and is whitelisted
in the staff_guard so restaurant/front-office staff can use it.
"""

from __future__ import annotations

import json as _json
from datetime import date, timedelta

from flask import (
    Blueprint, render_template, request, redirect, url_for, flash,
    jsonify,
)
from flask_login import login_required, current_user

from ..models import (
    db, Booking, Guest, Room, PosCategory, PosItem,
)
from ..decorators import admin_required
from ..services.audit import log_activity
from ..services.pos import (
    POS_FOLIO_ITEM_TYPES, validate_cart, post_sale, can_post_to_booking,
    cart_total,
)


pos_bp = Blueprint('pos', __name__, url_prefix='/pos')


# ── Tiny helpers ────────────────────────────────────────────────────

def _form_int(name, default=None):
    raw = (request.values.get(name) or '').strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _form_float(name, default=None):
    raw = (request.values.get(name) or '').strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _form_bool(name):
    return (request.form.get(name) or '').strip().lower() in (
        '1', 'on', 'true', 'yes')


# ── Admin: landing ──────────────────────────────────────────────────

@pos_bp.route('/admin/', methods=['GET'])
@login_required
@admin_required
def admin_overview():
    counts = {
        'categories':       PosCategory.query.count(),
        'active_categories': PosCategory.query.filter_by(is_active=True).count(),
        'items':            PosItem.query.count(),
        'active_items':     PosItem.query.filter_by(is_active=True).count(),
    }
    return render_template('pos/admin_overview.html', counts=counts)


# ── Admin: categories ───────────────────────────────────────────────

@pos_bp.route('/admin/categories', methods=['GET'])
@login_required
@admin_required
def categories_list():
    cats = (PosCategory.query
            .order_by(PosCategory.sort_order, PosCategory.name)
            .all())
    return render_template('pos/categories_list.html', categories=cats)


@pos_bp.route('/admin/categories/new', methods=['GET', 'POST'])
@login_required
@admin_required
def category_new():
    if request.method == 'POST':
        name = (request.form.get('name') or '').strip()
        sort_order = _form_int('sort_order', 100) or 100
        errors = []
        if not name:
            errors.append('name required.')
        if PosCategory.query.filter_by(name=name).first():
            errors.append(f'category {name!r} already exists.')
        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('pos/category_form.html',
                                   category=None,
                                   form=request.form), 400
        cat = PosCategory(name=name, sort_order=sort_order, is_active=True)
        db.session.add(cat)
        db.session.commit()
        flash(f'Category "{cat.name}" created.', 'success')
        return redirect(url_for('pos.categories_list'))
    return render_template('pos/category_form.html',
                           category=None, form={})


@pos_bp.route('/admin/categories/<int:cat_id>/edit',
              methods=['GET', 'POST'])
@login_required
@admin_required
def category_edit(cat_id):
    cat = PosCategory.query.get_or_404(cat_id)
    if request.method == 'POST':
        cat.name = (request.form.get('name') or cat.name).strip()
        cat.sort_order = _form_int('sort_order', cat.sort_order) or cat.sort_order
        cat.is_active = _form_bool('is_active')
        db.session.commit()
        flash(f'Category "{cat.name}" saved.', 'success')
        return redirect(url_for('pos.categories_list'))
    return render_template('pos/category_form.html',
                           category=cat, form={})


@pos_bp.route('/admin/categories/<int:cat_id>/toggle', methods=['POST'])
@login_required
@admin_required
def category_toggle(cat_id):
    cat = PosCategory.query.get_or_404(cat_id)
    cat.is_active = not cat.is_active
    db.session.commit()
    flash(f'Category {cat.name} '
          f'{"activated" if cat.is_active else "deactivated"}.',
          'success')
    return redirect(url_for('pos.categories_list'))


# ── Admin: items ────────────────────────────────────────────────────

@pos_bp.route('/admin/items', methods=['GET'])
@login_required
@admin_required
def items_list():
    items = (PosItem.query
             .order_by(PosItem.is_active.desc(),
                       PosItem.category_id, PosItem.sort_order)
             .all())
    cats = {c.id: c for c in PosCategory.query.all()}
    return render_template('pos/items_list.html',
                           items=items, categories=cats)


@pos_bp.route('/admin/items/new', methods=['GET', 'POST'])
@login_required
@admin_required
def item_new():
    cats = (PosCategory.query
            .filter_by(is_active=True)
            .order_by(PosCategory.name)
            .all())
    if request.method == 'POST':
        category_id = _form_int('category_id')
        name = (request.form.get('name') or '').strip()
        description = (request.form.get('description') or '').strip() or None
        price = _form_float('price', 0.0)
        default_item_type = (request.form.get('default_item_type')
                             or 'restaurant').strip().lower()
        sort_order = _form_int('sort_order', 100) or 100

        errors = []
        if not name:
            errors.append('name required.')
        if category_id is None or PosCategory.query.get(category_id) is None:
            errors.append('valid category required.')
        if price is None or price < 0:
            errors.append('price must be ≥ 0.')
        if default_item_type not in POS_FOLIO_ITEM_TYPES:
            errors.append(
                f'item type must be one of: {", ".join(POS_FOLIO_ITEM_TYPES)}.')

        if errors:
            for e in errors:
                flash(e, 'error')
            return render_template('pos/item_form.html',
                                   item=None, categories=cats,
                                   pos_types=POS_FOLIO_ITEM_TYPES,
                                   form=request.form), 400

        item = PosItem(
            category_id=category_id, name=name, description=description,
            price=round(price, 2), default_item_type=default_item_type,
            sort_order=sort_order, is_active=True,
        )
        db.session.add(item); db.session.commit()
        flash(f'Item "{item.name}" created.', 'success')
        return redirect(url_for('pos.items_list'))
    return render_template('pos/item_form.html',
                           item=None, categories=cats,
                           pos_types=POS_FOLIO_ITEM_TYPES, form={})


@pos_bp.route('/admin/items/<int:item_id>/edit',
              methods=['GET', 'POST'])
@login_required
@admin_required
def item_edit(item_id):
    item = PosItem.query.get_or_404(item_id)
    cats = (PosCategory.query
            .filter_by(is_active=True)
            .order_by(PosCategory.name)
            .all())
    if request.method == 'POST':
        item.name = (request.form.get('name') or item.name).strip()
        item.description = (request.form.get('description') or '').strip() or None
        new_price = _form_float('price', item.price)
        if new_price is not None and new_price >= 0:
            item.price = round(new_price, 2)
        new_type = (request.form.get('default_item_type')
                     or item.default_item_type).strip().lower()
        if new_type in POS_FOLIO_ITEM_TYPES:
            item.default_item_type = new_type
        item.sort_order = _form_int('sort_order', item.sort_order) \
                           or item.sort_order
        item.is_active = _form_bool('is_active')
        cat_id = _form_int('category_id', item.category_id)
        if cat_id and PosCategory.query.get(cat_id):
            item.category_id = cat_id
        db.session.commit()
        flash(f'Item "{item.name}" saved.', 'success')
        return redirect(url_for('pos.items_list'))
    return render_template('pos/item_form.html',
                           item=item, categories=cats,
                           pos_types=POS_FOLIO_ITEM_TYPES, form={})


@pos_bp.route('/admin/items/<int:item_id>/toggle', methods=['POST'])
@login_required
@admin_required
def item_toggle(item_id):
    item = PosItem.query.get_or_404(item_id)
    item.is_active = not item.is_active
    db.session.commit()
    flash(f'Item {item.name} '
          f'{"activated" if item.is_active else "deactivated"}.',
          'success')
    return redirect(url_for('pos.items_list'))


# ── Terminal (staff-allowed) ────────────────────────────────────────

@pos_bp.route('/', methods=['GET'])
@login_required
def terminal():
    """Main POS UI. Cat tabs + item grid + cart sidebar."""
    cats = (PosCategory.query
            .filter_by(is_active=True)
            .order_by(PosCategory.sort_order, PosCategory.name)
            .all())
    items = (PosItem.query
             .filter_by(is_active=True)
             .order_by(PosItem.category_id, PosItem.sort_order, PosItem.name)
             .all())

    # In-house + arriving-soon bookings for the dropdown picker
    today = date.today()
    bookings = (Booking.query
                .filter(Booking.status.in_(
                    ('checked_in', 'confirmed', 'payment_verified',
                     'payment_uploaded', 'pending_payment')))
                .filter(Booking.check_out_date >= today)
                .order_by(Booking.check_in_date.desc())
                .limit(80)
                .all())

    return render_template(
        'pos/terminal.html',
        categories=cats, items=items,
        bookings=bookings,
        # JSON shapes embedded directly so the JS can build the cart
        items_json=_json.dumps([
            {'id': i.id, 'name': i.name, 'price': float(i.price),
             'category_id': i.category_id,
             'item_type': i.default_item_type}
            for i in items
        ]),
        bookings_json=_json.dumps([
            {'id': b.id, 'ref': b.booking_ref,
             'guest': b.guest.full_name if b.guest else '?',
             'room':  b.room.number if b.room else '?',
             'status': b.status,
             'check_in':  b.check_in_date.isoformat(),
             'check_out': b.check_out_date.isoformat()}
            for b in bookings
        ]),
    )


@pos_bp.route('/post', methods=['POST'])
@login_required
def post():
    """Atomic submit of a cart.

    Form:
      booking_id        — int, REQUIRED
      mode              — 'room' | 'pay_now'
      payment_method    — required when mode='pay_now'
      reference_number  — optional
      sale_note         — optional, ≤ 500 chars
      cart_json         — JSON array: [{pos_item_id, qty,
                          price_override?, note?}, ...]
    """
    booking_id = _form_int('booking_id')
    mode = (request.form.get('mode') or '').strip().lower() or 'room'
    sale_note = (request.form.get('sale_note') or '').strip() or None
    payment_method = (request.form.get('payment_method') or '').strip() or None
    reference_number = (request.form.get('reference_number') or '').strip() \
                        or None

    cart_raw = request.form.get('cart_json') or '[]'
    try:
        cart = _json.loads(cart_raw)
        if not isinstance(cart, list):
            raise ValueError
    except (ValueError, TypeError):
        cart = None

    booking = Booking.query.get(booking_id) if booking_id else None

    # ── Validate ──
    errors = []
    if booking is None:
        errors.append('booking is required.')
    else:
        e = can_post_to_booking(booking)
        if e:
            errors.append(e)
    if cart is None:
        errors.append('cart payload invalid.')
    else:
        v = validate_cart(cart)
        if v['errors']:
            errors.extend(v['errors'])
            cleaned = []
        else:
            cleaned = v['cleaned']
    if mode not in ('room', 'pay_now'):
        errors.append(f'unknown mode {mode!r}.')
    if mode == 'pay_now' and not payment_method:
        errors.append('payment_method required for pay_now.')

    if errors:
        # The audit metadata sanitizer keeps only scalar values, so we
        # join the reasons into a single short string for the audit row
        # (and a flash for the operator).
        reasons_str = ' | '.join(errors[:5])[:240]
        log_activity(
            'pos.sale_failed',
            actor_user_id=getattr(current_user, 'id', None),
            booking=booking,
            description='POS submit rejected: ' + '; '.join(errors[:3]),
            metadata={
                'booking_id':  booking.id if booking else None,
                'booking_ref': booking.booking_ref if booking else None,
                'mode':        mode,
                'item_count':  len(cart) if isinstance(cart, list) else 0,
                'reasons':     reasons_str,
                'source_module': 'pos',
            },
        )
        db.session.commit()
        for e in errors:
            flash('POS: ' + e, 'error')
        return redirect(url_for('pos.terminal'))

    # ── Post sale ──
    result = post_sale(
        booking=booking,
        cleaned_cart=cleaned,
        mode=mode,
        cashier_user=current_user,
        sale_note=sale_note,
        payment_method=payment_method,
        reference_number=reference_number,
    )

    if not result['ok']:
        db.session.rollback()
        log_activity(
            'pos.sale_failed',
            actor_user_id=getattr(current_user, 'id', None),
            booking=booking,
            description=f'POS sale failed: {result["error"]}',
            metadata={
                'booking_id':  booking.id,
                'booking_ref': booking.booking_ref,
                'mode':        mode,
                'reason':      result['error'],
                'source_module': 'pos',
            },
        )
        db.session.commit()
        flash('POS: ' + result['error'], 'error')
        return redirect(url_for('pos.terminal'))

    db.session.commit()

    if mode == 'pay_now':
        flash(
            f'Sale paid · {result["total"]:.2f} settled to '
            f'booking {booking.booking_ref}.', 'success')
    else:
        flash(
            f'Posted {len(cleaned)} item'
            f'{"s" if len(cleaned) != 1 else ""} '
            f'({result["total"]:.2f}) to booking {booking.booking_ref}.',
            'success')
    return redirect(url_for('pos.terminal'))


# ── JSON booking search (used by terminal autocomplete) ─────────────

@pos_bp.route('/api/bookings', methods=['GET'])
@login_required
def api_bookings():
    q = (request.args.get('q') or '').strip().lower()
    today = date.today()
    query = (Booking.query
             .join(Guest)
             .join(Room)
             .filter(Booking.status.in_(
                 ('checked_in', 'confirmed', 'payment_verified',
                  'payment_uploaded', 'pending_payment')))
             .filter(Booking.check_out_date >= today))
    if q:
        like = f'%{q}%'
        query = query.filter(
            (Booking.booking_ref.ilike(like)) |
            (Guest.first_name.ilike(like)) |
            (Guest.last_name.ilike(like)) |
            (Room.number.ilike(like))
        )
    rows = query.order_by(Booking.check_in_date.desc()).limit(20).all()
    return jsonify({
        'bookings': [
            {'id': b.id, 'ref': b.booking_ref,
             'guest': b.guest.full_name if b.guest else '?',
             'room':  b.room.number if b.room else '?',
             'status': b.status,
             'check_in':  b.check_in_date.isoformat(),
             'check_out': b.check_out_date.isoformat()}
            for b in rows
        ],
    })

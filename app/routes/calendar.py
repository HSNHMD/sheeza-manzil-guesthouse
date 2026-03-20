import calendar
from datetime import date, timedelta
from flask import Blueprint, render_template, request
from flask_login import login_required
from ..models import Room, Booking
from ..utils import hotel_date

calendar_bp = Blueprint('cal', __name__, url_prefix='/calendar')


@calendar_bp.route('/')
@login_required
def index():
    today = hotel_date()
    year = int(request.args.get('year', today.year))
    month = int(request.args.get('month', today.month))

    # Clamp
    if month < 1:
        month, year = 12, year - 1
    elif month > 12:
        month, year = 1, year + 1

    # First and last day of displayed month
    first_day = date(year, month, 1)
    last_day = date(year, month, calendar.monthrange(year, month)[1])

    # All active rooms ordered by number
    rooms = sorted(Room.query.filter_by(is_active=True).all(), key=lambda r: int(r.number))

    # All bookings overlapping this month
    bookings = Booking.query.filter(
        Booking.status.in_(['confirmed', 'checked_in', 'checked_out']),
        Booking.check_in_date <= last_day,
        Booking.check_out_date > first_day
    ).all()

    # Build a lookup: (room_id, day) -> booking
    cell = {}  # (room_id, day_num) -> {'status': ..., 'booking': ...}
    for b in bookings:
        # iterate over each night of the booking that falls in this month
        d = max(b.check_in_date, first_day)
        end = min(b.check_out_date - timedelta(days=1), last_day)
        while d <= end:
            if d.month == month:
                cell[(b.room_id, d.day)] = b
            d += timedelta(days=1)

    days_in_month = last_day.day
    day_range = list(range(1, days_in_month + 1))
    # Weekday abbreviations for each day: 0=Mon … 6=Sun
    weekdays = ['Mo','Tu','We','Th','Fr','Sa','Su']
    day_weekdays = {d: weekdays[date(year, month, d).weekday()] for d in day_range}
    weekend_days = {d for d in day_range if date(year, month, d).weekday() >= 5}

    # Prev / next month links
    prev_month = month - 1 or 12
    prev_year = year - 1 if month == 1 else year
    next_month = month + 1 if month < 12 else 1
    next_year = year + 1 if month == 12 else year

    month_name = first_day.strftime('%B %Y')

    return render_template('calendar/index.html',
                           rooms=rooms, day_range=day_range,
                           cell=cell, month=month, year=year,
                           today=today, month_name=month_name,
                           timedelta=timedelta,
                           day_weekdays=day_weekdays,
                           weekend_days=weekend_days,
                           prev_month=prev_month, prev_year=prev_year,
                           next_month=next_month, next_year=next_year)

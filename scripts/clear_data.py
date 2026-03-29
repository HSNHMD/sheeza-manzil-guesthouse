"""
One-time script: clears all transactional data from the database.
Keeps: rooms, users (staff accounts).
Clears: bookings, guests, invoices, housekeeping_logs, bank_transactions, expenses.
Resets: room.status → 'vacant', room.housekeeping_status → 'clean', room.notes → None.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, db
from app.models import Booking, Invoice, Guest, HousekeepingLog, BankTransaction, Expense, Room

app = create_app()
with app.app_context():
    # counts before
    before = {
        'bookings': Booking.query.count(),
        'guests': Guest.query.count(),
        'invoices': Invoice.query.count(),
        'housekeeping_logs': HousekeepingLog.query.count(),
        'bank_transactions': BankTransaction.query.count(),
        'expenses': Expense.query.count(),
        'rooms': Room.query.count(),
    }
    print("=== BEFORE ===")
    for k, v in before.items():
        print(f"  {k}: {v}")

    # Delete in FK-safe order
    HousekeepingLog.query.delete()
    BankTransaction.query.delete()
    Expense.query.delete()
    Invoice.query.delete()
    Booking.query.delete()
    Guest.query.delete()

    # Reset room state
    Room.query.update({
        'status': 'vacant',
        'housekeeping_status': 'clean',
        'notes': None,
    })

    db.session.commit()

    print("=== AFTER ===")
    print(f"  bookings: {Booking.query.count()}")
    print(f"  guests: {Guest.query.count()}")
    print(f"  invoices: {Invoice.query.count()}")
    print(f"  housekeeping_logs: {HousekeepingLog.query.count()}")
    print(f"  bank_transactions: {BankTransaction.query.count()}")
    print(f"  expenses: {Expense.query.count()}")
    print(f"  rooms: {Room.query.count()} (kept, status reset to vacant/clean)")
    print("=== DONE ===")

"""Run to reset and populate rooms: python seed_data.py"""
from app import create_app
from app.models import db, Room, User

app = create_app()

ROOMS = [
    # (number, name,         type,          floor, capacity, price)
    ('1', 'Deluxe Double',   'Deluxe',       0,     2,        600.0),
    ('2', 'Deluxe Double',   'Deluxe',       0,     2,        600.0),
    ('3', 'Deluxe Double',   'Deluxe',       0,     2,        600.0),
    ('4', 'Deluxe Double',   'Deluxe',       0,     2,        600.0),
    ('5', 'Deluxe Double',   'Deluxe',       1,     2,        600.0),
    ('6', 'Deluxe Double',   'Deluxe',       1,     2,        600.0),
    ('7', 'Twin Room',       'Twin',         1,     2,        600.0),
    ('8', 'Twin Room',       'Twin',         0,     2,        600.0),
]

with app.app_context():
    # Remove all existing rooms (soft-delete bypassed — hard delete for clean seed)
    Room.query.delete()
    db.session.commit()

    for number, name, rtype, floor, cap, price in ROOMS:
        room = Room(
            number=number, name=name, room_type=rtype,
            floor=floor, capacity=cap, price_per_night=price,
            amenities='WiFi, AC, TV, En-suite Bathroom'
        )
        db.session.add(room)

    db.session.commit()
    print(f"Seeded {Room.query.count()} rooms.")
    for r in Room.query.order_by(Room.number).all():
        floor_label = 'Ground Floor' if r.floor == 0 else f'Floor {r.floor}'
        print(f"  Room {r.number} — {r.name} ({r.room_type}) · {floor_label} · MVR {r.price_per_night:.0f}/night")
    print("\nLogin: admin / admin123")

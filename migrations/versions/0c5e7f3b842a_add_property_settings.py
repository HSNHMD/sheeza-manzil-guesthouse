"""add Property Settings / Branding Foundation V1

Revision ID: 0c5e7f3b842a
Revises: f9a4b8d2c531
Create Date: 2026-04-30 16:00:00.000000

Creates the property_settings singleton table and seeds the row from
the same defaults the existing brand-context env reader (services/
branding.py) uses. The values shipped here ARE NOT secrets; they
mirror the existing public-facing branding so a fresh staging install
keeps working unchanged. Operators edit them via /admin/property-
settings.

V1 is single-property. Multi-property tenancy is deferred (see
docs/channel_manager_architecture.md §10).

Hand-written. Touches no other tables. Downgrade reverses cleanly.
"""
from datetime import datetime

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import column, table


revision = '0c5e7f3b842a'
down_revision = 'f9a4b8d2c531'
branch_labels = None
depends_on = None


def upgrade():
    settings_t = op.create_table(
        'property_settings',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),

        # Branding
        sa.Column('property_name', sa.String(160), nullable=False),
        sa.Column('short_name',    sa.String(80),  nullable=True),
        sa.Column('tagline',       sa.String(255), nullable=True),
        sa.Column('logo_path',     sa.String(255), nullable=True),
        sa.Column('primary_color', sa.String(16),  nullable=True),
        sa.Column('website_url',   sa.String(255), nullable=True),

        # Contact
        sa.Column('email',           sa.String(120), nullable=True),
        sa.Column('phone',           sa.String(40),  nullable=True),
        sa.Column('whatsapp_number', sa.String(40),  nullable=True),
        sa.Column('address',         sa.String(255), nullable=True),
        sa.Column('city',            sa.String(80),  nullable=True),
        sa.Column('country',         sa.String(80),  nullable=True),

        # Operational
        sa.Column('currency_code', sa.String(8), nullable=False,
                  server_default='USD'),
        sa.Column('timezone', sa.String(64), nullable=False,
                  server_default='Indian/Maldives'),
        sa.Column('check_in_time',  sa.String(8), nullable=True),
        sa.Column('check_out_time', sa.String(8), nullable=True),

        # Billing
        sa.Column('invoice_display_name',     sa.String(160), nullable=True),
        sa.Column('payment_instructions_text', sa.Text(),     nullable=True),
        sa.Column('bank_name',                sa.String(120), nullable=True),
        sa.Column('bank_account_name',        sa.String(120), nullable=True),
        sa.Column('bank_account_number',      sa.String(60),  nullable=True),

        # Tax basics
        sa.Column('tax_name',            sa.String(40), nullable=True),
        sa.Column('tax_rate',            sa.Float(),    nullable=True),
        sa.Column('service_charge_rate', sa.Float(),    nullable=True),

        # Policies
        sa.Column('booking_terms',       sa.Text(), nullable=True),
        sa.Column('cancellation_policy', sa.Text(), nullable=True),
        sa.Column('wifi_info',           sa.Text(), nullable=True),

        # Lifecycle
        sa.Column('is_active', sa.Boolean(), nullable=False,
                  server_default=sa.true()),
    )

    # Seed the singleton row. Defaults intentionally match the existing
    # branding fall-backs in services/branding.py and the constants in
    # services/payment_instructions.py so a fresh upgrade keeps every
    # public page rendering exactly as it did before.
    now = datetime.utcnow()
    op.bulk_insert(settings_t, [{
        'created_at': now,
        'updated_at': now,
        'property_name': 'Sheeza Manzil Guesthouse',
        'short_name':    'Sheeza Manzil',
        'tagline':       '',
        'logo_path':     '/static/img/logo.png',
        'primary_color': '#7B3F00',
        'website_url':   None,

        'email':           None,
        'phone':           '+960 737 5797',
        'whatsapp_number': '+960 737 5797',
        'address':         'Maaveyo Magu, Hdh. Hanimaadhoo',
        'city':            'Hanimaadhoo',
        'country':         'Maldives',

        'currency_code':   'USD',
        'timezone':        'Indian/Maldives',
        'check_in_time':   '14:00',
        'check_out_time':  '11:00',

        'invoice_display_name':     'Sheeza Manzil Guesthouse',
        'payment_instructions_text': (
            'Bank Transfer Details\n'
            '\n'
            'Account Name: SHEEZA IMAD/MOHAMED S.R.\n'
            'Account Number: 7770000212622\n'
            '\n'
            'Please send the payment slip after transfer so we can '
            'verify your booking.'
        ),
        'bank_name':           'Bank of Maldives (BML)',
        'bank_account_name':   'SHEEZA IMAD/MOHAMED S.R.',
        'bank_account_number': '7770000212622',

        'tax_name':            None,
        'tax_rate':            None,
        'service_charge_rate': None,

        'booking_terms':       None,
        'cancellation_policy': None,
        'wifi_info':           None,

        'is_active': True,
    }])


def downgrade():
    op.drop_table('property_settings')

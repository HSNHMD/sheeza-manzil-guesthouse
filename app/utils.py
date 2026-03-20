from datetime import datetime, date, timedelta, timezone

# Maldives is UTC+5
_MVT = timezone(timedelta(hours=5))


def hotel_date() -> date:
    """
    Return the current 'hotel date' using a 3 AM rollover.

    Between midnight and 02:59 Maldives time the night-shift is still
    considered part of the previous calendar day, so we return yesterday.
    """
    now_mv = datetime.now(_MVT)
    if now_mv.hour < 3:
        return (now_mv - timedelta(days=1)).date()
    return now_mv.date()

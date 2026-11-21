import datetime
from zoneinfo import ZoneInfo
from django.utils import timezone
from rest_framework.exceptions import ValidationError

KOLKATA_TZ = ZoneInfo('Asia/Kolkata')


def resolve_date_range(query_params: dict):
    """
    Parses date filter parameters into start_datetime (inclusive) and end_datetime (inclusive).
    Supports:
      - filter_type: 'today', 'yesterday', 'this_week', 'this_month', 'last_month', 'this_year', 'custom'
      - from_date (YYYY-MM-DD), to_date (YYYY-MM-DD)
    Boundaries are converted to UTC datetime representations of Asia/Kolkata local dates.
    Returns:
      {
        'filter_type': str,
        'from_date': datetime.date,
        'to_date': datetime.date,
        'start_dt': datetime.datetime (UTC),
        'end_dt': datetime.datetime (UTC),
        'label': str,
      }
    """
    now_kolkata = timezone.now().astimezone(KOLKATA_TZ)
    today = now_kolkata.date()

    filter_type = (query_params.get('filter_type') or query_params.get('date_filter') or '').lower().strip()
    from_date_str = query_params.get('from_date')
    to_date_str = query_params.get('to_date')

    # If from_date or to_date explicitly supplied without explicit filter_type, treat as custom
    if (from_date_str or to_date_str) and not filter_type:
        filter_type = 'custom'

    if not filter_type:
        filter_type = 'last_30_days'

    if filter_type == 'last_30_days':
        start_date = today - datetime.timedelta(days=29)
        end_date = today
        label = f"Last 30 Days ({start_date.isoformat()} to {end_date.isoformat()})"
    elif filter_type == 'last_3_months':
        start_date = today - datetime.timedelta(days=89)
        end_date = today
        label = f"Last 3 Months ({start_date.isoformat()} to {end_date.isoformat()})"
    elif filter_type == 'today':
        start_date = today
        end_date = today
        label = f"Today ({today.isoformat()})"
    elif filter_type == 'yesterday':
        start_date = today - datetime.timedelta(days=1)
        end_date = start_date
        label = f"Yesterday ({start_date.isoformat()})"
    elif filter_type == 'this_week':
        # Monday to today/Sunday
        start_date = today - datetime.timedelta(days=today.weekday())
        end_date = today
        label = f"This Week ({start_date.isoformat()} to {end_date.isoformat()})"
    elif filter_type == 'this_month':
        start_date = today.replace(day=1)
        end_date = today
        label = f"This Month ({start_date.strftime('%B %Y')})"
    elif filter_type == 'last_month':
        first_this_month = today.replace(day=1)
        last_day_last_month = first_this_month - datetime.timedelta(days=1)
        start_date = last_day_last_month.replace(day=1)
        end_date = last_day_last_month
        label = f"Last Month ({start_date.strftime('%B %Y')})"
    elif filter_type == 'this_year':
        start_date = today.replace(month=1, day=1)
        end_date = today
        label = f"This Year ({today.year})"
    elif filter_type == 'custom':
        if not from_date_str:
            raise ValidationError({'from_date': "from_date (YYYY-MM-DD) is required for custom filter."})
        try:
            start_date = datetime.date.fromisoformat(from_date_str)
        except ValueError:
            raise ValidationError({'from_date': "Invalid date format. Expected YYYY-MM-DD."})

        if to_date_str:
            try:
                end_date = datetime.date.fromisoformat(to_date_str)
            except ValueError:
                raise ValidationError({'to_date': "Invalid date format. Expected YYYY-MM-DD."})
        else:
            end_date = today

        if start_date > end_date:
            raise ValidationError({'from_date': "from_date cannot be later than to_date."})

        # Cap max query range to 5 years to protect server memory
        if (end_date - start_date).days > (365 * 5):
            raise ValidationError({'detail': "Date range cannot exceed 5 years."})

        label = f"Custom ({start_date.isoformat()} to {end_date.isoformat()})"
    else:
        raise ValidationError({
            'filter_type': f"Invalid filter_type '{filter_type}'. Allowed: last_30_days, last_month, last_3_months, today, yesterday, this_week, this_month, this_year, custom."
        })

    # Convert local calendar days into UTC datetime boundaries
    start_dt_local = datetime.datetime.combine(start_date, datetime.time.min, tzinfo=KOLKATA_TZ)
    end_dt_local = datetime.datetime.combine(end_date, datetime.time.max, tzinfo=KOLKATA_TZ)

    start_dt_utc = start_dt_local.astimezone(datetime.timezone.utc)
    end_dt_utc = end_dt_local.astimezone(datetime.timezone.utc)

    return {
        'filter_type': filter_type,
        'from_date': start_date,
        'to_date': end_date,
        'start_dt': start_dt_utc,
        'end_dt': end_dt_utc,
        'label': label,
    }

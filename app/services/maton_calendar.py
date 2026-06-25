"""Maton Google Calendar service — fetches meetings via Maton API proxy."""

import os
import json
import urllib.request
import urllib.error
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

MATON_API_KEY = os.environ.get('MATON_API_KEY', '')
MATON_BASE = 'https://api.maton.ai/google-calendar/calendar/v3'

# Simple in-memory cache with 5-minute expiration
_cache = {}
_cache_expiry = {}
CACHE_TTL = 300  # 5 minutes in seconds

def _get_from_cache(key):
    """Get data from cache if not expired."""
    if key in _cache and key in _cache_expiry:
        if datetime.now(timezone.utc).timestamp() < _cache_expiry[key]:
            logger.info(f'Cache HIT for {key}')
            return _cache[key]
        else:
            # Expired, remove from cache
            del _cache[key]
            del _cache_expiry[key]
            logger.info(f'Cache EXPIRED for {key}')
    return None

def _set_cache(key, value):
    """Store data in cache with expiration."""
    _cache[key] = value
    _cache_expiry[key] = datetime.now(timezone.utc).timestamp() + CACHE_TTL
    logger.info(f'Cache SET for {key} (expires in {CACHE_TTL}s)')


def _api_get(path, params=None):
    """Make a Maton-proxied Google Calendar API call."""
    if not MATON_API_KEY:
        raise ValueError('MATON_API_KEY not configured')

    url = f'{MATON_BASE}/{path}'
    if params:
        import urllib.parse
        url += '?' + urllib.parse.urlencode(params)

    for attempt in range(3):
        try:
            req = urllib.request.Request(url)
            req.add_header('Authorization', f'Bearer {MATON_API_KEY}')
            with urllib.request.urlopen(req, timeout=8) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:500]
            logger.error(f'Maton Calendar API error {e.code}: {body}')
            if e.code == 429:
                import time
                time.sleep(int(e.headers.get('Retry-After', '5')))
                continue
            raise
        except Exception as e:
            logger.error(f'Maton Calendar request failed: {e}')
            if attempt < 2:
                import time
                time.sleep(1)
                continue
            raise

    raise Exception('Max retries exceeded for Maton Calendar API')


def _parse_event(event):
    """Parse a Google Calendar event into our meeting format."""
    start = event.get('start', {})
    end = event.get('end', {})

    # Get start time
    start_time = start.get('dateTime') or start.get('date')
    end_time = end.get('dateTime') or end.get('date')

    # Extract company/client info from description
    description = event.get('description', '') or ''
    company = ''
    client_name = ''

    # Try to find company/client in description
    import re
    # Patterns like "Company: X" or "Client: X" or "with X from Y"
    company_match = re.search(r'[Cc]ompany:\s*(.+?)(?:\n|$)', description)
    client_match = re.search(r'[Cc]lient:\s*(.+?)(?:\n|$)', description)
    with_match = re.search(r'[Ww]ith\s+(.+?)\s+(?:from|at)\s+(.+)', description)

    if company_match:
        company = company_match.group(1).strip()
    if client_match:
        client_name = client_match.group(1).strip()
    if not client_name and with_match:
        client_name = with_match.group(1).strip()
        company = with_match.group(2).strip() if not company else company

    # Attendees
    attendees = []
    for att in event.get('attendees', []):
        attendees.append({
            'name': att.get('displayName', att.get('email', '')),
            'email': att.get('email', ''),
            'status': att.get('responseStatus', 'unknown'),
            'organizer': att.get('organizer', False),
        })

    # Organizer
    organizer = event.get('organizer', {}).get('email', '') if event.get('organizer') else ''

    # Location / conference
    location = event.get('location', '')
    conference_data = event.get('conferenceData', {})
    conference_link = ''
    if conference_data:
        entry_points = conference_data.get('entryPoints', [])
        for ep in entry_points:
            if ep.get('entryPointType') == 'video':
                conference_link = ep.get('uri', '')
                break

    return {
        'id': event.get('id', ''),
        'title': event.get('summary', 'Untitled Meeting'),
        'description': description,
        'start': start_time,
        'end': end_time,
        'timezone': start.get('timeZone', 'UTC'),
        'status': event.get('status', 'confirmed'),
        'company': company,
        'client_name': client_name,
        'location': location,
        'conference_link': conference_link,
        'organizer': organizer,
        'attendees': attendees,
        'html_link': event.get('htmlLink', ''),
    }


def get_events(days_back=7, days_ahead=14, max_results=50, calendar_id='primary'):
    """Fetch calendar events from Google Calendar via Maton.
    
    Args:
        days_back: How many days in the past to include
        days_ahead: How many days in the future to include
        max_results: Maximum number of events
        calendar_id: Calendar ID (default: 'primary')
    """
    # Check cache first
    cache_key = f'events_{days_back}_{days_ahead}_{max_results}_{calendar_id}'
    cached = _get_from_cache(cache_key)
    if cached:
        return cached
    
    now = datetime.now(timezone.utc)
    time_min = (now - timedelta(days=days_back)).strftime('%Y-%m-%dT%H:%M:%SZ')
    time_max = (now + timedelta(days=days_ahead)).strftime('%Y-%m-%dT%H:%M:%SZ')

    params = {
        'timeMin': time_min,
        'timeMax': time_max,
        'singleEvents': 'true',
        'orderBy': 'startTime',
        'maxResults': str(max_results),
    }

    data = _api_get(f'calendars/{calendar_id}/events', params)
    events = data.get('items', [])

    parsed = [_parse_event(e) for e in events]

    # Separate into upcoming and past
    upcoming = [e for e in parsed if e['start'] >= now.isoformat()]
    past = [e for e in parsed if e['start'] < now.isoformat()]

    result = {
        'calendar_name': data.get('summary', ''),
        'timezone': data.get('timeZone', 'UTC'),
        'total': len(events),
        'upcoming': upcoming,
        'upcoming_count': len(upcoming),
        'past': past,
        'past_count': len(past),
    }
    
    # Cache the result
    _set_cache(cache_key, result)
    
    return result


def get_upcoming_only(days_ahead=2, max_results=10, calendar_id='primary'):
    """Fetch only today + N days ahead — minimal window for fast loads."""
    # Check cache first
    cache_key = f'upcoming_{days_ahead}_{max_results}_{calendar_id}'
    cached = _get_from_cache(cache_key)
    if cached:
        return cached
    
    now = datetime.now(timezone.utc)
    time_min = now.strftime('%Y-%m-%dT%H:%M:%SZ')
    time_max = (now + timedelta(days=days_ahead)).replace(
        hour=23, minute=59, second=59
    ).strftime('%Y-%m-%dT%H:%M:%SZ')

    params = {
        'timeMin': time_min,
        'timeMax': time_max,
        'singleEvents': 'true',
        'orderBy': 'startTime',
        'maxResults': str(max_results),
    }

    data = _api_get(f'calendars/{calendar_id}/events', params)
    events = data.get('items', [])
    parsed = [_parse_event(e) for e in events]

    result = {
        'calendar_name': data.get('summary', ''),
        'timezone': data.get('timeZone', 'UTC'),
        'total': len(parsed),
        'upcoming': parsed,
        'upcoming_count': len(parsed),
        'past': [],
        'past_count': 0,
    }
    
    # Cache the result
    _set_cache(cache_key, result)
    
    return result


def get_meetings_weekly():
    """Get this week's meetings — past 3 days and next 7 days."""
    return get_events(days_back=3, days_ahead=7, max_results=50)
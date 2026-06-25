"""Dashboard API — Clean minimal SDR dashboard with HubSpot deals and LinkedIn metrics only.

GET /api/dashboard/summary — returns only:
- HubSpot deals analytics
- LinkedIn connections sent/accepted
- LinkedIn profile views
- LinkedIn DMs sent
- LinkedIn DMs interested
"""

import logging
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from app.services.supabase import supabase, select, select_one, insert, update, delete, eq, gte, lte, in_
from app.services.maton_calendar import get_events, get_upcoming_only

logger = logging.getLogger(__name__)

dashboard_bp = Blueprint('dashboard', __name__)


def _get_user(user_id_str):
    try:
        return select_one('users', filters=[eq('id', int(user_id_str))])
    except (ValueError, TypeError):
        return None


# ── Helpers ────────────────────────────────────────────────────────────────────

def _today_start():
    """Start of today in UTC."""
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _today_end():
    return datetime.now(timezone.utc).replace(hour=23, minute=59, second=59, microsecond=999999)


def _days_ago(n):
    return datetime.now(timezone.utc) - timedelta(days=n)


# ── HubSpot Deals Analytics ────────────────────────────────────────────────────

def _build_hubspot_deals(workspace_id):
    """Fetch HubSpot deals analytics from hubspot_deals table."""
    try:
        deals_result = supabase.table('hubspot_deals').select('*').eq('workspace_id', workspace_id).execute()
        deals = deals_result.data

        if not deals:
            return {
                'total_deals': 0,
                'total_value': 0,
                'deals_by_stage': [],
                'recent_deals': []
            }

        # Calculate totals
        total_deals = len(deals)
        total_value = sum(float(d.get('amount') or 0) for d in deals)

        # Group by stage
        stage_map = {}
        for d in deals:
            stage = d.get('stage') or 'Unknown'
            if stage not in stage_map:
                stage_map[stage] = {'count': 0, 'value': 0.0}
            stage_map[stage]['count'] += 1
            stage_map[stage]['value'] += float(d.get('amount') or 0)

        deals_by_stage = [
            {
                'stage': stage,
                'count': data['count'],
                'value': data['value'],
            }
            for stage, data in stage_map.items()
        ]
        deals_by_stage.sort(key=lambda x: x['value'], reverse=True)

        # Recent deals (last 10)
        recent_deals = sorted(deals, key=lambda x: x.get('created_at', ''), reverse=True)[:10]
        recent_deals_formatted = [
            {
                'id': d.get('deal_id'),
                'name': d.get('deal_name'),
                'stage': d.get('stage'),
                'amount': float(d.get('amount') or 0),
                'close_date': d.get('close_date'),
                'created_at': d.get('created_at'),
            }
            for d in recent_deals
        ]

        return {
            'total_deals': total_deals,
            'total_value': total_value,
            'deals_by_stage': deals_by_stage,
            'recent_deals': recent_deals_formatted,
        }

    except Exception as e:
        logger.warning(f'HubSpot deals unavailable: {e}')
        return {
            'total_deals': 0,
            'total_value': 0,
            'deals_by_stage': [],
            'recent_deals': []
        }


# ── LinkedIn Analytics ─────────────────────────────────────────────────────────

def _build_linkedin_analytics(workspace_id, user_id):
    """Calculate LinkedIn metrics from linkedin_activities table."""
    try:
        # Fetch all LinkedIn activities for this workspace/user
        activities_result = supabase.table('linkedin_activities').select('*').eq('workspace_id', workspace_id).eq('user_id', user_id).execute()
        activities = activities_result.data

        if not activities:
            return {
                'connections_sent': 0,
                'connections_accepted': 0,
                'profile_views': 0,
                'dms_sent': 0,
                'dms_interested': 0,
                'recent_activities': [],
                'reply_rate': 0,
                'acceptance_rate': 0,
                'today_activity': {
                    'connections_sent': 0,
                    'dms_sent': 0,
                    'profile_views': 0
                },
                'weekly_trend': [],
                'total_engagement': 0
            }

        # Count by activity type
        connections_sent = len([a for a in activities if a.get('activity_type') == 'connection_sent'])
        connections_accepted = len([a for a in activities if a.get('activity_type') == 'connection_accepted'])
        profile_views = len([a for a in activities if a.get('activity_type') == 'profile_viewed'])
        dms_sent = len([a for a in activities if a.get('activity_type') == 'dm_sent'])
        dms_interested = len([a for a in activities if a.get('activity_type') == 'interested'])
        reply_received = len([a for a in activities if a.get('activity_type') == 'reply_received'])

        # Calculate rates
        reply_rate = round((dms_interested + reply_received) / dms_sent * 100, 1) if dms_sent > 0 else 0.0
        acceptance_rate = round(connections_accepted / connections_sent * 100, 1) if connections_sent > 0 else 0.0

        # Today's activity
        today_start = _today_start().isoformat()
        today_activities = [a for a in activities if a.get('created_at', '') >= today_start]
        today_connections = len([a for a in today_activities if a.get('activity_type') == 'connection_sent'])
        today_dms = len([a for a in today_activities if a.get('activity_type') == 'dm_sent'])
        today_views = len([a for a in today_activities if a.get('activity_type') == 'profile_viewed'])

        # Weekly trend (last 7 days)
        weekly_trend = []
        for days_back in range(6, -1, -1):
            day_start = (_days_ago(days_back)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
            day_end = (_days_ago(days_back)).replace(hour=23, minute=59, second=59, microsecond=999999).isoformat()
            day_activities = [a for a in activities if day_start <= a.get('created_at', '') <= day_end]
            
            weekly_trend.append({
                'date': day_start[:10],
                'connections': len([a for a in day_activities if a.get('activity_type') == 'connection_sent']),
                'dms': len([a for a in day_activities if a.get('activity_type') == 'dm_sent']),
                'views': len([a for a in day_activities if a.get('activity_type') == 'profile_viewed']),
                'replies': len([a for a in day_activities if a.get('activity_type') in ['reply_received', 'interested']])
            })

        # Total engagement score
        total_engagement = connections_accepted + (dms_interested * 2) + (reply_received * 2) + profile_views

        # Recent activities (last 15)
        recent_activities = sorted(activities, key=lambda x: x.get('created_at', ''), reverse=True)[:15]
        recent_activities_formatted = [
            {
                'type': a.get('activity_type'),
                'lead_name': a.get('lead_name'),
                'company': a.get('company'),
                'notes': a.get('notes'),
                'created_at': a.get('created_at'),
            }
            for a in recent_activities
        ]

        return {
            'connections_sent': connections_sent,
            'connections_accepted': connections_accepted,
            'profile_views': profile_views,
            'dms_sent': dms_sent,
            'dms_interested': dms_interested,
            'reply_received': reply_received,
            'reply_rate': reply_rate,
            'acceptance_rate': acceptance_rate,
            'today_activity': {
                'connections_sent': today_connections,
                'dms_sent': today_dms,
                'profile_views': today_views
            },
            'weekly_trend': weekly_trend,
            'total_engagement': total_engagement,
            'recent_activities': recent_activities_formatted,
        }

    except Exception as e:
        logger.warning(f'LinkedIn analytics unavailable: {e}')
        return {
            'connections_sent': 0,
            'connections_accepted': 0,
            'profile_views': 0,
            'dms_sent': 0,
            'dms_interested': 0,
            'reply_received': 0,
            'reply_rate': 0,
            'acceptance_rate': 0,
            'today_activity': {
                'connections_sent': 0,
                'dms_sent': 0,
                'profile_views': 0
            },
            'weekly_trend': [],
            'total_engagement': 0,
            'recent_activities': []
        }


# ── Main Endpoint ──────────────────────────────────────────────────────────────

@dashboard_bp.route('/api/dashboard/summary', methods=['GET'])
def dashboard_summary():
    """Clean minimal dashboard - HubSpot deals + LinkedIn metrics only."""
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    
    # Get user
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        # Create a default user context for unauthenticated requests
        user = {
            'id': current_user_id,
            'name': 'Guest User',
            'workspace_id': 1,
            'email': 'guest@example.com',
            'role': 'sdr'
        }

    workspace_id = user['workspace_id']

    # Build only the metrics you need - FAST response without meetings
    hubspot_deals = {}
    linkedin_analytics = {}

    try:
        hubspot_deals = _build_hubspot_deals(workspace_id)
    except Exception as e:
        logger.warning(f'Failed to build HubSpot deals: {e}')

    try:
        linkedin_analytics = _build_linkedin_analytics(workspace_id, user['id'])
    except Exception as e:
        logger.warning(f'Failed to build LinkedIn analytics: {e}')

    # Return immediately without meetings for fast load
    # Meetings will be loaded separately by the meetings tab
    return jsonify({
        'hubspot_deals': hubspot_deals,
        'linkedin_analytics': linkedin_analytics,
        'maton_meetings': [],  # Empty - meetings tab loads separately
        'user': {
            'name': user.get('name', 'Guest User'),
            'email': user.get('email', 'guest@example.com'),
            'role': user.get('role', 'sdr'),
        }
    })
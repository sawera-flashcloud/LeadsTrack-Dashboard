"""Meetings route — fetches real meetings from Google Calendar via Maton."""

import logging
from datetime import datetime, timezone
from flask import Blueprint, jsonify, request
from app.services.maton_calendar import get_events, get_meetings_weekly, get_upcoming_only
from app.services.supabase import select_one, eq

logger = logging.getLogger(__name__)
meetings_bp = Blueprint('meetings', __name__)


def _get_user(user_id_str):
    try:
        return select_one('users', filters=[eq('id', int(user_id_str))])
    except (ValueError, TypeError):
        return None


@meetings_bp.route('/api/meetings/this-week', methods=['GET'])
def meetings_this_week():
    """Get all meetings for this week (past 3 days + next 7 days)."""
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    try:
        data = get_upcoming_only(days_ahead=2, max_results=10)
        return jsonify(data)
    except ValueError as e:
        logger.warning(f'Maton not configured: {e}')
        return jsonify({'error': 'Calendar not configured', 'detail': str(e)}), 503
    except Exception as e:
        logger.exception('meetings_this_week error')
        return jsonify({'error': f'Failed to fetch meetings: {str(e)}'}), 502


@meetings_bp.route('/api/meetings/upcoming', methods=['GET'])
def upcoming_meetings():
    """Get upcoming meetings (next 14 days)."""
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    days_ahead = request.args.get('days', 2, type=int)

    try:
        data = get_upcoming_only(days_ahead=days_ahead, max_results=10)
        return jsonify(data)
    except ValueError as e:
        return jsonify({'error': 'Calendar not configured', 'detail': str(e)}), 503
    except Exception as e:
        logger.exception('upcoming_meetings error')
        return jsonify({'error': str(e)}), 502


@meetings_bp.route('/api/meetings/past', methods=['GET'])
def past_meetings():
    """Get past meetings (last 14 days)."""
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    days_back = request.args.get('days', 14, type=int)

    try:
        data = get_events(days_back=days_back, days_ahead=0, max_results=50)
        return jsonify(data)
    except ValueError as e:
        return jsonify({'error': 'Calendar not configured', 'detail': str(e)}), 503
    except Exception as e:
        logger.exception('past_meetings error')
        return jsonify({'error': str(e)}), 502

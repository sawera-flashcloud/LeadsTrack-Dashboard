import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify
from app.services.supabase import supabase, select, select_one, insert, update, delete, eq, in_

logger = logging.getLogger(__name__)
linkedin_bp = Blueprint('linkedin', __name__)


def _get_current_user(user_id_str):
    try:
        return select_one('users', filters=[eq('id', int(user_id_str))])
    except (ValueError, TypeError):
        return None


def _activity_to_dict(a):
    return {
        'id': a.get('id'),
        'workspace_id': a.get('workspace_id'),
        'user_id': a.get('user_id'),
        'lead_name': a.get('lead_name', ''),
        'company': a.get('company', ''),
        'linkedin_url': a.get('linkedin_url', ''),
        'activity_type': a.get('activity_type', ''),
        'activity_date': a.get('activity_date', ''),
        'notes': a.get('notes', ''),
        'source': a.get('source', 'manual'),
        'created_at': a.get('created_at'),
    }


@linkedin_bp.route('/api/linkedin/activities', methods=['GET'])
def list_activities():
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 25, type=int)
    activity_type = request.args.get('type', '', type=str)
    source = request.args.get('source', '', type=str)

    per_page = min(per_page, 100)

    q = supabase.table('linkedin_activities').select('*', count='exact').eq('workspace_id', user['workspace_id'])

    if activity_type:
        q = q.eq('activity_type', activity_type)
    if source:
        q = q.eq('source', source)

    q = q.order('created_at', desc=True)

    # Get total count
    count_q = supabase.table('linkedin_activities').select('id', count='exact').eq('workspace_id', user['workspace_id'])
    if activity_type:
        count_q = count_q.eq('activity_type', activity_type)
    if source:
        count_q = count_q.eq('source', source)
    count_result = count_q.execute()
    total = int(count_result.count) if hasattr(count_result, 'count') else 0

    # Paginate
    offset_val = (page - 1) * per_page
    q = q.limit(per_page).offset(offset_val)
    result = q.execute()
    activities = result.data

    return jsonify({
        'activities': [_activity_to_dict(a) for a in activities],
        'total': total,
        'page': page,
        'pages': max(1, -(-total // per_page)) if total else 1,
    })


@linkedin_bp.route('/api/linkedin/activities', methods=['POST'])
def create_activity():
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    now = datetime.now(timezone.utc).isoformat()
    activity_data = {
        'workspace_id': user['workspace_id'],
        'user_id': user['id'],
        'lead_name': data.get('lead_name', ''),
        'company': data.get('company', ''),
        'linkedin_url': data.get('linkedin_url', ''),
        'activity_type': data.get('activity_type', 'dm_sent'),
        'activity_date': data.get('activity_date', ''),
        'notes': data.get('notes', ''),
        'source': data.get('source', 'manual'),
        'created_at': now,
    }

    result = insert('linkedin_activities', activity_data)
    created = result.data[0] if result.data else activity_data

    return jsonify({'activity': _activity_to_dict(created)}), 201


@linkedin_bp.route('/api/linkedin/activities/<int:activity_id>', methods=['PUT'])
def update_activity(activity_id):
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    activity = select_one('linkedin_activities', filters=[eq('id', activity_id), eq('workspace_id', user['workspace_id'])])
    if not activity:
        return jsonify({'error': 'Activity not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    allowed_fields = [
        'lead_name', 'company', 'linkedin_url', 'activity_type',
        'activity_date', 'notes', 'source',
    ]

    update_data = {}
    for field in allowed_fields:
        if field in data:
            update_data[field] = data[field]

    update('linkedin_activities', update_data, filters=[eq('id', activity_id)])

    updated = select_one('linkedin_activities', filters=[eq('id', activity_id)])

    return jsonify({'activity': _activity_to_dict(updated)})


@linkedin_bp.route('/api/linkedin/activities/<int:activity_id>', methods=['DELETE'])
def delete_activity(activity_id):
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    activity = select_one('linkedin_activities', filters=[eq('id', activity_id), eq('workspace_id', user['workspace_id'])])
    if not activity:
        return jsonify({'error': 'Activity not found'}), 404

    delete('linkedin_activities', filters=[eq('id', activity_id)])

    return jsonify({'success': True})


@linkedin_bp.route('/api/linkedin/activities/batch-delete', methods=['POST'])
def batch_delete_activities():
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json() or {}
    ids = data.get('ids', [])
    if not ids:
        return jsonify({'error': 'No IDs provided'}), 400

    # Validate and coerce IDs to integers
    try:
        int_ids = [int(i) for i in ids]
    except (ValueError, TypeError):
        return jsonify({'error': 'Invalid ID format'}), 400

    workspace_id = user['workspace_id']
    try:
        # Single IN query scoped to the user's workspace — no N+1 loop
        supabase.table('linkedin_activities') \
            .delete() \
            .in_('id', int_ids) \
            .eq('workspace_id', workspace_id) \
            .execute()
    except Exception as e:
        logger.error(f"Batch delete failed: {e}")
        return jsonify({'error': 'Delete failed', 'detail': str(e)}), 500

    return jsonify({'success': True, 'deleted': len(int_ids)})


@linkedin_bp.route('/api/linkedin/stats', methods=['GET'])
def get_linkedin_stats():
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    activities_result = supabase.table('linkedin_activities').select('activity_type').eq('workspace_id', user['workspace_id']).execute()
    activities = activities_result.data

    total = len(activities)
    accepted = sum(1 for a in activities if a.get('activity_type') == 'connection_accepted')
    dms = sum(1 for a in activities if a.get('activity_type') == 'dm_sent')
    replies = sum(1 for a in activities if a.get('activity_type') == 'reply_received')
    meetings = sum(1 for a in activities if a.get('activity_type') == 'meeting_booked')
    interested = sum(1 for a in activities if a.get('activity_type') == 'interested')
    connections_sent = sum(1 for a in activities if a.get('activity_type') == 'connection_sent')

    return jsonify({
        'total': total,
        'connections_sent': connections_sent,
        'accepted': accepted,
        'dms': dms,
        'replies': replies,
        'meetings': meetings,
        'interested': interested,
        'accept_rate': round(accepted / connections_sent * 100, 1) if connections_sent > 0 else 0,
        'reply_rate': round(replies / dms * 100, 1) if dms > 0 else 0,
        'meeting_rate': round(meetings / (replies + dms) * 100, 1) if (replies + dms) > 0 else 0,
    })

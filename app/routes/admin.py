import logging
import time

from flask import Blueprint, request, jsonify
from app.services.supabase import supabase, select, select_one, eq

logger = logging.getLogger(__name__)

admin_bp = Blueprint('admin', __name__)

_start_time = time.time()


def _get_current_user(user_id_str):
    try:
        return select_one('users', filters=[eq('id', int(user_id_str))])
    except (ValueError, TypeError):
        return None


@admin_bp.route('/api/admin/users', methods=['GET'])
def list_users():
    current_user_id = None
    current_user = _get_current_user(current_user_id)
    if not current_user:
        return jsonify({'error': 'User not found'}), 404

    if current_user.get('role') not in ('admin', 'manager'):
        return jsonify({'error': 'Admin access required'}), 403

    users_result = supabase.table('users').select('*').eq('workspace_id', current_user['workspace_id']).execute()
    users = users_result.data

    return jsonify({
        'users': [{
            'id': u['id'],
            'workspace_id': u['workspace_id'],
            'name': u['name'],
            'email': u['email'],
            'role': u['role'],
            'avatar': u.get('avatar', ''),
            'created_at': u.get('created_at'),
        } for u in users],
        'total': len(users),
    })


@admin_bp.route('/api/admin/system-health', methods=['GET'])
def system_health():
    """Public healthcheck endpoint. No auth required."""
    try:
        result = supabase.table('users').select('id', count='exact').execute()
        db_ok = True
        user_count = int(result.count) if hasattr(result, 'count') and result.count else len(result.data)
    except Exception as e:
        logger.warning(f"Health check DB query failed: {e}")
        db_ok = False
        user_count = None

    groq_ok = False
    try:
        from app.services.scoring import get_groq_client
        groq_ok = get_groq_client() is not None
    except Exception:
        pass

    uptime_seconds = int(time.time() - _start_time)

    return jsonify({
        'status': 'healthy' if db_ok else 'degraded',
        'database': db_ok,
        'groq': groq_ok,
        'uptime': f'{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m',
        'uptime_seconds': uptime_seconds,
        'user_count': user_count,
    })


@admin_bp.route('/api/admin/sdr-performance', methods=['GET'])
def sdr_performance():
    """Return real SDR performance metrics from Supabase."""
    current_user_id = request.headers.get('X-User-ID', '1')
    user = _get_current_user(current_user_id)
    if not user:
        return jsonify({'error': 'User not found'}), 404

    workspace_id = user['workspace_id']
    try:
        users_result = supabase.table('users').select('id,name,email,role').eq('workspace_id', workspace_id).execute()
        users = users_result.data or []
    except Exception as e:
        logger.error(f"SDR performance query failed: {e}")
        return jsonify({'error': 'Failed to fetch team data'}), 500

    sdrs = []
    for u in users:
        uid = u['id']
        try:
            leads = supabase.table('leads').select('id', count='exact').eq('workspace_id', workspace_id).eq('user_id', uid).execute()
            leads_count = int(leads.count) if hasattr(leads, 'count') and leads.count else len(leads.data)

            emails = supabase.table('email_activities').select('id', count='exact').eq('workspace_id', workspace_id).eq('user_id', uid).execute()
            emails_count = int(emails.count) if hasattr(emails, 'count') and emails.count else len(emails.data)

            meetings = supabase.table('linkedin_activities').select('id', count='exact').eq('workspace_id', workspace_id).eq('user_id', uid).eq('activity_type', 'meeting_booked').execute()
            meetings_count = int(meetings.count) if hasattr(meetings, 'count') and meetings.count else len(meetings.data)

            conversion = round(meetings_count / leads_count * 100, 1) if leads_count > 0 else 0

            last_activity = supabase.table('linkedin_activities').select('created_at').eq('workspace_id', workspace_id).eq('user_id', uid).order('created_at', desc=True).limit(1).execute()
            from datetime import datetime, timezone, timedelta
            active = False
            if last_activity.data:
                last_ts = last_activity.data[0].get('created_at', '')
                if last_ts:
                    try:
                        last_dt = datetime.fromisoformat(last_ts.replace('Z', '+00:00'))
                        active = (datetime.now(timezone.utc) - last_dt) < timedelta(days=3)
                    except Exception:
                        pass

            sdrs.append({
                'name': u.get('name') or u.get('email', 'Unknown'),
                'leads_found': leads_count,
                'emails_sent': emails_count,
                'meetings': meetings_count,
                'conversion': conversion,
                'active': active,
            })
        except Exception as e:
            logger.warning(f"Failed to fetch stats for user {uid}: {e}")

    return jsonify({'sdrs': sdrs})

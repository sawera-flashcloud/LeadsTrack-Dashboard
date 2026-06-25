import csv
import io
import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, Response
from app.services.supabase import supabase, select, select_one, insert, update, delete, eq, like, in_
from app.services.dedup import is_duplicate_lead
from app.services.scoring import score_lead_via_groq, enrich_lead_via_groq

leads_bp = Blueprint('leads', __name__)
logger = logging.getLogger(__name__)


def _get_current_user(user_id_str):
    """Get user dict from Supabase by JWT identity string."""
    try:
        return select_one('users', filters=[eq('id', int(user_id_str))])
    except (ValueError, TypeError):
        return None


def _lead_to_dict(lead):
    """Convert a raw Supabase lead row to the expected dict format (same as old Lead.to_dict)."""
    return {
        'id': lead.get('id'),
        'workspace_id': lead.get('workspace_id'),
        'user_id': lead.get('user_id'),
        'first_name': lead.get('first_name', ''),
        'last_name': lead.get('last_name', ''),
        'email': lead.get('email', ''),
        'company': lead.get('company', ''),
        'job_title': lead.get('job_title', ''),
        'phone': lead.get('phone', ''),
        'linkedin_url': lead.get('linkedin_url', ''),
        'website': lead.get('website', ''),
        'industry': lead.get('industry', ''),
        'location': lead.get('location', ''),
        'company_size': lead.get('company_size', ''),
        'source': lead.get('source', 'manual'),
        'lead_score': lead.get('lead_score', 0),
        'status': lead.get('status', 'new'),
        'icp_match': lead.get('icp_match', 0.0),
        'score_reason': lead.get('score_reason', ''),
        'enriched_at': lead.get('enriched_at'),
        'created_at': lead.get('created_at'),
        'updated_at': lead.get('updated_at'),
    }


@leads_bp.route('/api/leads', methods=['GET'])
def list_leads():
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
    search = request.args.get('search', '', type=str)
    source = request.args.get('source', '', type=str)
    status = request.args.get('status', '', type=str)
    min_score = request.args.get('min_score', 0, type=int)

    per_page = min(per_page, 100)

    filters = [eq('workspace_id', user['workspace_id'])]

    if search:
        search_term = f'%{search}%'
        # Use OR-like approach: fetch all workspace leads and filter in Python
        # since Supabase REST doesn't support OR natively in the simple API
        pass

    if source:
        filters.append(eq('source', source))

    if status:
        filters.append(eq('status', status))

    if min_score > 0:
        filters.append(('lead_score', 'gte', min_score))

    # Build query
    query = supabase.table('leads').select('*', count='exact')

    # Apply workspace filter
    query = query.eq('workspace_id', user['workspace_id'])

    if source:
        query = query.eq('source', source)
    if status:
        query = query.eq('status', status)
    if min_score > 0:
        query = query.gte('lead_score', min_score)

    query = query.order('updated_at', desc=True)

    # With search, fetch all and filter in Python (OR querying across columns)
    if search:
        # Fetch all leads for this workspace to search across columns
        all_query = supabase.table('leads').select('*', count='exact').eq('workspace_id', user['workspace_id']).order('updated_at', desc=True)
        all_result = all_query.execute()
        all_leads = all_result.data
        total = len(all_leads)
        search_lower = search.lower()
        filtered = [
            l for l in all_leads
            if search_lower in (l.get('first_name') or '').lower()
            or search_lower in (l.get('last_name') or '').lower()
            or search_lower in (l.get('email') or '').lower()
            or search_lower in (l.get('company') or '').lower()
            or search_lower in (l.get('job_title') or '').lower()
        ]
        total_filtered = len(filtered)
        # Apply pagination
        start = (page - 1) * per_page
        end = start + per_page
        leads = filtered[start:end]

        return jsonify({
            'leads': [_lead_to_dict(l) for l in leads],
            'total': total_filtered,
            'page': page,
            'pages': max(1, -(-total_filtered // per_page)),
            'per_page': per_page,
        })

    # No search — use paginated query
    offset_val = (page - 1) * per_page
    query = query.limit(per_page).offset(offset_val)
    result = query.execute()
    leads = result.data
    total = result.count if hasattr(result, 'count') else 0

    return jsonify({
        'leads': [_lead_to_dict(l) for l in leads],
        'total': total,
        'page': page,
        'pages': max(1, -(-total // per_page)) if total else 1,
        'per_page': per_page,
    })


@leads_bp.route('/api/leads', methods=['POST'])
def create_lead():
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

    # Check for duplicate by email
    if data.get('email'):
        existing = select_one('leads', filters=[eq('email', data['email'].strip().lower())])
        if existing:
            return jsonify({'error': 'Lead with this email already exists', 'lead': _lead_to_dict(existing)}), 409

    now = datetime.now(timezone.utc).isoformat()
    lead_data = {
        'workspace_id': user['workspace_id'],
        'user_id': user['id'],
        'first_name': data.get('first_name', ''),
        'last_name': data.get('last_name', ''),
        'email': data.get('email', '').strip().lower() if data.get('email') else '',
        'company': data.get('company', ''),
        'job_title': data.get('job_title', ''),
        'phone': data.get('phone', ''),
        'linkedin_url': data.get('linkedin_url', ''),
        'website': data.get('website', ''),
        'industry': data.get('industry', ''),
        'location': data.get('location', ''),
        'company_size': data.get('company_size', ''),
        'source': data.get('source', 'manual'),
        'lead_score': data.get('lead_score', 0),
        'status': data.get('status', 'new'),
        'created_at': now,
        'updated_at': now,
    }

    result = insert('leads', lead_data)
    created = result.data[0] if result.data else lead_data
    created['id'] = created.get('id')

    return jsonify({'lead': _lead_to_dict(created)}), 201


@leads_bp.route('/api/leads/bulk', methods=['POST'])
def bulk_create_leads():
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json()
    if not data or not isinstance(data, list):
        return jsonify({'error': 'Expected array of lead objects'}), 400

    # Fetch existing leads for dedup
    existing_result = supabase.table('leads').select('*').eq('workspace_id', user['workspace_id']).execute()
    existing_dicts = existing_result.data

    saved = 0
    errors = 0
    now = datetime.now(timezone.utc).isoformat()

    for lead_data_entry in data:
        try:
            # Skip duplicates
            is_dup, matched_on = is_duplicate_lead(lead_data_entry, existing_dicts)
            if is_dup:
                errors += 1
                continue

            new_lead = {
                'workspace_id': user['workspace_id'],
                'user_id': user['id'],
                'first_name': lead_data_entry.get('first_name', ''),
                'last_name': lead_data_entry.get('last_name', ''),
                'email': lead_data_entry.get('email', '').strip().lower() if lead_data_entry.get('email') else '',
                'company': lead_data_entry.get('company', ''),
                'job_title': lead_data_entry.get('job_title', ''),
                'phone': lead_data_entry.get('phone', ''),
                'linkedin_url': lead_data_entry.get('linkedin_url', ''),
                'website': lead_data_entry.get('website', ''),
                'industry': lead_data_entry.get('industry', ''),
                'location': lead_data_entry.get('location', ''),
                'company_size': lead_data_entry.get('company_size', ''),
                'source': lead_data_entry.get('source', 'manual'),
                'lead_score': lead_data_entry.get('lead_score', 0),
                'status': lead_data_entry.get('status', 'new'),
                'created_at': now,
                'updated_at': now,
            }
            result = insert('leads', new_lead)
            saved += 1
        except Exception as e:
            errors += 1
            logger.error(f'Bulk lead error: {str(e)}')

    return jsonify({'saved': saved, 'errors': errors}), 201


@leads_bp.route('/api/leads/<int:lead_id>', methods=['PUT'])
def update_lead(lead_id):
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    lead = select_one('leads', filters=[eq('id', lead_id), eq('workspace_id', user['workspace_id'])])
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404

    data = request.get_json()
    if not data:
        return jsonify({'error': 'No data provided'}), 400

    allowed_fields = [
        'first_name', 'last_name', 'email', 'company', 'job_title', 'phone',
        'linkedin_url', 'website', 'industry', 'location', 'company_size',
        'source', 'lead_score', 'status', 'icp_match', 'score_reason',
    ]

    update_data = {}
    for field in allowed_fields:
        if field in data:
            update_data[field] = data[field]

    update_data['updated_at'] = datetime.now(timezone.utc).isoformat()

    try:
        update('leads', update_data, filters=[eq('id', lead_id)])
    except Exception as e:
        logger.error(f"Failed to update lead {lead_id}: {e}")
        return jsonify({'error': 'Failed to update lead'}), 500

    # Fetch updated lead
    updated_lead = select_one('leads', filters=[eq('id', lead_id)])
    if not updated_lead:
        return jsonify({'error': 'Lead not found after update'}), 404

    return jsonify({'lead': _lead_to_dict(updated_lead)})


@leads_bp.route('/api/leads/<int:lead_id>', methods=['GET'])
def get_lead(lead_id):
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404
    lead = select_one('leads', filters=[eq('id', lead_id), eq('workspace_id', user['workspace_id'])])
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404
    return jsonify(lead)


@leads_bp.route('/api/leads/<int:lead_id>', methods=['DELETE'])
def delete_lead(lead_id):
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    lead = select_one('leads', filters=[eq('id', lead_id), eq('workspace_id', user['workspace_id'])])
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404

    # Delete related records first (respect foreign keys)
    delete('email_activities', filters=[eq('lead_id', lead_id)])
    delete('lead_activities', filters=[eq('lead_id', lead_id)])
    delete('meetings', filters=[eq('lead_id', lead_id)])

    delete('leads', filters=[eq('id', lead_id)])

    return jsonify({'success': True})


@leads_bp.route('/api/leads/batch-delete', methods=['POST'])
def batch_delete_leads():
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json()
    lead_ids = data.get('ids', []) if data else []
    if not lead_ids:
        return jsonify({'error': 'No lead IDs provided'}), 400

    for lid in lead_ids:
        delete('email_activities', filters=[eq('lead_id', lid)])
        delete('lead_activities', filters=[eq('lead_id', lid)])
        delete('meetings', filters=[eq('lead_id', lid)])
        delete('leads', filters=[eq('id', lid)])

    return jsonify({'success': True, 'deleted': len(lead_ids)})


@leads_bp.route('/api/leads/hunt', methods=['POST'])
def hunt_leads():
    """Lead hunting endpoint using real data sources (Apollo, Hunter, Serper, Firecrawl)."""
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json() or {}
    icp = data.get('icp', {})
    limit = min(data.get('limit', 50), 200)
    sources = data.get('sources', [])
    min_score = data.get('min_score', 60)

    if not sources:
        return jsonify({'error': 'At least one source is required'}), 400

    # Run real hunt across selected sources
    from app.services.lead_sources import run_hunt
    try:
        found_leads = run_hunt(sources, icp, limit=limit)
    except Exception as e:
        logger.exception('hunt_leads')
        return jsonify({'error': f'Hunt failed: {str(e)}'}), 502

    # Pre-load existing emails in this workspace to avoid UNIQUE constraint failures
    existing_emails = set()
    emails_result = supabase.table('leads').select('email').neq('email', '').not_.is_('email', 'null').eq('workspace_id', user['workspace_id']).execute()
    for row in emails_result.data:
        em = row.get('email')
        if em:
            existing_emails.add(em.strip().lower())

    saved_leads = []
    errors = 0
    now = datetime.now(timezone.utc).isoformat()

    for ld in found_leads:
        try:
            # Skip if missing basic info
            if not ld.get('company') and not ld.get('first_name'):
                errors += 1
                continue

            # Skip if email already in DB
            lead_email = ld.get('email') or None
            if lead_email and lead_email.strip().lower() in existing_emails:
                errors += 1
                continue
            if lead_email:
                existing_emails.add(lead_email.strip().lower())

            new_lead = {
                'workspace_id': user['workspace_id'],
                'user_id': user['id'],
                'first_name': ld.get('first_name', ''),
                'last_name': ld.get('last_name', ''),
                'email': lead_email or '',
                'company': ld.get('company', ''),
                'job_title': ld.get('job_title', ''),
                'phone': ld.get('phone', ''),
                'linkedin_url': ld.get('linkedin_url', ''),
                'website': ld.get('website', ''),
                'industry': ld.get('industry', ''),
                'location': ld.get('location', ''),
                'company_size': ld.get('company_size', ''),
                'source': ld.get('source', 'unknown'),
                'lead_score': ld.get('lead_score', 0),
                'status': 'new',
                'created_at': now,
                'updated_at': now,
            }
            result = insert('leads', new_lead)
            saved_row = result.data[0] if result.data else new_lead
            saved_leads.append(_lead_to_dict(saved_row if isinstance(saved_row, dict) else new_lead))
        except Exception as e:
            logger.warning(f'Hunt save error: {e}')
            errors += 1

    return jsonify({
        'status': 'completed',
        'leads_found': len(saved_leads),
        'leads': saved_leads,
    })


@leads_bp.route('/api/leads/enrich', methods=['POST'])
def enrich_lead():
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json() or {}
    lead_id = data.get('lead_id')

    if not lead_id:
        return jsonify({'error': 'lead_id is required'}), 400

    lead = select_one('leads', filters=[eq('id', lead_id), eq('workspace_id', user['workspace_id'])])
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404

    # Enrich using Groq (or fallback)
    enriched_data = enrich_lead_via_groq(_lead_to_dict(lead))

    # Update lead with any new data
    update_data = {}
    for field in ['industry', 'company_size', 'location', 'company', 'job_title']:
        if enriched_data.get(field) and not lead.get(field):
            update_data[field] = enriched_data[field]

    now = datetime.now(timezone.utc).isoformat()
    if update_data:
        update_data['enriched_at'] = now
        update_data['updated_at'] = now
        update('leads', update_data, filters=[eq('id', lead_id)])

    updated_lead = select_one('leads', filters=[eq('id', lead_id)])

    return jsonify({'lead': _lead_to_dict(updated_lead)})


@leads_bp.route('/api/leads/score', methods=['POST'])
def score_lead():
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json() or {}
    lead_id = data.get('lead_id')
    icp = data.get('icp', {})

    if not lead_id:
        return jsonify({'error': 'lead_id is required'}), 400

    lead = select_one('leads', filters=[eq('id', lead_id), eq('workspace_id', user['workspace_id'])])
    if not lead:
        return jsonify({'error': 'Lead not found'}), 404

    result = score_lead_via_groq(_lead_to_dict(lead), icp)

    now = datetime.now(timezone.utc).isoformat()
    update('leads', {
        'lead_score': result['score'],
        'icp_match': result['icp_match'],
        'score_reason': result['reason'],
        'updated_at': now,
    }, filters=[eq('id', lead_id)])

    updated_lead = select_one('leads', filters=[eq('id', lead_id)])

    return jsonify({
        'score': result['score'],
        'reason': result['reason'],
        'icp_match': result['icp_match'],
        'lead': _lead_to_dict(updated_lead),
    })


@leads_bp.route('/api/leads/export', methods=['GET'])
def export_leads():
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    fmt = request.args.get('format', 'csv')

    result = supabase.table('leads').select('*').eq('workspace_id', user['workspace_id']).order('created_at', desc=True).execute()
    leads = result.data

    if fmt == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'ID', 'First Name', 'Last Name', 'Email', 'Company', 'Job Title',
            'Phone', 'LinkedIn URL', 'Website', 'Industry', 'Location',
            'Company Size', 'Source', 'Score', 'Status', 'ICP Match',
            'Created At', 'Updated At'
        ])
        for lead in leads:
            writer.writerow([
                lead.get('id'), lead.get('first_name', ''), lead.get('last_name', ''), lead.get('email', ''),
                lead.get('company', ''), lead.get('job_title', ''), lead.get('phone', ''), lead.get('linkedin_url', ''),
                lead.get('website', ''), lead.get('industry', ''), lead.get('location', ''), lead.get('company_size', ''),
                lead.get('source', ''), lead.get('lead_score', 0), lead.get('status', ''), lead.get('icp_match', ''),
                lead.get('created_at', '') or '',
                lead.get('updated_at', '') or '',
            ])

        csv_content = output.getvalue()
        return Response(
            csv_content,
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=leads_export.csv'},
        )

    return jsonify({'leads': [_lead_to_dict(l) for l in leads]})

import json
import logging
from datetime import datetime, timezone

from flask import Blueprint, request, jsonify, Response, stream_with_context

from app.services.supabase import supabase, select, select_one, insert, update, delete, eq
from app.services.scoring import get_groq_client, score_lead_via_groq
from app.services.hubspot_service import build_context_summary, search_owner_by_name, get_deals_for_owner, HubSpotError

logger = logging.getLogger(__name__)

ai_bp = Blueprint('ai', __name__)


@ai_bp.route('/api/ai/chat', methods=['POST'])
def ai_chat():
    """Streaming AI chat endpoint using Groq SSE."""
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    
    # Make user check optional - allow unauthenticated access
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        # Create a default user context for unauthenticated requests
        user = {
            'id': current_user_id,
            'name': 'Guest User',
            'workspace_id': 1
        }

    data = request.get_json() or {}
    messages = data.get('messages', [])

    if not messages:
        return jsonify({'error': 'Messages array is required'}), 400

    # System prompt for Lead Hunter
    system_prompt = {
        'role': 'system',
        'content': (
            'You are Lead Hunter AI, an expert sales assistant. You help SDRs and sales teams '
            'find leads, craft outreach emails, analyze ICP fit, and provide sales strategy advice. '
            'Be concise, actionable, and data-driven. When asked to write emails, personalize them. '
            'When analyzing leads, focus on buying signals. You can help with cold email copy, '
            'LinkedIn messages, follow-up sequences, ICP definitions, and outreach strategy.'
        ),
    }

    # Check for Gmail-specific queries — fetch real emails and inject as context
    import re
    import os
    import urllib.request, urllib.parse
    from datetime import datetime, timedelta, timezone

    gmail_query = None
    user_msg = messages[-1]['content'] if messages else ''
    user_msg_lower = user_msg.lower()

    # Detect Gmail-related requests
    is_gmail_request = any(p in user_msg_lower for p in ['my last', 'my emails', 'my gmail', 'my emails from gmail', 'gmail emails', 'recent emails', 'show my inbox', 'last 5 emails', 'my email'])
    is_linkedin_request = any(p in user_msg_lower for p in ['linkedin', 'li activity', 'activity tracking', 'linkedin notification'])

    if is_gmail_request or is_linkedin_request:
        limit_match = re.search(r'last (\d+)', user_msg_lower)
        limit = int(limit_match.group(1)) if limit_match else 10

        # Detect date range: "last X days" or "7 days"
        days_match = re.search(r'(\d+)\s*days?', user_msg_lower)
        days = int(days_match.group(1)) if days_match else None

        # Build Gmail search query
        search_terms = []
        if is_linkedin_request:
            search_terms.append('linkedin OR notification@linkedin OR linkedin.com OR connection OR "connection accepted" OR "profile view" OR "message reply" OR "new message"')
        if days:
            after_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y/%m/%d')
            search_terms.append(f'after:{after_date}')

        gmail_query_str = ' '.join(search_terms) if search_terms else None

        # Use LinkedIn-specific API key for LinkedIn requests, otherwise use regular MATON key
        if is_linkedin_request:
            maton_key = os.environ.get('LinkedIn_Maton_API_Key', '')
        else:
            maton_key = os.environ.get('MATON_API_KEY', '')
        
        if maton_key:
            try:
                import base64 as b64
                # Build request URL with optional search query
                if gmail_query_str:
                    encoded_query = urllib.parse.quote(gmail_query_str)
                    req_url = f'https://api.maton.ai/google-mail/gmail/v1/users/me/messages?q={encoded_query}&maxResults=50'
                else:
                    req_url = f'https://api.maton.ai/google-mail/gmail/v1/users/me/messages?maxResults={limit}'

                req = urllib.request.Request(req_url)
                req.add_header('Authorization', f'Bearer {maton_key}')
                with urllib.request.urlopen(req) as resp:
                    msg_list = json.load(resp).get('messages', [])

                emails = []
                for msg in msg_list[:limit]:
                    detail_req = urllib.request.Request(f'https://api.maton.ai/google-mail/gmail/v1/users/me/messages/{msg["id"]}?format=metadata&metadataHeaders=From&metadataHeaders=Subject&metadataHeaders=Date&metadataHeaders=To&metadataHeaders=References&metadataHeaders=In-Reply-To')
                    detail_req.add_header('Authorization', f'Bearer {maton_key}')
                    with urllib.request.urlopen(detail_req) as resp:
                        detail = json.load(resp)
                    payload = detail.get('payload', {})
                    headers = {h['name']: h['value'] for h in payload.get('headers', [])}

                    # Check if this is a reply (has In-Reply-To or References header)
                    has_reply = bool(headers.get('In-Reply-To', '') or headers.get('References', ''))

                    emails.append({
                        'from': headers.get('From', '?'),
                        'subject': headers.get('Subject', '?'),
                        'date': headers.get('Date', '?'),
                        'to': headers.get('To', '?'),
                        'snippet': detail.get('snippet', ''),
                        'has_reply': has_reply,
                        'message_id': msg['id'],
                    })

                # Inject emails as context
                if is_linkedin_request:
                    email_context = f'\n\n--- YOUR LINKEDIN EMAILS FROM GMAIL (last {days or "7"} days) ---\n'
                    email_context += f'Total LinkedIn-related emails found: {len(emails)}\n\n'
                else:
                    email_context = '\n\n--- YOUR RECENT EMAILS FROM GMAIL ---\n'

                for i, e in enumerate(emails, 1):
                    reply_flag = ' [REPLY THREAD]' if e['has_reply'] else ''
                    email_context += f'{i}. From: {e["from"]}{reply_flag}\n   Subject: {e["subject"]}\n   Date: {e["date"]}\n   Preview: {e["snippet"]}\n\n'

                system_prompt['content'] += email_context
            except Exception as e:
                system_prompt['content'] += f'\n\nNote: Attempted to fetch Gmail but got error: {str(e)}'

    full_messages = [system_prompt] + messages

    client = get_groq_client()

    def generate():
        if not client:
            yield 'data: {"error": "GROQ_API_KEY not configured"}\n\n'
            yield 'data: [DONE]\n\n'
            return

        try:
            completion = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=full_messages,
                temperature=0.7,
                max_tokens=2048,
                stream=True,
            )

            for chunk in completion:
                if chunk.choices[0].delta.content:
                    content = chunk.choices[0].delta.content
                    yield f'data: {json.dumps({"text": content})}\n\n'

            yield 'data: [DONE]\n\n'
        except Exception as e:
            yield f'data: {json.dumps({"error": str(e)})}\n\n'
            yield 'data: [DONE]\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        },
    )


@ai_bp.route('/api/ai/generate-email', methods=['POST'])
def generate_email():
    """Generate a personalized email for a lead."""
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json() or {}
    lead_data = data.get('lead', {})
    email_type = data.get('type', 'cold')
    lead_id = data.get('lead_id')

    if not lead_data:
        return jsonify({'error': 'Lead data is required'}), 400

    prompt = _build_email_prompt(lead_data, email_type)

    client = get_groq_client()
    if not client:
        # Fallback template-based generation
        return _generate_template_email(lead_data, email_type, user['id'], lead_id)

    try:
        completion = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{'role': 'user', 'content': prompt}],
            temperature=0.7,
            max_tokens=1024,
            response_format={"type": "json_object"},
        )
        raw = completion.choices[0].message.content.strip()
        result = json.loads(raw)
    except Exception as e:
        logger.warning(f'Groq email generation failed: {e}, using template fallback')
        return _generate_template_email(lead_data, email_type, user['id'], lead_id)

    subject = result.get('subject', '')[:500]
    body = result.get('body', '')

    # Replace placeholder sender with actual user name
    user_name = user.get('name', '')
    if user_name:
        body = body.replace('[Your Name]', user_name).replace('[your name]', user_name).replace('[YOUR NAME]', user_name)
        body = body.replace('Your Name', user_name)

    # Save to database if lead_id provided
    if lead_id:
        try:
            now = datetime.now(timezone.utc).isoformat()
            gen_email_data = {
                'lead_id': lead_id,
                'user_id': user['id'],
                'email_type': email_type,
                'subject': subject,
                'body': body,
                'model': 'llama-3.1-8b-instant',
                'created_at': now,
            }
            insert('generated_emails', gen_email_data)
        except Exception:
            pass

    return jsonify({
        'subject': subject,
        'body': body,
    })


def _build_email_prompt(lead_data, email_type):
    """Build the prompt for email generation."""
    name = f"{lead_data.get('first_name', '')} {lead_data.get('last_name', '')}".strip()
    company = lead_data.get('company', '')
    title = lead_data.get('job_title', '')
    industry = lead_data.get('industry', '')
    pain_points = lead_data.get('pain_points', '')

    type_guidance = {
        'cold': 'A first-touch cold outreach email. Personalize it around their role and company. Reference a specific trigger (recent funding, job change, company news) or industry trend relevant to them. Keep it to 3-4 short sentences. End with a soft CTA — offer a relevant resource, not a meeting request.',
        'followup': 'A follow-up email to a lead you reached out to before. Reference the previous email. Add a new piece of value: a case study, relevant article, or specific insight about their company. Include a call to action.',
        'linkedin': 'A LinkedIn connection request or InMail. Maximum 300 characters. Must be extremely concise. Reference something specific about their work or company. Ask a single relevant question.',
        'sequence': 'Generate an email sequence: subjects and bodies for 3 emails (initial, follow-up, break-up). Return as JSON with keys sequence.[0-2].subject and sequence.[0-2].body.',
    }

    guidance = type_guidance.get(email_type, type_guidance['cold'])

    return f"""You are a professional SDR copywriter. Write a personalized {email_type} email for this lead.

LEAD DATA:
- Name: {name}
- Company: {company}
- Title: {title}
- Industry: {industry}
- Pain points: {pain_points}

WRITING GUIDELINES:
{guidance}

Respond with a JSON object containing "subject" and "body" keys. The body should use plain text with newlines, no markdown."""


def _generate_template_email(lead_data, email_type, user_id, lead_id):
    """Fallback template-based email generation."""
    name = f"{lead_data.get('first_name', '')} {lead_data.get('last_name', '')}".strip()
    first_name = lead_data.get('first_name', 'there')
    company = lead_data.get('company', 'your company')
    title = lead_data.get('job_title', '')

    templates = {
        'cold': {
            'subject': f'Quick question about {company}',
            'body': (
                f"Hi {first_name},\n\n"
                f"I noticed you're the {title} at {company}. "
                f"I've been working with similar companies to improve their sales pipeline "
                f"and thought you might find this relevant.\n\n"
                f"Would you be open to a 10-minute chat to see if there's a fit?\n\n"
                f"Best,\n[Your Name]"
            ),
        },
        'followup': {
            'subject': f'Re: Quick question about {company}',
            'body': (
                f"Hi {first_name},\n\n"
                f"I wanted to follow up on my previous email. "
                f"I know things get busy — just wanted to check if improving "
                f"your outreach pipeline is a priority right now?\n\n"
                f"No worries either way.\n\n"
                f"Best,\n[Your Name]"
            ),
        },
        'linkedin': {
            'subject': 'LinkedIn connection',
            'body': (
                f"Hi {first_name}, I've been following {company}'s work in the space. "
                f"Would love to connect and share ideas."
            ),
        },
        'sequence': {
            'subject': f'Sales outreach for {company}',
            'body': (
                f"Email 1 (Initial):\n"
                f"Subject: Quick question about {company}\n\n"
                f"Hi {first_name}, I noticed you're the {title} at {company}...\n\n"
                f"Email 2 (Follow-up):\n"
                f"Subject: Re: Quick question about {company}\n\n"
                f"Hi {first_name}, following up on my previous email...\n\n"
                f"Email 3 (Break-up):\n"
                f"Subject: Should I close your file?\n\n"
                f"Hi {first_name}, I haven't heard back so I'll assume timing isn't right..."
            ),
        },
    }

    template = templates.get(email_type, templates['cold'])

    if lead_id:
        try:
            now = datetime.now(timezone.utc).isoformat()
            gen_email_data = {
                'lead_id': lead_id,
                'user_id': user_id,
                'email_type': email_type,
                'subject': template['subject'][:500],
                'body': template['body'],
                'model': 'template-fallback',
                'created_at': now,
            }
            insert('generated_emails', gen_email_data)
        except Exception:
            pass

    return jsonify(template)


@ai_bp.route('/api/ai/score-lead', methods=['POST'])
def ai_score_lead():
    """Score a lead using AI based on ICP criteria."""
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json() or {}
    lead_data = data.get('lead', {})
    icp = data.get('icp', {})

    if not lead_data:
        return jsonify({'error': 'Lead data is required'}), 400

    result = score_lead_via_groq(lead_data, icp)

    # Update lead in DB if lead_id provided
    lead_id = data.get('lead_id')
    if lead_id:
        lead = select_one('leads', filters=[eq('id', lead_id), eq('workspace_id', user['workspace_id'])])
        if lead:
            now = datetime.now(timezone.utc).isoformat()
            update('leads', {
                'lead_score': result['score'],
                'icp_match': result['icp_match'],
                'score_reason': result['reason'],
                'updated_at': now,
            }, filters=[eq('id', lead_id)])

    return jsonify(result)


@ai_bp.route('/api/ai/parse-linkedin', methods=['POST'])
def parse_linkedin_notes():
    """Parse raw LinkedIn activity notes into structured activities using AI.
    Saves parsed people to the LinkedInActivity database."""
    current_user_id = request.headers.get('X-User-ID', '1')
    try:
        current_user_id = int(current_user_id)
    except (ValueError, TypeError):
        current_user_id = 1
    user = select_one('users', filters=[eq('id', int(current_user_id))])
    if not user:
        return jsonify({'error': 'User not found'}), 404

    data = request.get_json() or {}
    raw_text = data.get('text', '')

    if not raw_text:
        return jsonify({'error': 'Text to parse is required'}), 400

    client = get_groq_client()

    # Pre-extract LinkedIn URLs from raw text
    import re
    urls_found = re.findall(r'(https?://(?:www\.)?linkedin\.com/[^\s)\]]+)', raw_text)
    urls_hint = '\n'.join(urls_found) if urls_found else '(none found by regex)'

    prompt = f"""Parse these LinkedIn connection data entries into structured activities.

RAW DATA:
{raw_text[:4000]}

PRE-EXTRACTED LINKEDIN URLS FROM DATA (assign to correct person):
{urls_hint}

For each person, extract: name, job title, company name, linkedin_url (profile URL),
and activity type (connection_sent, connection_accepted, dm_sent, reply_received,
interested, not_interested, meeting_booked, followup_sent, profile_viewed).

The linkedin_url field is CRITICAL - include it for EVERY person. Look carefully at
each row for a linkedin.com/in/... URL.

Return ONLY valid JSON with a "people" key containing an array of objects:
{{"people": [{{"name": "...", "title": "...", "company": "...", "linkedin_url": "https://linkedin.com/in/...", "activity_type": "connection_sent"}}]}}"""

    people = []
    if client:
        try:
            completion = client.chat.completions.create(
                model="llama-3.1-8b-instant",
                messages=[{'role': 'user', 'content': prompt}],
                temperature=0.2,
                max_tokens=2000,
            )
            result = json.loads(completion.choices[0].message.content)
            people = result.get('people', result.get('activities', []))
        except Exception as e:
            logger.warning(f'Groq parse error: {e}')

    if not people:
        # Fallback: parse table format manually
        people = _parse_table_fallback(raw_text)

    # Post-process: inject regex-extracted URLs for any person missing one
    if people and urls_found:
        url_idx = 0
        for p in people:
            if not p.get('linkedin_url') and url_idx < len(urls_found):
                p['linkedin_url'] = urls_found[url_idx]
                url_idx += 1
    for p in people:
        if p.get('title') and not p.get('notes'):
            p['notes'] = p['title']

    # Save each parsed person to the DB
    saved_count = 0
    now = datetime.now(timezone.utc).isoformat()
    for p in people:
        activity_type = p.get('activity_type', 'connection_sent')
        name = p.get('name', '')[:200]
        company = p.get('company', '')[:255]
        url = p.get('linkedin_url', '')[:500]

        activity_data = {
            'workspace_id': user['workspace_id'],
            'user_id': user['id'],
            'lead_name': name,
            'company': company,
            'linkedin_url': url,
            'activity_type': activity_type,
            'activity_date': '',
            'notes': p.get('title', ''),
            'source': 'ai_dump',
            'created_at': now,
        }
        insert('linkedin_activities', activity_data)
        saved_count += 1

    logger.info(f'Parsed and saved {saved_count} LinkedIn activities from dump')

    for p in people:
        p['source'] = 'ai_dump'

    return jsonify({'people': people, 'saved': saved_count})


def _parse_table_fallback(raw_text):
    """Parse a markdown table or pipe-delimited data without AI."""
    status_words = {'connection sent', 'connection accepted', 'connected', 'pending',
                    'invitation sent', 'accepted', 'not interested', 'interested'}
    people = []
    lines = raw_text.strip().split('\n')
    for line in lines:
        line = line.strip()
        if not line or line.startswith('|---') or line.startswith('| -'):
            continue
        # Parse pipe-delimited rows
        if line.startswith('|'):
            parts = [p.strip() for p in line.split('|')]
            parts = [p for p in parts if p]  # remove empty first/last from pipes
            if len(parts) >= 3 and 'Name' not in parts[0] and 'Title' not in parts[0]:
                name = parts[0]
                title = parts[1] if len(parts) > 1 else ''
                url = ''
                company = ''
                for p in parts:
                    if p.startswith('http') or 'linkedin.com' in p.lower():
                        url = p
                        break
                for p in parts[1:]:
                    if (p.startswith('http') or p == name or p == title
                        or p.lower() in status_words
                        or 'linkedin' in p.lower()):
                        continue
                    company = p
                    break
                activity_type = 'connection_sent'
                status_str = parts[-1].lower() if parts else ''
                status_map = {
                    'connection_sent': 'connection_sent', 'connection': 'connection_sent',
                    'accepted': 'connection_accepted', 'connection_accepted': 'connection_accepted',
                    'message': 'dm_sent', 'dm_sent': 'dm_sent', 'dm': 'dm_sent',
                    'reply': 'reply_received', 'reply_received': 'reply_received',
                    'meeting': 'meeting_booked', 'meeting_booked': 'meeting_booked',
                    'interested': 'interested', 'not_interested': 'not_interested',
                    'follow-up': 'followup_sent', 'followup_sent': 'followup_sent',
                    'profile_view': 'profile_viewed', 'profile_viewed': 'profile_viewed',
                }
                for key, val in status_map.items():
                    if key in status_str:
                        activity_type = val
                        break
                people.append({
                    'name': name,
                    'title': title,
                    'company': company,
                    'linkedin_url': url,
                    'activity_type': activity_type,
                    'source': 'ai_dump',
                })
    return people


import re  # noqa: F811 — needed by _parse_table_fallback
from datetime import datetime, timezone  # noqa: F811
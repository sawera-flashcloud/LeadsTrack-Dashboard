"""
Supabase REST API Client — replaces SQLAlchemy/SQLite for all DB operations.

Usage:
    from app.services.supabase import supabase, service_headers
    data = supabase.table('leads').select('*').execute()
"""

import os
import logging
from supabase import create_client, Client

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://zunkrrcnaaicpqkplnzw.supabase.co')
SUPABASE_SERVICE_KEY = os.environ.get(
    'SUPABASE_SERVICE_KEY',
    'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inp1bmtycmNuYWFpY3Bxa3Bsbnp3Iiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc4MDYyMzI0MSwiZXhwIjoyMDk2MTk5MjQxfQ.DL54OfNnoQ9qwmUfypod_r722yjoGpsx-7AuBfDhj_g'
)

supabase: Client = None
service_headers = None

try:
    supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    logger.info("Supabase client initialized")
except Exception as e:
    logger.error(f"Failed to initialize Supabase: {e}")

# ── Helper: build filters ────────────────────────────────────────────

def eq(field, value):
    return (field, 'eq', value)

def gte(field, value):
    return (field, 'gte', value)

def lte(field, value):
    return (field, 'lte', value)

def is_(field, value):
    return (field, 'is', value)

def in_(field, values):
    return (field, 'in', values)

def like(field, pattern):
    return (field, 'like', pattern)

def apply_filters(query, filters):
    """Apply a list of (field, operator, value) tuples to a query."""
    for f in filters:
        field, op, val = f
        if op == 'eq':
            query = query.eq(field, val)
        elif op == 'gte':
            query = query.gte(field, val)
        elif op == 'lte':
            query = query.lte(field, val)
        elif op == 'is':
            if val is None:
                query = query.is_(field, 'null')
            else:
                query = query.eq(field, val)
        elif op == 'in':
            query = query.in_(field, val)
        elif op == 'like':
            query = query.like(field, val)
    return query

# ── Shorthand CRUD ───────────────────────────────────────────────────

def select(table, columns='*', filters=None, order=None, limit=None, offset=None, count=None):
    """Select rows from a table."""
    if supabase is None:
        logger.error("Supabase client not initialized")
        return type('obj', (object,), {'data': [], 'count': 0})()
    if count:
        query = supabase.table(table).select(columns, count='exact')
    else:
        query = supabase.table(table).select(columns)
    if filters:
        query = apply_filters(query, filters)
    if order:
        query = query.order(*order)
    if limit:
        query = query.limit(limit)
    if offset:
        query = query.offset(offset)
    return query.execute()

def select_one(table, columns='*', filters=None):
    """Select the first matching row."""
    if supabase is None:
        return None
    query = supabase.table(table).select(columns)
    if filters:
        query = apply_filters(query, filters)
    query = query.limit(1)
    result = query.execute()
    return result.data[0] if result.data else None

def insert(table, data):
    """Insert rows. data can be a dict or list of dicts."""
    if supabase is None:
        raise RuntimeError("Supabase client not initialized")
    return supabase.table(table).insert(data).execute()

def update(table, data, filters):
    """Update rows matching filters."""
    if supabase is None:
        raise RuntimeError("Supabase client not initialized")
    query = supabase.table(table).update(data)
    if filters:
        query = apply_filters(query, filters)
    return query.execute()

def delete(table, filters):
    """Delete rows matching filters."""
    if supabase is None:
        raise RuntimeError("Supabase client not initialized")
    query = supabase.table(table).delete()
    if filters:
        query = apply_filters(query, filters)
    return query.execute()

def count(table, filters=None):
    """Count rows matching filters."""
    if supabase is None:
        return 0
    query = supabase.table(table).select('id', count='exact')
    if filters:
        query = apply_filters(query, filters)
    result = query.execute()
    try:
        return int(result.count) if hasattr(result, 'count') and result.count else len(result.data)
    except Exception:
        return 0

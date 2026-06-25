"""Vercel serverless entrypoint for the Flask application."""

from app import create_app

app = create_app('production')

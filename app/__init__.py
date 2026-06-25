import os
from flask import Flask, render_template
from flask_cors import CORS
from flask_jwt_extended import JWTManager
from app.config import config_by_name
from app.services.supabase import supabase


def create_app(config_name='dev'):
    """Application factory."""
    app = Flask(__name__)

    # Load config
    config = config_by_name.get(config_name, config_by_name['dev'])
    app.config.from_object(config)

    # Initialize JWT
    jwt = JWTManager(app)

    # Enable CORS for all /api/* routes
    CORS(app, resources={r"/api/*": {"origins": "*"}})

    # Register blueprints
    from app.routes.auth import auth_bp
    from app.routes.leads import leads_bp
    from app.routes.ai_agent import ai_bp
    from app.routes.linkedin import linkedin_bp
    from app.routes.analytics import analytics_bp
    from app.routes.admin import admin_bp
    from app.routes.gmail import gmail_bp
    from app.routes.hubspot import hubspot_bp
    from app.routes.dashboard import dashboard_bp
    from app.routes.activity import activity_bp
    from app.routes.meetings import meetings_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(leads_bp)
    app.register_blueprint(ai_bp)
    app.register_blueprint(linkedin_bp)
    app.register_blueprint(analytics_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(gmail_bp)
    app.register_blueprint(hubspot_bp)
    app.register_blueprint(dashboard_bp)
    app.register_blueprint(activity_bp)
    app.register_blueprint(meetings_bp)

    @app.route('/')
    @app.route('/app')
    @app.route('/dashboard')
    def serve_frontend():
        return render_template('index.html')

    @app.route('/api/health')
    def health():
        return {'status': 'healthy'}

    return app

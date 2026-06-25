import os
from dotenv import load_dotenv

_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.env')
load_dotenv(_env_path)

# Read MATON_API_KEY directly from .env to override inherited stale shell value
import re as _re
try:
    with open(_env_path) as _f:
        for _line in _f:
            _m = _re.match(r'^MATON_API_KEY=(.+)$', _line.strip())
            if _m:
                os.environ['MATON_API_KEY'] = _m.group(1)
                break
except FileNotFoundError:
    # .env file doesn't exist - this is normal in production
    # Environment variables should be set via platform (e.g., Render environment variables)
    pass


class Config:
    """Base configuration."""
    SECRET_KEY = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')
    JWT_SECRET_KEY = os.environ.get('JWT_SECRET_KEY', 'jwt-dev-secret-change-in-production')
    GROQ_API_KEY = os.environ.get('GROQ_API_KEY', '')
    JWT_ACCESS_TOKEN_EXPIRES = 86400  # 24 hours


class DevConfig(Config):
    """Development configuration."""
    DEBUG = True


class TestConfig(Config):
    """Test configuration."""
    TESTING = True


class ProdConfig(Config):
    """Production configuration."""
    DEBUG = False


config_by_name = {
    'dev': DevConfig,
    'development': DevConfig,
    'test': TestConfig,
    'testing': TestConfig,
    'prod': ProdConfig,
    'production': ProdConfig,
}

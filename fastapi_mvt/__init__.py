"""
FastAPI MVT Framework
A Django-inspired framework built on top of FastAPI
"""

__version__ = "0.1.0"

from fastapi_mvt.core.signals import (
    Signal,
    receiver,
    register_model_signals,
    register_all_model_signals,
    autodiscover_signals,
    setup_signals,
    pre_save,
    post_save,
    pre_delete,
    post_delete,
    post_init,
)
from fastapi_mvt.core.registry import AppRegistry
from fastapi_mvt.utils import (
    get_project_root,
    discover_project_module,
    get_db_session,
    get_base_model,
)
from fastapi_mvt.auth import (
    Auth,
    hash_password,
    verify_password,
    get_auth_dependency,
    # New auth system
    AuthConfig,
    configure_auth,
    get_auth_config,
    get_user_model,
    AbstractAuthUser,
    DefaultAuthUser,
    get_current_user,
    auth_router,
)
from fastapi_mvt.middleware import (
    SecurityHeadersMiddleware,
    SQLInjectionProtectionMiddleware,
    RateLimitMiddleware,
    CustomMiddleware,
    setup_cors_middleware,
    setup_security_middleware,
)

__all__ = [
    "Signal",
    "receiver",
    "register_model_signals",
    "register_all_model_signals",
    "autodiscover_signals",
    "setup_signals",
    "pre_save",
    "post_save",
    "pre_delete",
    "post_delete",
    "post_init",
    "AppRegistry",
    "get_project_root",
    "discover_project_module",
    "get_db_session",
    "get_base_model",
    "Auth",
    "hash_password",
    "verify_password",
    "get_auth_dependency",
    "AuthConfig",
    "configure_auth",
    "get_auth_config",
    "get_user_model",
    "AbstractAuthUser",
    "DefaultAuthUser",
    "get_current_user",
    "auth_router",
    "SecurityHeadersMiddleware",
    "SQLInjectionProtectionMiddleware",
    "RateLimitMiddleware",
    "CustomMiddleware",
    "setup_cors_middleware",
    "setup_security_middleware",
    "__version__",
]

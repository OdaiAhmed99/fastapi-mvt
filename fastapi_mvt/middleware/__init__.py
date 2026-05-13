"""
Security Middleware for FastAPI MVT
Provides CORS, security headers, rate limiting, and SQL injection protection
"""

from fastapi import Request, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response
from typing import List, Optional, Callable
import re
import time
from collections import defaultdict


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """
    Add security headers to all responses
    
    Headers added:
    - X-Content-Type-Options: nosniff
    - X-Frame-Options: DENY
    - X-XSS-Protection: 1; mode=block
    - Strict-Transport-Security: max-age=31536000; includeSubDomains
    - Content-Security-Policy: default-src 'self'
    """
    
    # Paths used by Swagger UI / ReDoc — must not have a restrictive CSP
    _DOCS_PATHS = {"/docs", "/redoc", "/openapi.json"}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        # Skip restrictive CSP for API docs pages so Swagger/ReDoc load correctly
        if request.url.path not in self._DOCS_PATHS:
            response.headers["Content-Security-Policy"] = "default-src 'self'"
        return response


class SQLInjectionProtectionMiddleware(BaseHTTPMiddleware):
    """
    Basic SQL injection protection middleware
    
    Checks request parameters and body for common SQL injection patterns
    Note: This is NOT a replacement for parameterized queries - always use
    SQLAlchemy's built-in query parameterization
    """
    
    SQL_INJECTION_PATTERNS = [
        r"(\bUNION\b.*\bSELECT\b)",
        r"(\bSELECT\b.*\bFROM\b)",
        r"(\bINSERT\b.*\bINTO\b)",
        r"(\bDELETE\b.*\bFROM\b)",
        r"(\bUPDATE\b.*\bSET\b)",
        r"(\bDROP\b.*\bTABLE\b)",
        r"(--|;|\/\*|\*\/)",
        r"(\bOR\b.*=.*)",
        r"(\bAND\b.*=.*)",
    ]
    
    def __init__(self, app, enabled: bool = True):
        super().__init__(app)
        self.enabled = enabled
        self.patterns = [re.compile(pattern, re.IGNORECASE) for pattern in self.SQL_INJECTION_PATTERNS]
    
    def _check_for_sql_injection(self, text: str) -> bool:
        """Check if text contains SQL injection patterns"""
        if not isinstance(text, str):
            return False
        
        for pattern in self.patterns:
            if pattern.search(text):
                return True
        return False
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled:
            return await call_next(request)
        
        # Check query parameters
        for key, value in request.query_params.items():
            if self._check_for_sql_injection(value):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Potential SQL injection detected in query parameters"
                )
        
        # Check path parameters
        if self._check_for_sql_injection(str(request.url.path)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Potential SQL injection detected in URL"
            )
        
        return await call_next(request)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """
    Simple rate limiting middleware
    
    Limits requests per IP address within a time window
    """
    
    def __init__(self, app, requests_per_minute: int = 60, enabled: bool = True):
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.enabled = enabled
        self.request_counts = defaultdict(list)
    
    def _get_client_ip(self, request: Request) -> str:
        """Extract client IP from request"""
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled:
            return await call_next(request)
        
        client_ip = self._get_client_ip(request)
        current_time = time.time()
        
        # Clean old requests (older than 1 minute)
        self.request_counts[client_ip] = [
            req_time for req_time in self.request_counts[client_ip]
            if current_time - req_time < 60
        ]
        
        # Check rate limit
        if len(self.request_counts[client_ip]) >= self.requests_per_minute:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please try again later."
            )
        
        # Add current request
        self.request_counts[client_ip].append(current_time)
        
        response = await call_next(request)
        return response


class CustomMiddleware(BaseHTTPMiddleware):
    """
    Base class for custom user middleware
    
    Users can extend this class to add their own middleware logic
    
    Example:
        from fastapi_mvt.middleware import CustomMiddleware
        
        class MyMiddleware(CustomMiddleware):
            async def dispatch(self, request: Request, call_next):
                # Pre-processing
                print(f"Request: {request.method} {request.url}")
                
                response = await call_next(request)
                
                # Post-processing
                print(f"Response: {response.status_code}")
                
                return response
    """
    
    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """Override this method to implement custom middleware logic"""
        return await call_next(request)


def setup_cors_middleware(
    app,
    allow_origins: List[str] = ["*"],
    allow_credentials: bool = True,
    allow_methods: List[str] = ["*"],
    allow_headers: List[str] = ["*"],
) -> None:
    """
    Setup CORS middleware for the application
    
    Args:
        app: FastAPI application instance
        allow_origins: List of allowed origins (default: ["*"])
        allow_credentials: Allow credentials (default: True)
        allow_methods: List of allowed HTTP methods (default: ["*"])
        allow_headers: List of allowed headers (default: ["*"])
    """
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_credentials=allow_credentials,
        allow_methods=allow_methods,
        allow_headers=allow_headers,
    )


def setup_security_middleware(
    app,
    enable_security_headers: bool = True,
    enable_sql_injection_protection: bool = True,
    enable_rate_limiting: bool = True,
    rate_limit_requests_per_minute: int = 60,
    trusted_hosts: Optional[List[str]] = None,
) -> None:
    """
    Setup security middleware for the application
    
    Args:
        app: FastAPI application instance
        enable_security_headers: Enable security headers middleware
        enable_sql_injection_protection: Enable SQL injection protection
        enable_rate_limiting: Enable rate limiting
        rate_limit_requests_per_minute: Rate limit threshold
        trusted_hosts: List of trusted hosts (optional)
    """
    if enable_security_headers:
        app.add_middleware(SecurityHeadersMiddleware)
    
    if enable_sql_injection_protection:
        app.add_middleware(SQLInjectionProtectionMiddleware, enabled=True)
    
    if enable_rate_limiting:
        app.add_middleware(
            RateLimitMiddleware,
            requests_per_minute=rate_limit_requests_per_minute,
            enabled=True
        )
    
    if trusted_hosts:
        app.add_middleware(TrustedHostMiddleware, allowed_hosts=trusted_hosts)


__all__ = [
    "SecurityHeadersMiddleware",
    "SQLInjectionProtectionMiddleware",
    "RateLimitMiddleware",
    "CustomMiddleware",
    "setup_cors_middleware",
    "setup_security_middleware",
]

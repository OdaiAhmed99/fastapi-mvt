"""Admin site via SQLAdmin (``pip install "fastapi-mvt[admin]"``).

::

    from fastapi_mvt.admin import setup_admin
    setup_admin(app, db, auth=auth)            # every model, at /admin

Only active superusers can log in. The admin works on your models as they
are, because they are ordinary SQLAlchemy models.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional


def setup_admin(
    app: Any,
    database: Any,
    *,
    auth: Any = None,
    models: Optional[Iterable[type]] = None,
    title: str = "Admin",
    base_url: str = "/admin",
    allow_anonymous: bool = False,
) -> Any:
    try:
        from sqladmin import Admin, ModelView
        from sqladmin.authentication import AuthenticationBackend
    except ImportError:
        raise RuntimeError('SQLAdmin is not installed: pip install "fastapi-mvt[admin]"') from None
    from sqlalchemy import inspect
    from starlette.requests import Request

    from fastapi_mvt.db.models import Base

    if auth is None and not allow_anonymous:
        raise ValueError(
            "setup_admin() needs auth=... so only superusers can log in. "
            "Pass allow_anonymous=True only for local experiments."
        )

    backend = None
    if auth is not None:

        class SuperuserBackend(AuthenticationBackend):
            async def login(self, request: Request) -> bool:
                form = await request.form()
                user = await auth.authenticate(str(form.get("username", "")), str(form.get("password", "")))
                if user is None or not user.is_superuser:
                    return False
                request.session.update({"admin_user": str(user.pk), "admin_ver": user.token_version or 0})
                return True

            async def logout(self, request: Request) -> bool:
                request.session.clear()
                return True

            async def authenticate(self, request: Request) -> bool:
                user_id = request.session.get("admin_user")
                if user_id is None:
                    return False
                user = await auth.user_model.objects.get_or_none(pk=auth._parse_pk(user_id))
                return bool(
                    user
                    and user.is_active
                    and user.is_superuser
                    and (user.token_version or 0) == request.session.get("admin_ver")
                )

        backend = SuperuserBackend(secret_key=auth.secret_key)

    admin = Admin(
        app,
        session_maker=database.sessionmaker,
        title=title,
        base_url=base_url,
        authentication_backend=backend,
    )
    selected = list(models) if models is not None else sorted(
        (m.class_ for m in Base.registry.mappers if not m.class_.__dict__.get("__abstract__")),
        key=lambda cls: cls.__name__,
    )
    for model in selected:
        mapper: Any = inspect(model)
        private = [attr.key for attr in mapper.column_attrs if attr.columns[0].info.get("private")]
        view = type(
            f"{model.__name__}Admin",
            (ModelView,),
            {
                "column_exclude_list": private,
                "form_excluded_columns": private,
                "column_details_exclude_list": private,
                "name": model.__name__,
            },
            model=model,
        )
        admin.add_view(view)
    return admin

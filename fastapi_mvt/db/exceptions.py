"""ORM exceptions and the FastAPI handlers that turn them into HTTP responses."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError, InvalidRequestError
from sqlalchemy.orm.exc import DetachedInstanceError

logger = logging.getLogger("fastapi_mvt.db")


class ORMError(Exception):
    """Base class for every error raised by the fastapi-mvt ORM layer."""


class ObjectDoesNotExist(ORMError):
    """A lookup that expected exactly one row found none.

    Every model gets its own subclass, so both of these work::

        except Post.DoesNotExist: ...
        except ObjectDoesNotExist: ...
    """

    model_name = "Object"


class MultipleObjectsReturned(ORMError):
    """A lookup that expected exactly one row found several."""


class FieldError(ORMError):
    """A keyword lookup referenced a field or lookup type that does not exist."""


class SessionError(ORMError):
    """The ORM was used where no database session could be provided."""


class RelationNotLoaded(ORMError, InvalidRequestError):
    """A relationship was read before it was loaded (loading it would need hidden I/O)."""


class DetachedRelationNotLoaded(RelationNotLoaded, DetachedInstanceError):
    """Same, on an object whose transaction has already ended."""


def install_exception_handlers(app: FastAPI) -> None:
    """Map ORM errors to sensible HTTP responses.

    * ``Model.DoesNotExist``  -> 404
    * ``IntegrityError``      -> 409 (unique / foreign-key / not-null violation)
    """

    async def _not_found(request: Request, exc: ObjectDoesNotExist) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": f"{exc.model_name} not found."})

    async def _conflict(request: Request, exc: IntegrityError) -> JSONResponse:
        # The raw driver message can leak schema details, so log it and send a generic one.
        logger.info("IntegrityError on %s %s: %s", request.method, request.url.path, exc.orig)
        return JSONResponse(
            status_code=409,
            content={"detail": "This conflicts with existing data (duplicate or invalid reference)."},
        )

    app.add_exception_handler(ObjectDoesNotExist, _not_found)  # type: ignore[arg-type]
    app.add_exception_handler(IntegrityError, _conflict)  # type: ignore[arg-type]

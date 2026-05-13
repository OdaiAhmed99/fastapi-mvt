"""
Helper utilities for fastapi_mvt
"""

from typing import Any, Dict


def get_object_or_404(model, db, **filters):
    """
    Get an object or raise 404
    Similar to Django's get_object_or_404
    """
    from fastapi import HTTPException
    
    obj = db.query(model).filter_by(**filters).first()
    if not obj:
        raise HTTPException(status_code=404, detail=f"{model.__name__} not found")
    return obj


def get_list_or_404(model, db, **filters):
    """
    Get a list of objects or raise 404 if empty
    """
    from fastapi import HTTPException
    
    objects = db.query(model).filter_by(**filters).all()
    if not objects:
        raise HTTPException(status_code=404, detail=f"No {model.__name__} found")
    return objects


def paginate(query, page: int = 1, page_size: int = 20):
    """
    Paginate a SQLAlchemy query
    
    Returns:
        dict with items, total, page, page_size, total_pages
    """
    total = query.count()
    items = query.offset((page - 1) * page_size).limit(page_size).all()
    
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size
    }

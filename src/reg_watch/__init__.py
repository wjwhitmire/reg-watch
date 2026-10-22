"""Reg Watch: a self-hosting tracker of US federal rulemaking, built on the Federal Register API."""

__version__ = "0.1.0"

from .schema import SCHEMA_KEYS, TYPES, MalformedRecord, dedupe, normalize_record, validate_document  # noqa: E402
from .filters import filter_documents, closing_soon, days_between  # noqa: E402

__all__ = [
    "__version__", "SCHEMA_KEYS", "TYPES", "MalformedRecord", "dedupe", "normalize_record",
    "validate_document", "filter_documents", "closing_soon", "days_between",
]

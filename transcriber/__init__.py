"""transcriber: faithful, checkable transcriptions of scanned archival documents."""

__version__ = "0.1.0"

from .api import (TranscriberError, TranscriptionResult, ValidationResult,
                  transcribe_file, validate_file)

__all__ = [
    "__version__", "TranscriberError", "TranscriptionResult", "ValidationResult",
    "transcribe_file", "validate_file",
]

"""The ingestion poller - four MISO endpoints, written verbatim to data/raw/.

The logic lives in core.py rather than here, so that importing the package
does not drag in the whole program.
"""

from backend.poller.core import poll_once

__all__ = ["poll_once"]

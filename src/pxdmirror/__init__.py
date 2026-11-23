"""pxd-mirror: a local, verified mirror of public PRIDE / ProteomeXchange (PXD) data sets.

    from pxdmirror import Mirror
    m = Mirror()                       # root and settings from the configuration (see docs/CONFIGURATION.md)
    m.query("PXD000001")
"""
__version__ = "0.1.0"

from .util import CapacityError, CorruptRecord, PxdError, UsageError   # noqa: E402
from .config import Config   # noqa: E402
from .store import Mirror   # noqa: E402

__all__ = ["Mirror", "Config", "PxdError", "UsageError", "CapacityError", "CorruptRecord", "__version__"]

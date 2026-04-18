"""SaaS-only modules for the hosted GridOS tier.

The public OSS flow must work with an empty `cloud/` import path: nothing in
this package mutates shared state at import time, and `main.py` only wires
SaaS-specific routers when `config.SAAS_MODE` is true. Treat this directory as
the single seam between open-core and managed — if a module here leaks into the
OSS hot path, that's a bug.
"""

from cloud.config import SAAS_MODE, SAAS_FEATURES  # noqa: F401

__all__ = ["SAAS_MODE", "SAAS_FEATURES"]

"""Application services shared by NoneBot plugins.

The package deliberately contains no plugin registration.  Plugins depend on
these services, while service lifecycle wiring is kept in :mod:`lifecycle`.
"""

from .lifecycle import close_services, register_shutdown_hook

__all__ = ["close_services", "register_shutdown_hook"]

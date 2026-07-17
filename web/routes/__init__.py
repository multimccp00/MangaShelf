"""Route modules (FastAPI APIRouter) split out of the once-monolithic api.py.

Each module imports the shared infrastructure (DB proxy, auth dependencies,
caches) FROM api — that's safe because api.py includes these routers at the
very bottom, after everything they need is defined.
"""

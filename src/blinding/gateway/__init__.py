"""The Blinding gateway: API keys, a token bucket, and Prometheus metrics in
front of a llama.cpp server.

`blinding.gateway.app.create_app` builds the ASGI application for a caller that
owns the settings. `python -m blinding.gateway` builds the settings from the
command line and the `BLINDING_*` environment variables, then hands the
application to uvicorn.
"""

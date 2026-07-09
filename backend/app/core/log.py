"""Shared application logger for the backend.

A single named logger reused across modules (routers and core), so log
configuration lives in one place. Handlers/levels are left to the host
(uvicorn / the app entry point).
"""
import logging

logger = logging.getLogger("earthanalyser")

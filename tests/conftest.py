"""Shared test setup."""

import os

# FastMCP reads a .env file from the working directory when it is imported.
# Tests have no use for real credentials, so point it at nothing.
os.environ['FASTMCP_ENV_FILE'] = os.devnull

"""Modal deployment of the MCP server.

Deploy with:
    uv run modal deploy modal_app.py

Expects a Modal Secret named 'icloud-calendar-mcp' holding ICLOUD_USERNAME,
ICLOUD_APP_PASSWORD and MCP_AUTH_TOKEN.
"""

import modal

APP_NAME = 'icloud-calendar-mcp'

app = modal.App(APP_NAME)

image = modal.Image.debian_slim(python_version='3.14') \
    .uv_sync(extra_options='--no-dev') \
    .add_local_python_source('auth', 'icloud', 'server')


# Sized to cost close to nothing: the smallest CPU share, no warm
# containers, and a single container so traffic can never fan out.
@app.function(
    image=image,
    secrets=[modal.Secret.from_name(APP_NAME)],
    cpu=0.125,
    memory=256,
    min_containers=0,
    max_containers=1,
    scaledown_window=60
)
@modal.concurrent(max_inputs=16)
# Modal checks the proxy token before a request reaches the container. The
# app then checks the same Authorization header itself (see auth.py), so
# the endpoint stays closed even if this flag is ever removed.
@modal.asgi_app(requires_proxy_auth=True)
def web():
    from server import build_app

    return build_app()

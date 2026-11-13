"""Modal deployment of the MCP server.

Deploy with:
    uv run modal deploy modal_app.py

Expects a Modal Secret named 'icloud-calendar-mcp' holding ICLOUD_USERNAME,
ICLOUD_APP_PASSWORD and MCP_AUTH_TOKEN.
"""

import modal

APP_NAME = 'icloud-calendar-mcp'

app = modal.App(APP_NAME)

# Hosts the deployed server answers to. Add your own domain here if you
# put one in front of the Modal URL.
ALLOWED_HOSTS = ['*.modal.run']

# uv is pinned so a rebuild installs with the same tool, not whatever is
# newest that day. Raise it on purpose, along with the lock file.
image = modal.Image.debian_slim(python_version='3.14') \
    .uv_sync(extra_options='--no-dev', uv_version='0.12.5') \
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
    scaledown_window=60,
    # The server gives up on a tool call well before this (see the limits
    # in icloud.py). This is the stop behind it, far below Modal's default
    # of 300 seconds.
    timeout=90
)
@modal.concurrent(max_inputs=16)
# Modal checks the proxy token before a request reaches the container. The
# app then checks the same Authorization header itself (see auth.py), so
# the endpoint stays closed even if this flag is ever removed.
@modal.asgi_app(requires_proxy_auth=True)
def web():
    from server import build_app

    return build_app(allowed_hosts=ALLOWED_HOSTS)

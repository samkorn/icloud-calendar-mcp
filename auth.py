"""Bearer-token gate in front of the MCP server."""

import hmac

MIN_TOKEN_LENGTH = 32


class BearerAuthMiddleware:
    """ASGI middleware that rejects requests without the bearer token.

    Wraps the whole app, so every route is covered and nothing behind it
    runs (no MCP handling, no iCloud calls) for an unauthenticated request.
    """

    def __init__(self, app, token: str):
        if len(token) < MIN_TOKEN_LENGTH:
            message = (
                "MCP_AUTH_TOKEN must be at least "
                f"{MIN_TOKEN_LENGTH} characters."
            )
            raise ValueError(message)
        self.app = app
        self.expected = f'Bearer {token}'.encode()

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'http':
            if not self._authorized(scope):
                await _send_unauthorized(send)
                return
        elif scope['type'] != 'lifespan':
            # Anything else (websockets) is not something this server
            # offers, so drop it rather than pass it through unchecked.
            return
        await self.app(scope, receive, send)

    def _authorized(self, scope) -> bool:
        values = [
            value
            for name, value in scope['headers']
            if name == b'authorization'
        ]
        if len(values) != 1:
            return False
        return hmac.compare_digest(values[0], self.expected)


async def _send_unauthorized(send):
    await send({
        'type': 'http.response.start',
        'status': 401,
        'headers': [
            (b'content-type', b'text/plain; charset=utf-8'),
            (b'www-authenticate', b'Bearer'),
        ],
    })
    await send({'type': 'http.response.body', 'body': b'Unauthorized'})

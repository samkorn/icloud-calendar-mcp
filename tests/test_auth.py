"""The bearer-token gate: nothing gets through without the exact header."""

import pytest
from starlette.testclient import TestClient

import server
from auth import BearerAuthMiddleware

TOKEN = 'test-token-0123456789abcdefghijklmnopqrstuv'
INITIALIZE = {
    'jsonrpc': '2.0',
    'id': 1,
    'method': 'initialize',
    'params': {
        'protocolVersion': '2025-06-18',
        'capabilities': {},
        'clientInfo': {'name': 'test', 'version': '0'},
    },
}
MCP_HEADERS = {'accept': 'application/json, text/event-stream'}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('MCP_AUTH_TOKEN', TOKEN)
    with TestClient(server.build_app()) as test_client:
        yield test_client


def test_correct_token_reaches_the_server(client):
    headers = MCP_HEADERS | {'authorization': f'Bearer {TOKEN}'}
    response = client.post('/mcp', json=INITIALIZE, headers=headers)
    assert response.status_code == 200
    assert response.json()['result']['serverInfo']['name'] == 'iCloud Calendar'


@pytest.mark.parametrize(
    'authorization',
    [
        None,
        '',
        'Bearer',
        'Bearer ',
        TOKEN,
        f'bearer {TOKEN}',
        f'Bearer {TOKEN}x',
        f'Bearer {TOKEN[:-1]}',
        f'Basic {TOKEN}',
    ]
)
def test_anything_else_is_rejected(client, authorization):
    headers = dict(MCP_HEADERS)
    if authorization is not None:
        headers['authorization'] = authorization
    response = client.post('/mcp', json=INITIALIZE, headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize('path', ['/', '/mcp', '/anything/else'])
def test_every_path_is_gated(client, path):
    assert client.get(path).status_code == 401


def test_duplicate_authorization_headers_are_rejected(client):
    headers = [
        ('authorization', f'Bearer {TOKEN}'),
        ('authorization', 'Bearer something-else'),
    ]
    response = client.post('/mcp', json=INITIALIZE, headers=headers)
    assert response.status_code == 401


@pytest.mark.parametrize('token', ['', 'short'])
def test_weak_token_refuses_to_start(token):
    with pytest.raises(ValueError):
        BearerAuthMiddleware(app=None, token=token)

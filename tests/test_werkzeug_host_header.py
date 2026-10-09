"""Guards the ``werkzeug`` version pin in pyproject.toml (#196): Docker service names with underscores must be valid Host headers.

Exercises Werkzeug itself on purpose: the project's only defence against the regression is the dependency range.
"""

import pytest
from flask import Flask


@pytest.fixture
def client():
    app = Flask(__name__)

    @app.route("/")
    def index() -> str:
        return "ok"

    return app.test_client()


class TestUnderscoreHostHeader:
    @pytest.mark.parametrize("host", ["my_service:8080", "plex_agent", "a_b.c_d", "my-service:8080"])
    def test_request_succeeds_when_host_has_underscore(self, client, host):
        response = client.get("/", headers={"Host": host})

        assert response.status_code == 200

from fastapi.testclient import TestClient

from retrieval_api.main import app


def test_root() -> None:
    client = TestClient(app)
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}

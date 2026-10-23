import httpx
from types import SimpleNamespace
from evi import doctor

def _doctor_with_backend(monkeypatch, kind, **llm):
    from evi import config as config_mod
    cfg = config_mod.Config()
    cfg.llm.backend = kind
    for k, v in llm.items():
        setattr(cfg.llm, k, v)
    monkeypatch.setattr(config_mod.Config, "load", classmethod(lambda cls: cfg))
    return doctor

def test_doctor_backend_reachable_ok(monkeypatch):
    doctor = _doctor_with_backend(monkeypatch, "openai_compat",
                                  base_url="http://api.example.com/v1")
    seen = {}
    def fake_get(url, timeout=None, headers=None):
        seen["url"] = url
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(httpx, "get", fake_get)
    (check,) = doctor._check_backend()
    assert check.status == "ok"
    assert check.detail == "http://api.example.com/v1 (HTTP 200)"
    assert seen["url"] == "http://api.example.com/v1/models"

def test_doctor_backend_404_warns(monkeypatch):
    base = "http://api.example.com/v1"
    doctor = _doctor_with_backend(monkeypatch, "openai_compat", base_url=base)
    def fake_get(url, timeout=None, headers=None):
        return SimpleNamespace(status_code=404)

    monkeypatch.setattr(httpx, "get", fake_get)
    (check,) = doctor._check_backend()
    assert check.status == "warn"
    assert base in check.detail
    assert "/models" in check.detail
    assert "OpenAI-compatible server" in check.detail
    assert "usually ends in /v1" in check.detail

def test_doctor_backend_other_non_2xx_warns(monkeypatch):
    base = "http://api.example.com/v1"
    doctor = _doctor_with_backend(monkeypatch, "openai_compat", base_url=base)
    def fake_get(url, timeout=None, headers=None):
        return SimpleNamespace(status_code=302)

    monkeypatch.setattr(httpx, "get", fake_get)
    (check,) = doctor._check_backend()
    assert check.status == "warn"
    assert check.detail == f"{base} returned HTTP 302"

def test_doctor_backend_401_403_fails(monkeypatch):
    base = "http://api.example.com/v1"
    doctor = _doctor_with_backend(monkeypatch, "openai_compat", base_url=base)
    def fake_get(url, timeout=None, headers=None):
        return SimpleNamespace(status_code=403)

    monkeypatch.setattr(httpx, "get", fake_get)
    (check,) = doctor._check_backend()
    assert check.status == "fail"
    assert "rejected the API key" in check.detail
    assert "HTTP 403" in check.detail

def test_doctor_backend_5xx_warns(monkeypatch):
    base = "http://api.example.com/v1"
    doctor = _doctor_with_backend(monkeypatch, "openai_compat", base_url=base)
    def fake_get(url, timeout=None, headers=None):
        return SimpleNamespace(status_code=500)

    monkeypatch.setattr(httpx, "get", fake_get)
    (check,) = doctor._check_backend()
    assert check.status == "warn"
    assert check.detail == f"{base} returned HTTP 500"

def test_doctor_backend_localhost_normalization(monkeypatch):
    base = "http://localhost:1234/v1"
    doctor = _doctor_with_backend(monkeypatch, "openai_compat", base_url=base)
    seen = {}
    def fake_get(url, timeout=None, headers=None):
        seen["url"] = url
        return SimpleNamespace(status_code=200)

    monkeypatch.setattr(httpx, "get", fake_get)
    (check,) = doctor._check_backend()
    assert check.status == "ok"
    assert check.detail == f"{base} (HTTP 200)"
    assert seen["url"] == "http://127.0.0.1:1234/v1/models"

def test_doctor_backend_connection_error_fails(monkeypatch):
    base = "http://api.example.com/v1"
    doctor = _doctor_with_backend(monkeypatch, "openai_compat", base_url=base)
    def fake_get(url, timeout=None, headers=None):
        raise httpx.ConnectError("connection failed")

    monkeypatch.setattr(httpx, "get", fake_get)
    (check,) = doctor._check_backend()
    assert check.status == "fail"
    assert "unreachable" in check.detail
    assert "ConnectError" in check.detail

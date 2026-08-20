from types import SimpleNamespace
from unittest.mock import MagicMock

from app.yc import YcClient, YcKubeCredentials, _IAM_TOKEN_AUDIENCE


def _yc():
    yc = YcClient.__new__(YcClient)
    yc._node_group_id = "ng-1"
    yc._svc = MagicMock()
    yc._ops = MagicMock()
    yc._last_operation_id = None
    return yc


def test_set_size_records_operation_id():
    yc = _yc()
    yc._svc.Update.return_value = SimpleNamespace(id="op-123")

    yc.set_size(3)

    assert yc._last_operation_id == "op-123"


def test_operation_in_progress_false_without_a_tracked_operation():
    yc = _yc()
    assert yc.operation_in_progress() is False
    yc._ops.Get.assert_not_called()


def test_operation_in_progress_true_while_running():
    yc = _yc()
    yc._last_operation_id = "op-123"
    yc._ops.Get.return_value = SimpleNamespace(done=False)

    assert yc.operation_in_progress() is True


def test_operation_in_progress_clears_when_done():
    yc = _yc()
    yc._last_operation_id = "op-123"
    yc._ops.Get.return_value = SimpleNamespace(done=True)

    assert yc.operation_in_progress() is False
    assert yc._last_operation_id is None
    yc._ops.Get.reset_mock()
    assert yc.operation_in_progress() is False
    yc._ops.Get.assert_not_called()


def _creds():
    c = YcKubeCredentials.__new__(YcKubeCredentials)
    c._sa_key = {
        "id": "key-1",
        "service_account_id": "sa-1",
        "private_key": "-----BEGIN PRIVATE KEY-----\nx\n-----END PRIVATE KEY-----",
    }
    c._cluster_id = "cl-1"
    c._iam = MagicMock()
    c._token = None
    c._token_expiry = 0.0
    return c


def _cluster(internal, external, ca):
    return SimpleNamespace(
        master=SimpleNamespace(
            endpoints=SimpleNamespace(
                internal_v4_endpoint=internal,
                external_v4_endpoint=external,
            ),
            master_auth=SimpleNamespace(cluster_ca_certificate=ca),
        )
    )


def test_capture_connection_picks_internal_endpoint():
    c = _creds()
    cluster = _cluster("https://10.0.0.1:443", "https://203.0.113.1:443", "CA-PEM")
    c._capture_connection(MagicMock(Get=MagicMock(return_value=cluster)), "internal")
    assert c.endpoint == "https://10.0.0.1:443"
    assert c.ca_cert == "CA-PEM"


def test_capture_connection_picks_external_endpoint():
    c = _creds()
    cluster = _cluster("https://10.0.0.1:443", "https://203.0.113.1:443", "CA-PEM")
    c._capture_connection(MagicMock(Get=MagicMock(return_value=cluster)), "external")
    assert c.endpoint == "https://203.0.113.1:443"
    assert c.ca_cert == "CA-PEM"


def _iam_response(token, expires_at_seconds):
    return SimpleNamespace(
        iam_token=token,
        expires_at=SimpleNamespace(seconds=expires_at_seconds, nanos=0),
    )


def test_get_token_mints_and_caches(monkeypatch):
    monkeypatch.setattr("app.yc.jwt.encode", lambda *a, **k: "signed-jwt")
    monkeypatch.setattr("app.yc.time.time", lambda: 1000.0)
    c = _creds()
    c._iam.Create.return_value = _iam_response("tok-1", 1000 + 12 * 3600)

    assert c.get_token() == "tok-1"
    assert c.get_token() == "tok-1"
    assert c._iam.Create.call_count == 1


def test_get_token_re_mints_when_near_expiry(monkeypatch):
    monkeypatch.setattr("app.yc.jwt.encode", lambda *a, **k: "signed-jwt")
    monkeypatch.setattr("app.yc.time.time", lambda: 1000.0)
    c = _creds()
    c._iam.Create.return_value = _iam_response("tok-1", 1000 + 100)

    assert c.get_token() == "tok-1"
    c._iam.Create.return_value = _iam_response("tok-2", 1000 + 12 * 3600)
    assert c.get_token() == "tok-2"
    assert c._iam.Create.call_count == 2


def test_get_token_signs_jwt_with_sa_key_fields(monkeypatch):
    captured = {}

    def fake_encode(payload, key, **kw):
        captured["payload"] = payload
        captured["key"] = key
        captured["algorithm"] = kw.get("algorithm")
        captured["headers"] = kw.get("headers")
        return "signed-jwt"

    monkeypatch.setattr("app.yc.jwt.encode", fake_encode)
    monkeypatch.setattr("app.yc.time.time", lambda: 1000.0)
    c = _creds()
    c._iam.Create.return_value = _iam_response("tok-1", 1000 + 12 * 3600)

    c.get_token()

    assert captured["algorithm"] == "PS256"
    assert captured["headers"]["kid"] == "key-1"
    assert captured["payload"]["iss"] == "sa-1"
    assert captured["payload"]["aud"] == _IAM_TOKEN_AUDIENCE
    sent = c._iam.Create.call_args.args[0]
    assert sent.jwt == "signed-jwt"


def test_capture_connection_rejects_unknown_endpoint_type():
    import pytest
    c = _creds()
    cluster = _cluster("https://10.0.0.1:443", "https://203.0.113.1:443", "CA-PEM")
    with pytest.raises(ValueError):
        c._capture_connection(
            MagicMock(Get=MagicMock(return_value=cluster)), "public"
        )

import pytest

from app import metrics


@pytest.fixture
def fresh_metrics():
    metrics.reset()
    yield metrics


def _sample(name, **labels):
    return metrics.registry.get_sample_value(name, labels or None)


def test_get_current_size_failure_counts_a_get_size_error(fresh_metrics):
    yc = _yc()
    yc._svc.Get.side_effect = RuntimeError("grpc down")

    with pytest.raises(RuntimeError):
        yc.get_current_size()

    assert _sample("litescaler_yc_api_errors_total", op="get_size") == 1


def test_set_size_failure_counts_an_update_error(fresh_metrics):
    yc = _yc()
    yc._svc.Update.side_effect = RuntimeError("grpc down")

    with pytest.raises(RuntimeError):
        yc.set_size(3)

    assert _sample("litescaler_yc_api_errors_total", op="update") == 1


def test_operation_lookup_failure_counts_a_get_operation_error(fresh_metrics):
    yc = _yc()
    yc._last_operation_id = "op-123"
    yc._ops.Get.side_effect = RuntimeError("grpc down")

    with pytest.raises(RuntimeError):
        yc.operation_in_progress()

    assert _sample("litescaler_yc_api_errors_total", op="get_operation") == 1


def test_successful_calls_count_no_errors(fresh_metrics):
    yc = _yc()
    yc._svc.Get.return_value = SimpleNamespace(
        scale_policy=SimpleNamespace(fixed_scale=SimpleNamespace(size=4))
    )

    assert yc.get_current_size() == 4
    assert _sample("litescaler_yc_api_errors_total", op="get_size") is None


def _credentials(monkeypatch):
    creds = YcKubeCredentials.__new__(YcKubeCredentials)
    creds._sa_key = {
        "service_account_id": "sa-1", "id": "key-1", "private_key": "pk",
    }
    creds._iam = MagicMock()
    creds._token = None
    creds._token_expiry = 0.0
    monkeypatch.setattr("app.yc.jwt.encode", lambda *a, **kw: "signed-jwt")
    return creds


def test_minting_a_token_counts_a_mint(monkeypatch, fresh_metrics):
    creds = _credentials(monkeypatch)
    creds._iam.Create.return_value = SimpleNamespace(
        iam_token="tok", expires_at=SimpleNamespace(seconds=9999, nanos=0)
    )

    assert creds.get_token() == "tok"
    assert _sample("litescaler_iam_token_mints_total") == 1


def test_a_failed_mint_counts_an_iam_token_error(monkeypatch, fresh_metrics):
    creds = _credentials(monkeypatch)
    creds._iam.Create.side_effect = RuntimeError("iam down")

    with pytest.raises(RuntimeError):
        creds.get_token()

    assert _sample("litescaler_yc_api_errors_total", op="iam_token") == 1
    assert _sample("litescaler_iam_token_mints_total") == 0

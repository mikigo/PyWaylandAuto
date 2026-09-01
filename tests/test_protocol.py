import json

import pytest

from pywaylandauto import protocol as p


def test_request_round_trip():
    line = p.encode_request(7, "input.click", {"button": "left"})
    assert line.endswith(b"\n")
    req = p.decode_request(line.decode("utf-8"))
    assert req == {"id": 7, "method": "input.click", "params": {"button": "left"}}


def test_request_empty_params_defaults_to_dict():
    req = p.decode_request(p.encode_request(1, "ping").decode())
    assert req["params"] == {}


def test_response_success_round_trip():
    line = p.encode_response(7, ok=True, result={"stopped": True})
    resp = p.decode_response(line.decode())
    assert resp == {"id": 7, "ok": True, "result": {"stopped": True}}


def test_response_error_round_trip():
    line = p.encode_response(7, ok=False, error={"code": "portal_failed", "message": "boom"})
    resp = p.decode_response(line.decode())
    assert resp == {"id": 7, "ok": False, "code": "portal_failed", "message": "boom"}


def test_decode_request_tolerates_crlf():
    req = p.decode_request('{"id": 3, "method": "ping", "params": {}}\r\n')
    assert req["id"] == 3


@pytest.mark.parametrize("bad", [
    "", "not json", '"just a string"', "[]", "null",
    '{"method": "ping", "params": {}}',          # missing id
    '{"id": "x", "method": "ping", "params": {}}',  # id not int
    '{"id": 1, "params": {}}',                    # missing method
    '{"id": 1, "method": "ping", "params": []}',  # params not dict
    '{"id": 1, "method": ""}',                    # empty method
])
def test_decode_request_rejects_bad_input(bad):
    with pytest.raises(p.ProtocolError):
        p.decode_request(bad)


@pytest.mark.parametrize("bad", [
    '{"id": 1, "ok": true}',                  # missing result
    '{"id": 1, "ok": false}',                 # missing error
    '{"id": 1, "ok": false, "error": {}}',    # error without code
    '{"id": 1, "ok": "yes"}',                 # ok not bool
])
def test_decode_response_rejects_bad_input(bad):
    with pytest.raises(p.ProtocolError):
        p.decode_response(bad)


def test_decode_request_line_too_long():
    huge = '{"id": 1, "method": "' + "a" * (p.MAX_LINE + 10) + '", "params": {}}'
    with pytest.raises(p.ProtocolError) as exc:
        p.decode_request(huge)
    assert exc.value.code == p.ERR_INVALID_JSON


def test_remote_error_carries_code_and_message():
    err = p.RemoteError("permission_denied", "user denied the dialog")
    assert err.code == "permission_denied"
    assert "permission_denied" in str(err)


def test_error_codes_stable_names():
    # The wire protocol depends on these exact strings; guard against edits.
    assert p.ERR_PERMISSION_DENIED == "permission_denied"
    assert p.ERR_PERMISSION_PENDING == "permission_pending"
    assert p.ERR_SESSION_NOT_STARTED == "session_not_started"
    assert p.ERR_PORTAL_UNAVAILABLE == "portal_unavailable"
    assert p.ERR_ALREADY_RUNNING == "already_running"

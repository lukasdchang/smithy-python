#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
from typing import Any
from unittest.mock import Mock

import pytest
from smithy_aws_core.interceptors.recursion_detection import (
    TRACE_ID_HEADER,
    RecursionDetectionInterceptor,
)
from smithy_core import URI
from smithy_core.interceptors import RequestContext
from smithy_http import Field, Fields, HTTPRequest
from smithy_http.interfaces import HTTPRequest as HTTPRequestInterface

_TRACE_ID = (
    "Root=1-5759e988-bd862e3fe1be46a994272793;Parent=53995c3f42cd8ad8;"
    "Sampled=1;lineage=a87bd80c:0,68fd508a:5,c512fbe3:2"
)


def _request(fields: Fields | None = None) -> HTTPRequest:
    return HTTPRequest(
        method="POST",
        destination=URI(host="example.com"),
        fields=fields if fields is not None else Fields(),
        body=b"",
    )


def _sign(request: HTTPRequest) -> HTTPRequestInterface:
    context = RequestContext[Any, HTTPRequest](
        request=Mock(), transport_request=request, properties=Mock()
    )
    return RecursionDetectionInterceptor().modify_before_signing(context)


def _trace_header(request: HTTPRequestInterface) -> list[str] | None:
    if TRACE_ID_HEADER not in request.fields:
        return None
    return request.fields[TRACE_ID_HEADER].values


def _set_env(
    monkeypatch: pytest.MonkeyPatch,
    *,
    function_name: str | None = None,
    trace_id: str | None = None,
) -> None:
    for name, value in (
        ("AWS_LAMBDA_FUNCTION_NAME", function_name),
        ("_X_AMZN_TRACE_ID", trace_id),
    ):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)


# SEP test case 1.
def test_omits_header_outside_lambda(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch)

    assert _trace_header(_sign(_request())) is None


# SEP test case 2.
def test_adds_header_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, function_name="some-function", trace_id=_TRACE_ID)

    assert _trace_header(_sign(_request())) == [_TRACE_ID]


# SEP test case 3.
def test_omits_header_without_function_name(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, trace_id=_TRACE_ID)

    assert _trace_header(_sign(_request())) is None


# SEP test case 4.
def test_keeps_existing_header(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, function_name="some-function", trace_id="EnvValue")
    fields = Fields([Field(name=TRACE_ID_HEADER, values=["OriginalValue"])])

    assert _trace_header(_sign(_request(fields))) == ["OriginalValue"]


# SEP test case 5: control characters are percent encoded.
def test_percent_encodes_control_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, function_name="some-function", trace_id="first\nsecond")

    assert _trace_header(_sign(_request())) == ["first%0Asecond"]


# 0x00 is excluded: the OS rejects a null byte inside an environment variable,
# so it can never reach the interceptor.
@pytest.mark.parametrize("character", [chr(code) for code in range(0x01, 0x20)])
def test_percent_encodes_every_control_character(
    monkeypatch: pytest.MonkeyPatch, character: str
) -> None:
    _set_env(monkeypatch, function_name="some-function", trace_id=f"a{character}b")

    header = _trace_header(_sign(_request()))

    assert header is not None
    assert header[0] == f"a%{ord(character):02X}b"


# SEP test case 6: characters the SEP requires to be sent as is.
def test_does_not_encode_trace_id_characters(monkeypatch: pytest.MonkeyPatch) -> None:
    trace_id = "test123-=;:+&[]{}\"'"
    _set_env(monkeypatch, function_name="some-function", trace_id=trace_id)

    assert _trace_header(_sign(_request())) == [trace_id]


def test_does_not_encode_comma(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, function_name="some-function", trace_id="a:0,b:5")

    assert _trace_header(_sign(_request())) == ["a:0,b:5"]


def test_reads_environment_on_every_request(monkeypatch: pytest.MonkeyPatch) -> None:
    # Lambda rewrites the trace ID for each invocation, and a client may be
    # reused across them, so the interceptor must not cache the value.
    interceptor = RecursionDetectionInterceptor()
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "some-function")
    headers: list[list[str] | None] = []

    for trace_id in ("Root=1-first", "Root=1-second"):
        monkeypatch.setenv("_X_AMZN_TRACE_ID", trace_id)
        request = _request()
        context = RequestContext[Any, HTTPRequest](
            request=Mock(), transport_request=request, properties=Mock()
        )
        headers.append(_trace_header(interceptor.modify_before_signing(context)))

    assert headers == [["Root=1-first"], ["Root=1-second"]]


def test_keeps_existing_header_set_with_different_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_env(monkeypatch, function_name="some-function", trace_id="EnvValue")
    fields = Fields([Field(name="x-amzn-trace-id", values=["OriginalValue"])])

    assert _trace_header(_sign(_request(fields))) == ["OriginalValue"]


@pytest.mark.parametrize(
    "function_name,trace_id",
    [("", _TRACE_ID), ("some-function", ""), ("", "")],
)
def test_treats_empty_environment_variables_as_unset(
    monkeypatch: pytest.MonkeyPatch, function_name: str, trace_id: str
) -> None:
    _set_env(monkeypatch, function_name=function_name, trace_id=trace_id)

    assert _trace_header(_sign(_request())) is None


def test_leaves_other_fields_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_env(monkeypatch, function_name="some-function", trace_id=_TRACE_ID)
    fields = Fields([Field(name="User-Agent", values=["aws-sdk-python/1.0.0"])])

    result = _sign(_request(fields))

    assert result.fields["User-Agent"].values == ["aws-sdk-python/1.0.0"]
    assert len(list(result.fields)) == 2

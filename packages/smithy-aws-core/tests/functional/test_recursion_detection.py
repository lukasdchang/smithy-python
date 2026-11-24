#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
"""End-to-end recursion detection through a generated AWS client.

Drives the generated awsJson1.0 client (``codegen-output/aws-json-1-0``) and checks
the ``X-Amzn-Trace-Id`` header the SDK puts on the wire.
"""

import os
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("awsjson10")

from awsjson10.aio.client import AsyncJSONRPC10Client
from awsjson10.aio.config import AsyncJSONRPC10Config
from awsjson10.client import JSONRPC10Client
from awsjson10.config import JSONRPC10Config
from smithy_aws_core.interceptors.recursion_detection import TRACE_ID_HEADER
from smithy_core.interceptors import Interceptor, RequestContext
from smithy_http import Field, HTTPResponse, tuples_to_fields
from smithy_http.interfaces import HTTPRequest

_JSON_CONTENT_TYPE = ("content-type", "application/x-amz-json-1.0")
_TRACE_ID = (
    "Root=1-5759e988-bd862e3fe1be46a994272793;Parent=53995c3f42cd8ad8;"
    "Sampled=1;lineage=a87bd80c:0,68fd508a:5"
)


class _RecordingTransport:
    """Returns empty responses and records the trace ID of every request."""

    TIMEOUT_EXCEPTIONS = ()

    def __init__(self, *, throttle_first: bool = False) -> None:
        self.trace_ids: list[list[str] | None] = []
        self._throttle_first = throttle_first

    def send(self, request: Any, *, request_config: Any = None) -> HTTPResponse:
        if TRACE_ID_HEADER in request.fields:
            self.trace_ids.append(request.fields[TRACE_ID_HEADER].values)
        else:
            self.trace_ids.append(None)
        if self._throttle_first and len(self.trace_ids) == 1:
            return HTTPResponse(
                status=429,
                fields=tuples_to_fields([_JSON_CONTENT_TYPE]),
                body=b'{"__type":"ThrottlingException"}',
            )
        return HTTPResponse(
            status=200,
            fields=tuples_to_fields([_JSON_CONTENT_TYPE]),
            body=b"{}",
        )


class _AsyncRecordingTransport(_RecordingTransport):
    async def send(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, request: Any, *, request_config: Any = None
    ) -> HTTPResponse:
        return super().send(request, request_config=request_config)


class _StaticTraceIdInterceptor(Interceptor[Any, Any, HTTPRequest, None]):
    """Stands in for a caller that sets its own trace ID."""

    def modify_before_signing(
        self, context: RequestContext[Any, HTTPRequest]
    ) -> HTTPRequest:
        request = context.transport_request
        request.fields.set_field(Field(name=TRACE_ID_HEADER, values=["CallerValue"]))
        return request


@pytest.fixture(params=[True, False], ids=["sync", "async"])
def sync(request: pytest.FixtureRequest) -> bool:
    return request.param


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the host's AWS settings, and any real Lambda variables, out of the call.
    for name in list(os.environ):
        if name.startswith("AWS_") or name == "_X_AMZN_TRACE_ID":
            monkeypatch.delenv(name)


async def _call(
    sync: bool,
    tmp_path: Path,
    transport: _RecordingTransport,
    calls: int = 1,
    **config: Any,
) -> list[list[str] | None]:
    missing = str(tmp_path / "missing")
    config = {
        "config_file_path": missing,
        "credentials_file_path": missing,
        "region": "us-west-2",
        "aws_access_key_id": "AKID",
        "aws_secret_access_key": "secret",
        "transport": transport,
        **config,
    }
    if sync:
        client = JSONRPC10Client(config=JSONRPC10Config.resolve(**config))
        for _ in range(calls):
            client.empty_input_and_empty_output()
    else:
        async_client = AsyncJSONRPC10Client(
            config=await AsyncJSONRPC10Config.resolve(**config)
        )
        for _ in range(calls):
            await async_client.empty_input_and_empty_output()
    return transport.trace_ids


def _transport(sync: bool, **kwargs: Any) -> _RecordingTransport:
    return (_RecordingTransport if sync else _AsyncRecordingTransport)(**kwargs)


async def test_sends_trace_id_in_lambda(
    sync: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "some-function")
    monkeypatch.setenv("_X_AMZN_TRACE_ID", _TRACE_ID)

    assert await _call(sync, tmp_path, _transport(sync)) == [[_TRACE_ID]]


async def test_omits_trace_id_outside_lambda(sync: bool, tmp_path: Path) -> None:
    assert await _call(sync, tmp_path, _transport(sync)) == [None]


async def test_sends_current_trace_id_from_one_client(
    sync: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A client outlives one invocation, so each call must carry its own trace ID.
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "some-function")
    monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-first")
    transport = _transport(sync)
    missing = str(tmp_path / "missing")
    shared: dict[str, Any] = {
        "config_file_path": missing,
        "credentials_file_path": missing,
        "region": "us-west-2",
        "aws_access_key_id": "AKID",
        "aws_secret_access_key": "secret",
        "transport": transport,
    }

    if sync:
        client = JSONRPC10Client(config=JSONRPC10Config.resolve(**shared))
        client.empty_input_and_empty_output()
        monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-second")
        client.empty_input_and_empty_output()
    else:
        async_client = AsyncJSONRPC10Client(
            config=await AsyncJSONRPC10Config.resolve(**shared)
        )
        await async_client.empty_input_and_empty_output()
        monkeypatch.setenv("_X_AMZN_TRACE_ID", "Root=1-second")
        await async_client.empty_input_and_empty_output()

    assert transport.trace_ids == [["Root=1-first"], ["Root=1-second"]]


async def test_sends_trace_id_on_every_retry(
    sync: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "some-function")
    monkeypatch.setenv("_X_AMZN_TRACE_ID", _TRACE_ID)

    trace_ids = await _call(sync, tmp_path, _transport(sync, throttle_first=True))

    assert trace_ids == [[_TRACE_ID], [_TRACE_ID]]


async def test_caller_interceptor_wins(
    sync: bool, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Caller plugins run after the client's own, so a caller's trace ID is sent
    # instead of the environment's, with no duplicate value.
    monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "some-function")
    monkeypatch.setenv("_X_AMZN_TRACE_ID", _TRACE_ID)
    transport = _transport(sync)
    missing = str(tmp_path / "missing")
    shared: dict[str, Any] = {
        "config_file_path": missing,
        "credentials_file_path": missing,
        "region": "us-west-2",
        "aws_access_key_id": "AKID",
        "aws_secret_access_key": "secret",
        "transport": transport,
    }

    def plugin(config: Any) -> None:
        config.interceptors.append(_StaticTraceIdInterceptor())

    if sync:
        client = JSONRPC10Client(config=JSONRPC10Config.resolve(**shared))
        client.empty_input_and_empty_output(plugins=[plugin])
    else:
        async_client = AsyncJSONRPC10Client(
            config=await AsyncJSONRPC10Config.resolve(**shared)
        )
        await async_client.empty_input_and_empty_output(plugins=[plugin])

    assert transport.trace_ids == [["CallerValue"]]

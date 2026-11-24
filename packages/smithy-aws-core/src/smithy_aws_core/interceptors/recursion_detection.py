#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0
import os
from typing import Any
from urllib.parse import quote

from smithy_core.interceptors import Interceptor, RequestContext
from smithy_http import Field
from smithy_http.interfaces import HTTPRequest

TRACE_ID_HEADER = "X-Amzn-Trace-Id"
"""Header carrying the X-Ray trace ID, including the Lambda invocation lineage."""

_LAMBDA_FUNCTION_NAME_ENV = "AWS_LAMBDA_FUNCTION_NAME"
_TRACE_ID_ENV = "_X_AMZN_TRACE_ID"

# Characters a trace ID may contain, which services do not expect to be encoded.
# quote() also leaves alphanumeric characters alone.
_SAFE_CHARS = "-=;:+&[]{}\"',"

__all__ = ["TRACE_ID_HEADER", "RecursionDetectionInterceptor"]


class RecursionDetectionInterceptor(Interceptor[Any, Any, HTTPRequest, None]):
    """Propagates the Lambda trace ID so services can detect recursive invocations.

    Lambda publishes the invocation lineage in ``_X_AMZN_TRACE_ID``, and services use
    it to halt loops such as a function that writes to the bucket that triggers it.
    This interceptor copies that value into the ``X-Amzn-Trace-Id`` header when the
    client runs in Lambda and the request carries no trace ID of its own. It never
    inspects the lineage.
    """

    def modify_before_signing(
        self, context: RequestContext[Any, HTTPRequest]
    ) -> HTTPRequest:
        request = context.transport_request

        # Read on every attempt: Lambda rewrites the trace ID per invocation,
        # and a client may outlive one.
        if not os.environ.get(_LAMBDA_FUNCTION_NAME_ENV):
            return request

        trace_id = os.environ.get(_TRACE_ID_ENV)
        if not trace_id:
            return request

        # A value from the model, the caller, or a service customization wins.
        if TRACE_ID_HEADER in request.fields:
            return request

        request.fields.set_field(
            Field(name=TRACE_ID_HEADER, values=[quote(trace_id, safe=_SAFE_CHARS)])
        )
        return request

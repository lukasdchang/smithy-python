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

# Characters that must not be percent encoded, in addition to the alphanumeric
# characters quote() always leaves alone. Services do not expect percent encoding
# in a trace ID, so encoding is limited to characters that would otherwise break
# the HTTP request.
_SAFE_CHARS = "-=;:+&[]{}\"',"

__all__ = ["TRACE_ID_HEADER", "RecursionDetectionInterceptor"]


class RecursionDetectionInterceptor(Interceptor[Any, Any, HTTPRequest, None]):
    """Propagates the Lambda trace ID so services can detect recursive invocations.

    A misconfigured resource can invoke a Lambda function in an endless loop, for
    example a function that writes to the S3 bucket that triggers it. Lambda
    publishes the invocation lineage in the ``_X_AMZN_TRACE_ID`` environment
    variable, and services use it to detect and halt such loops. This interceptor
    copies that value into the ``X-Amzn-Trace-Id`` header; it never inspects or
    modifies the lineage itself.

    The header is only added when the client runs inside a Lambda function and the
    request does not already carry a trace ID from the model, the caller, or a
    service customization.
    """

    def modify_before_signing(
        self, context: RequestContext[Any, HTTPRequest]
    ) -> HTTPRequest:
        request = context.transport_request

        # The environment is read on every attempt because Lambda rewrites the
        # trace ID for each invocation, and a client may outlive one invocation.
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

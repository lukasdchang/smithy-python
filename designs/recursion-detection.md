# Recursion detection

## Summary

When an AWS client runs inside a Lambda function, it adds an `X-Amzn-Trace-Id`
header to every request. The header value is copied from the `_X_AMZN_TRACE_ID`
environment variable, which Lambda sets for each invocation. AWS services use
the `lineage` part of that value to find and stop runaway loops, for example a
Lambda that writes to the S3 bucket that triggers it.

The SDK only carries the value. It never reads, counts, or changes `lineage`.
Loop detection and the loop limit live in the services.

This change implements the
[Recursion Detection SEP](https://code.amazon.com/packages/AwsDrSeps/blobs/main/--/seps/accepted/shared/recursion-detection.md)
for smithy-python generated AWS clients.

## Background

### What the header looks like

```
X-Amzn-Trace-Id: Root=1-5759e988-bd862e3fe1be46a994272793;Parent=53995c3f42cd8ad8;Sampled=1;lineage=a87bd80c:0,68fd508a:5
```

| Part | Written by | Read by |
|---|---|---|
| Header name | SDK (this change) | AWS services |
| `Root`, `Parent`, `Sampled` | Lambda | X-Ray |
| `lineage` | Lambda | Lambda, SQS, SNS, S3 (loop detection) |

### Where the value comes from

```
Lambda service
  -> Lambda-Runtime-Trace-Id header on each new event
  -> Lambda Python runtime (awslambdaric) sets os.environ["_X_AMZN_TRACE_ID"]
  -> RecursionDetectionInterceptor reads os.environ
  -> X-Amzn-Trace-Id request header
```

The Python runtime rewrites `_X_AMZN_TRACE_ID` for every invocation
([`bootstrap.py`](https://github.com/aws/aws-lambda-python-runtime-interface-client/blob/main/awslambdaric/bootstrap.py)).
A client created once and reused across invocations must therefore read the
variable on every request.

### Current state

smithy-python does not send `X-Amzn-Trace-Id` today. The only reference to it is
in the SigV4 signer, which excludes the header from signing
(`packages/aws-sdk-signers/src/aws_sdk_signers/_sigv4.py`). Besides recursion
detection, adding the header also lets X-Ray traces continue from a Lambda into
the AWS services it calls.

## Goals

- Follow every MUST and MUST NOT in the SEP.
- Add the header to requests to every AWS service when running in Lambda.
- Never override a trace header the customer or the model already set.
- Use the trace ID of the current invocation, even when a client is reused.

## Non-goals

- Reading or changing the `lineage` value.
- An SDK setting to turn the feature off. The SEP defines none. Customers turn
  off loop detection on the Lambda function (`RecursiveLoop`).
- Runtimes other than Lambda. The SEP may cover them later.

## Behavior

The interceptor runs on every request attempt, in every environment. It adds the
header only when all checks pass, in this order:

| # | Check | If no | If yes |
|---|---|---|---|
| 1 | Is `AWS_LAMBDA_FUNCTION_NAME` set and non-empty? | Leave request alone | Go to 2 |
| 2 | Is `_X_AMZN_TRACE_ID` set and non-empty? | Leave request alone | Go to 3 |
| 3 | Is `X-Amzn-Trace-Id` already on the request? | Go to 4 | Leave request alone |
| 4 | Percent-encode the value and add the header | | Done |

Example, inside a Lambda:

```
Before:                               After:
PUT /my-key                           PUT /my-key
Host: my-bucket.s3.amazonaws.com      Host: my-bucket.s3.amazonaws.com
User-Agent: aws-sdk-python/...        User-Agent: aws-sdk-python/...
                                      X-Amzn-Trace-Id: Root=1-...;Parent=...;Sampled=1;lineage=...
```

Outside a Lambda, the request is unchanged. The cost is one environment variable
lookup per attempt.

## Design decisions

| Decision | Choice | Reason |
|---|---|---|
| Mechanism | New `Interceptor`, modeled on `smithy_http.interceptors.user_agent.UserAgentInterceptor.modify_before_signing` | Same hook and the same `fields.set_field(Field(...))` call already used to add a header |
| Hook | `modify_before_signing` | Runs after serialization, so headers from modeled members are already present for check 3. Runs once per attempt. |
| Package | `smithy-aws-core` (`smithy_aws_core/interceptors/recursion_detection.py`) | Lambda and the trace header are AWS-specific. `smithy-http` stays protocol-generic. |
| When env vars are read | On every attempt, inside the hook | Lambda changes `_X_AMZN_TRACE_ID` per invocation. Reading it in `__init__` would send a stale trace ID from a reused client. |
| Empty values | Treated as unset (`if value:`) | An empty header value carries no trace ID |
| Check order | Function name, then trace ID, then existing header | Matches the SEP text. Cheap env lookups first. |
| Header name | Module constant `TRACE_ID_HEADER = "X-Amzn-Trace-Id"` | One spelling, reused in tests |
| Existing-header check | `TRACE_ID_HEADER in request.fields` | `Fields` lowercases names, so any casing set by the customer or a modeled member is found |
| Header-safe value | `urllib.parse.quote(trace_id, safe='-=;:+&[]{}"\',')` | Standard library. Same call botocore uses. Verified against the SEP rules (see below). |
| Codegen | New `AwsRecursionDetectionIntegration` class | One feature per integration, like `AwsUserAgentIntegration` and `AwsFlexibleChecksumsIntegration` |
| Which clients | AWS services with an HTTP protocol (`CodegenUtils.isAwsService` and `isHttpProtocol()`) | The header is HTTP. The feature targets AWS services. |
| Opt-out setting | None | The SEP defines none. YAGNI. |
| Retries | Header is added on every attempt | Each attempt starts from a fresh transport request, so earlier changes are not kept |
| Sync and async clients | One sync interceptor shared by both | Interceptor hooks are sync. The plugin file gets a sync twin through `SyncVariant.writeSyncTwin`, like the other AWS plugins. |

## SEP compliance

| SEP requirement | How this design meets it |
|---|---|
| MUST check whether `X-Amzn-Trace-Id` is already set | Check 3 |
| If not set, MUST look for `AWS_LAMBDA_FUNCTION_NAME` and `_X_AMZN_TRACE_ID` | Checks 1 and 2, read from `os.environ` on every attempt |
| If both are set, MUST add `X-Amzn-Trace-Id` with the value of `_X_AMZN_TRACE_ID` | Step 4 |
| A customer-provided value for a modeled trace header MUST take priority | Modeled members are serialized before `modify_before_signing`, so check 3 finds them |
| A custom header set by the customer MUST take priority | Check 3 finds it. See open question 1 for customer interceptors. |
| A service customization that adds the header MUST take priority | Check 3 finds it, as long as the customization runs first. None exist today. |
| MUST NOT insert the value without validating OR percent-encoding | Always percent-encoded with `quote` |
| 0x00-0x1F MUST be percent-encoded | `quote` encodes all 32 (verified) |
| Alphanumerics and `- = ; : + & [ ] { } " ' ,` MUST NOT be percent-encoded | All passed through unchanged (verified) |

Verified `quote` output:

| Input | Output |
|---|---|
| `first\nsecond` | `first%0Asecond` |
| `test123-=;:+&[]{}"'` | `test123-=;:+&[]{}"'` |
| A real trace ID | Unchanged |
| space, `/`, DEL, `é` | `%20`, `%2F`, `%7F`, `%C3%A9` |

The last row shows characters the SEP does not list. Encoding them is allowed,
and they never appear in a real trace ID.

## Implementation

### Runtime: `packages/smithy-aws-core/src/smithy_aws_core/interceptors/recursion_detection.py`

```python
import os
from typing import Any
from urllib.parse import quote

from smithy_core.interceptors import Interceptor, RequestContext
from smithy_http import Field
from smithy_http.interfaces import HTTPRequest

__all__ = ["RecursionDetectionInterceptor"]

TRACE_ID_HEADER = "X-Amzn-Trace-Id"
_LAMBDA_FUNCTION_NAME_ENV = "AWS_LAMBDA_FUNCTION_NAME"
_TRACE_ID_ENV = "_X_AMZN_TRACE_ID"
# Characters the SEP says MUST NOT be percent-encoded, besides alphanumerics.
_SAFE_CHARS = "-=;:+&[]{}\"',"


class RecursionDetectionInterceptor(Interceptor[Any, Any, HTTPRequest, None]):
    """Adds the Lambda trace ID header so services can detect recursive loops."""

    def modify_before_signing(
        self, context: RequestContext[Any, HTTPRequest]
    ) -> HTTPRequest:
        request = context.transport_request
        if not os.environ.get(_LAMBDA_FUNCTION_NAME_ENV):
            return request
        trace_id = os.environ.get(_TRACE_ID_ENV)
        if not trace_id:
            return request
        if TRACE_ID_HEADER in request.fields:
            return request
        value = quote(trace_id, safe=_SAFE_CHARS)
        request.fields.set_field(Field(name=TRACE_ID_HEADER, values=[value]))
        return request
```

### Codegen: `codegen/aws/core/.../AwsRecursionDetectionIntegration.java`

- Implements `PythonIntegration`.
- `getClientPlugins` returns nothing unless `CodegenUtils.isAwsService(context)`
  and `context.applicationProtocol().isHttpProtocol()`.
- Writes a plugin function into the client's `aio` module and its sync twin:

  ```python
  def aws_recursion_detection_plugin(config: $1T):
      config.interceptors.append($2T())
  ```

- No new config properties.
- Registered in
  `codegen/aws/core/src/main/resources/META-INF/services/software.amazon.smithy.python.codegen.integrations.PythonIntegration`.

### Changelog

Add a `feature` fragment under `packages/smithy-aws-core/.changes/next-release/`.
CI checks for one.

## Testing

Unit tests in `packages/smithy-aws-core/tests/unit/interceptors/test_recursion_detection.py`,
using `monkeypatch` for environment variables.

| # | Case | Expected |
|---|---|---|
| 1 | No env vars | No header |
| 2 | Both env vars set | Header equals the trace ID |
| 3 | Only `_X_AMZN_TRACE_ID` set | No header |
| 4 | Both set, header already on request | Original value kept |
| 5 | Trace ID with `\n` | `first%0Asecond` |
| 6 | Trace ID with `-=;:+&[]{}"'` | Unchanged |
| 7 | Env changes between two calls on one interceptor | Each call gets its own trace ID |
| 8 | Header already set in a different case (`x-amzn-trace-id`) | Original value kept |
| 9 | Empty `AWS_LAMBDA_FUNCTION_NAME` or `_X_AMZN_TRACE_ID` | No header |

Cases 1-6 are the SEP test cases. Cases 7-9 cover this design's decisions.

Codegen and functional checks:

- A generated AWS client lists `aws_recursion_detection_plugin` in `_client_plugins`.
- A non-AWS generated client does not.
- A retried request still has the header on the second attempt.

## Resolved during implementation

- **Order of customer interceptors.** `_prepare` in
  `packages/smithy-core/src/smithy_core/_client.py` applies the client's own
  plugins before the caller's, so a caller's interceptor runs after this one and
  its `set_field` replaces the environment's value. The caller's trace ID is
  sent, with no duplicate value.
  (`test_caller_interceptor_wins`)
- **Null bytes.** The SEP requires encoding `0x00-0x1F`. `quote` encodes all of
  them, but the operating system rejects a null byte inside an environment
  variable, so `0x00` can never reach the interceptor. The parametrized test
  covers `0x01-0x1F`.

## Follow-up

- After release, update the Python row in the SEP implementation table.

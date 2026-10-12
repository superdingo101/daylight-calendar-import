# Parser provider failures

The configured Home Assistant AI Task remains the parser boundary. Source
activity records include a stable `failure_code` and generic recovery advice for
provider failures. Original exception messages, provider response bodies,
credentials, request URLs and prompts are never copied into these fields.
Boundary errors suppress upstream exception chaining in ordinary tracebacks.

| Category | Recovery |
| --- | --- |
| `provider_timeout` | Wait before submitting the source again. |
| `provider_rate_limited` | Wait and check the provider quota. |
| `provider_unavailable` | Check the AI Task entity and its connection. |
| `provider_authentication` | Check provider credentials and account permissions. |
| `provider_invalid_response` | Check the configured AI Task and try again. |
| `provider_error` | Check the configured AI Task; the failure had no recognized structured category. |

Classification uses typed built-in, aiohttp and httpx timeout/connection errors,
documented numeric HTTP
status codes (429, 401/403, 500–599) and explicit exception causes. Exception text
and implicit exception context are not interpreted. Traversal is bounded and
cycle-safe. A resolved unavailable AI Task is rejected before invocation;
text-only submissions retain Home Assistant's existing lazy entity lookup.
Malformed AI results are classified separately. Candidate validation failures
handled by the existing parser still produce per-event warnings; unexpected
malformed-result exceptions are contained as invalid responses. Provider entity
discovery and exception descriptors are guarded without copying their errors.

The category and guidance are committed in the existing atomic failed-source
transaction, including any opted-in failure notification. The notification
remains generic. Older activities without `failure_code` continue to load.
This change adds no retries or time limits. Email provider failures retain their
existing retryable disposition and are not acknowledged as terminal sources;
manual submissions propagate the safe provider exception. Cancellation remains
cancellation and does not become a timeout.

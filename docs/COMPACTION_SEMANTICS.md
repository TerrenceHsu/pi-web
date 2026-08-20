# Compaction Semantics

## Boundary contract

`CompactionConfig.boundary_mode` defaults to `turn`. A turn starts at a user
message and includes every following assistant message and tool result up to the
next user message. The retained suffix therefore starts at a user message; a
tool result can never survive without the assistant tool call that introduced
it.

`keep_last_n_turns` retains a fixed number of complete recent turns.
`keep_recent_tokens`, when provided, takes precedence and retains as many recent
complete turns as fit the target. The newest turn is always retained whole even
when it alone exceeds the target. `boundary_mode="message"` remains an explicit
legacy mode for callers that intentionally need message-count slicing.

## Token and window accounting

Compaction and request preflight share `mixed-char-v1` through
`estimate_message_tokens`. `CompactionTokenStats` records:

- provider-visible message tokens before and after compaction;
- estimated input and projected input-plus-output-reserve before and after;
- the configured context window, ratios, estimator version and approximate flag.

The Web endpoint supplies its canonical preflight `ContextEstimate`, so system
prompt, tool definitions, session instructions and durable memory remain in the
fixed portion of the before/after calculation. API responses return both
`budget_before` and the normal post-compaction `budget`.

## Summary prefix and chained compaction

Summary text is stored without an instruction wrapper. At the Provider boundary
it is projected as a user message with pi-compatible stable text and explicit
`<summary>...</summary>` boundaries.

When an earlier `SummaryMessage` enters the compacted prefix, its body is exposed
to the generator as `CompactionInput.previous_summary`. The default generator
folds it once into `## Prior Summary`; it does not treat the wrapper as new
conversation content. This keeps chained compactions iterative instead of
accumulating nested display prefixes.

## Retry contract

`CompactionRetryPolicy` is opt-in and defaults to one attempt. Automatic retries
cover only transient `TimeoutError`, `ConnectionError`, provider rate-limit and
provider-stream failures. Authentication, configuration, protocol, type and
cancellation errors are not retried.

Every attempt receives a fresh deep copy of the same prepared
`CompactionInput`. No Agent, Session or SQLite state changes until a complete
summary result exists. Exhaustion re-raises the last error and leaves the source
messages unchanged. Optional callbacks observe `retry_scheduled`,
`retry_attempt_start` and `retry_finished` lifecycle events.

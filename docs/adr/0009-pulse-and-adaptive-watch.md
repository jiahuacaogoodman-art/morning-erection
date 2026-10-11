# ADR 0009: Pulse and adaptive website observation

Status: accepted for the local Pulse application.

## Product contract

The agent conversation is the primary surface. Pulse is one small, central status island:
quiet observation, a brief change notice, visible work, and persistent human attention.
Hover reveals context; clicking reveals the task; settings alone reveal the extractor,
session handle and polling policy. No browser dashboard is placed in the conversation.
The projection uses a source kind so future connectors can share this surface. Only Web
is implemented in this change; Mail, Calendar and GitHub are not advertised as connected.

## Runtime extension

Existing interval triggers, occurrence deduplication, leases, reducers, approval digests,
and dispatch/reconciliation remain the execution path. An **opt-in** `source.resource_scope.polling`
policy adjusts the main interval trigger after an observation commits. Unchanged trusted
snapshots increase the interval, changes reset it, transient errors back off, and HTTP
Retry-After is a lower bound. Positive deterministic jitter spreads checks without
requesting earlier than a rate limit. The current interval and due time are durable in
the existing TriggerRecord; no separate in-memory scheduler is introduced.
For `web_watch.v1`, observation identity excludes cadence/presentation/action-recipe
metadata while retaining URL, origins, extractor and target scope. Changing the interval
keeps the trusted baseline and cannot create a second initial wake. Reconfiguration
preserves the minimum interval, a server backoff deadline and authentication suspension.

Authentication walls and invalid extractors suspend automatic observation. A human
explicitly requests another check after logging in or fixing configuration. The last
trusted snapshot is kept, with its age and blocked state displayed. A manual check is
queued through the trigger and respects the minimum interval and server backoff; it
does not fetch in an HTTP handler. Paused/terminal tasks cannot be checked.

## Creation and actions

A short request produces a reviewable draft. Built-in course wording is deterministic;
unrecognised wording asks for an explicit field/operator/value instead of pretending to
understand it. A declarative extractor and browser session must be configured for a new
site. Start confirms the exact task spec and grant. Normal polling never calls a model.

An optional configured action planner copies only the user-confirmed action recipe,
checks current allowed tools and proposes it through the existing admission path.
It does not synthesize arbitrary instructions from webpage text. Browser operations
require a configured Computer Use sidecar, explicit screenshot egress, a postcondition
and an approval of the exact payload. Missing model capability is surfaced, not simulated.

The opt-in course demo uses real Chromium, a local HTTP course page, CSRF-checked POST,
the kernel's approval path, and post-action observation. Its deterministic demo tool is
limited to that local origin. It is not evidence of enrolling in a real university.

## Local application boundary

Pulse is a loopback-only application. Its browser session uses HttpOnly/SameSite cookies,
checks Host and Origin, requires a custom header on mutations, and exposes no sidecar
credentials or profile paths. The existing management API remains bearer-authenticated.
This local session mechanism is not a multi-user production authentication service.

## Acceptance

Exercise quiet -> change -> approval -> verified result in a real browser. Cover
pause/resume, stale/blocked observations, reconnect, expired approval, rejection,
adaptive cadence and restart persistence. Verify desktop/mobile and keyboard behavior.
Report public-site observation and any model-backed execution separately from the demo.

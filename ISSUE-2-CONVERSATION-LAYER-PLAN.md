# Issue #2: Natural WhatsApp Control And Live Meeting Recall

## Summary

Extend `OrbitWhatsAppService` so WhatsApp can control meetings and answer what is
happening inside an active Google Meet from live STT.

```text
WhatsApp message
  |
  +--> Meet URL ------------> reset dialogue -> join
  +--> /new ----------------> reset dialogue only
  +--> status/control ------> list/status/leave/stop
  +--> live discussion -----> active meeting STT -> cited answer
  +--> historical question -> memory retrieval -> cited answer
  +--> general message -----> dialogue-aware fallback
```

## Key Changes

- Add a pure deterministic intent parser and centralized executor in
  `orbit/whatsapp_service.py`.
- Support natural phrases for:
  - join from a Meet URL
  - list active meetings
  - current meeting status
  - leave meeting
  - stop recording / stop monitoring
  - `/new`
  - live discussion queries such as "what are people discussing?" and
    "summarize the meeting"
- Resolve active-meeting targets consistently:
  - one active meeting: use it automatically
  - multiple active meetings: ask which meeting code
  - explicit unknown code: report that it is not active
  - zero active meetings: explain that Orbit is not monitoring a meeting

## Live STT Recall

- Add an in-memory transcript-segment buffer to `MeetingState`.
- Append normalized Deepgram STT segments as they arrive, including speaker and
  timestamps when available.
- Answer live-discussion questions from relevant segments belonging only to the
  selected active session.
- For broad recap requests, retrieve a bounded representative sample across the
  entire active meeting.
- Cite transcript sources with meeting code, speaker when known, and timestamps.
- If audio has not started or STT captured too little context, say so and ask a
  useful follow-up. Never silently substitute historical company memory.

```text
Live STT segment
  |
  +--> persistent MemoryService indexing
  |
  +--> MeetingState live transcript buffer
            |
            +--> select active meeting
            +--> retrieve bounded relevant segments
            +--> generate concise cited answer
```

## Lifecycle And History

- Add cooperative meeting shutdown with timeout fallback.
- Treat `leave`, `stop recording`, and `stop monitoring` as aliases for ending
  the Orbit session. Do not add pause/resume state.
- Report success only after browser monitoring and STT cleanup complete.
- Store bounded dialogue-only WhatsApp history:
  - direct inbound messages and direct replies only
  - maximum 12 turns
  - maximum 2,000 characters per message
  - reset on `/new` or any received Meet URL
  - reset dialogue only; never stop meetings implicitly

## Test Plan

Run:

```bash
.venv-browser-use/bin/python -m unittest discover -s tests
```

Add coverage for:

- intent parsing and existing fallback routing
- unauthorized senders
- zero, one, and multiple active meetings
- explicit and unknown meeting codes
- cooperative stop success, timeout, and cleanup failure
- `/new` and Meet URL dialogue resets
- history bounds and truncation
- live STT questions using only the selected active session
- entire-meeting recap using bounded representative segments
- timestamped source citations
- missing, sparse, and unavailable STT context
- focused prompt eval fixtures for natural replies, follow-ups, citations,
  ambiguity, and non-fabrication

Verified baseline when this plan was written: `39` tests pass on HEAD `935ca23`.

## NOT In Scope

- Persistent WhatsApp dialogue history until restart continuity or multiple
  workers are needed.
- Pause/resume recording.
- New database schema.
- Dashboard, Slack, email, or document integrations.
- Observability and browser-selector reliability work unrelated to issue #2.

## Implementation Tasks

- [ ] Add deterministic WhatsApp intent parsing and execution.
- [ ] Add bounded resettable dialogue history.
- [ ] Add live transcript buffering and active-session segment retrieval.
- [ ] Add cited live-discussion and entire-meeting recap answers.
- [ ] Add cooperative meeting shutdown with truthful reporting.
- [ ] Add complete unit tests and conversation eval fixtures.
- [ ] Update `README.md` with controls, `/new`, and live-recall behavior.
- [ ] Add a TODO for durable WhatsApp history when scaling requires it.

## Assumptions

- WhatsApp remains a single-process pilot control plane.
- Live-recall answers use the entire selected active meeting as the source pool,
  but prompts include only bounded relevant segments.
- When multiple meetings are active, Orbit asks for a meeting code rather than
  merging discussions.

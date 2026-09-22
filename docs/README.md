# Design documents

The reasoning behind `src/reminders/`, in the order it was produced. Five
documents, roughly 7,500 lines, and **none of them is required reading** to run
the project or review the result — [`../SUBMISSION.md`](../SUBMISSION.md) is the
short version and stands alone.

They are kept because they are the audit trail: what was believed before the
code existed, where that turned out to be wrong, and which failure corrected it.

## Where to start

| If you want | Read | Length |
| --- | --- | --- |
| the result, and nothing else | [`../SUBMISSION.md`](../SUBMISSION.md) | 400 |
| why the problem is harder than it looks | [`ANALYSIS.md`](ANALYSIS.md) | 1,071 |
| what "correct" is defined to mean | [`CORRECTNESS_MODEL.md`](CORRECTNESS_MODEL.md) | 687 |
| how the pieces fit, and why each seam is where it is | [`ARCHITECTURE.md`](ARCHITECTURE.md) | 3,602 |
| what must ultimately be true | [`BUILD_PLAN.md`](BUILD_PLAN.md) | 248 |
| how it was actually built, failure by failure | [`STAGES.md`](STAGES.md) | 1,908 |

## What each one is for

- **[`ANALYSIS.md`](ANALYSIS.md)** — the first pass at the problem, before any
  code. Problem framing, the concepts the domain forces on you, and the
  ambiguities in the brief. **Partially superseded:** its §3 (invariants), §4
  (failure map) and §12 (state machine) were replaced by
  [`CORRECTNESS_MODEL.md`](CORRECTNESS_MODEL.md); the rest still stands, and the
  document says so at the top rather than being quietly edited.

- **[`CORRECTNESS_MODEL.md`](CORRECTNESS_MODEL.md)** — the authoritative
  statement of what correctness means here, produced by an adversarial pass
  against `ANALYSIS.md`. Invariants, the failure map, the state machine, and the
  eight questions that pass left open.

- **[`ARCHITECTURE.md`](ARCHITECTURE.md)** — the design, and the longest
  document by far. Settles all eight open questions and records four findings
  against `CORRECTNESS_MODEL.md`, one of which is a genuine over-claim in that
  document's crash matrix. Read §0 first; it is the list of things the rest of
  the document had to correct.

- **[`BUILD_PLAN.md`](BUILD_PLAN.md)** — the **destination**: the list of what
  must eventually be true, with nothing about sequencing.

- **[`STAGES.md`](STAGES.md)** — the **journey**: seventeen stages, each one
  *build something small that works → break it on purpose → fix what broke*.
  This is where the transcripts, the rules the build held itself to, and the
  record of where the plan was wrong all live.

`BUILD_PLAN.md` and `STAGES.md` are deliberately two files rather than one.
Confusing "what must be true" with "what do I do next" is what went wrong the
first time.

## Conventions

- Every document states its own status at the top — authoritative, superseded,
  or partially superseded. Superseded sections are marked, never deleted; a
  document that quietly rewrites its own history cannot be audited.
- Cross-references between these five are plain relative links, so the whole
  folder can be moved or published without rewriting them.
- The only things outside this folder that these documents point at are
  [`../problems/03-durable-reminders/README.md`](../problems/03-durable-reminders/README.md)
  (the brief, which is upstream and unmodified) and `../SUBMISSION.md`.

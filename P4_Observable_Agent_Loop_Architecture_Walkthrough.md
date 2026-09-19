# The Observable Agent Loop — An Architecture Walkthrough

**What this document is.** An explanation of how the system works and *why it is shaped the way it is*. It assumes you know nothing about TypeScript and nothing about the implementation. Every idea is introduced as a problem first, a solution second, and only then given its technical name.

**What this document is not.** It is not the build specification. The authoritative blueprint lives in `DESIGN.md` (the locked v3.1 design). Nothing here changes that design — this is a reading layer over it. When you finish this document you should be able to explain the architecture out loud, from memory, without opening the blueprint.

**Reading order.** Sections 1–4 are the foundation: the problem, the core idea, the shape of the system, and one complete run narrated end to end. If you read only those, you will understand the system. Sections 5–12 go deeper into responsibilities, failure behaviour, rationale, and alternatives.

---

## Table of contents

1. [What problem are we actually solving?](#1-what-problem-are-we-actually-solving)
2. [The one idea the whole system is built on](#2-the-one-idea-the-whole-system-is-built-on)
3. [The system at a glance](#3-the-system-at-a-glance)
4. [One complete investigation, narrated](#4-one-complete-investigation-narrated)
5. [The seven responsibilities](#5-the-seven-responsibilities)
6. [Data flow is not control flow](#6-data-flow-is-not-control-flow)
7. [At every step: what can happen?](#7-at-every-step-what-can-happen)
8. [When things go wrong: eleven stories](#8-when-things-go-wrong-eleven-stories)
9. [Why does this feature exist, and where does it belong?](#9-why-does-this-feature-exist-and-where-does-it-belong)
10. [Why here and not there?](#10-why-here-and-not-there)
11. [Things we could have done differently](#11-things-we-could-have-done-differently)
12. [How the system grew from dumb to reliable](#12-how-the-system-grew-from-dumb-to-reliable)
13. [The architecture in six pictures](#13-the-architecture-in-six-pictures)
14. [How I would explain this in an interview](#14-how-i-would-explain-this-in-an-interview)

---

# 1. What problem are we actually solving?

Someone on an incident call asks a question:

> *"Why did checkout-api error rates spike around 14:00 UTC on 2026-09-15?"*

That question cannot be answered by looking in one place. The answer lives scattered across four different systems — deployment records, metrics dashboards, application logs, and a runbook wiki. A human engineer would open four tabs, look at each in turn, and assemble a story. Crucially, **what they look at second depends on what they found first.**

We are building a small program that does that assembling. It receives the question, works out what information it needs, goes and gets it, and produces an answer that cites what it actually found.

## 1.1 What the program must be able to do

Thirteen capabilities, in the order they occur during a run:

1. **Understand the objective** it was given.
2. **Decide what information it needs** next — not from a fixed script, but based on what it already knows.
3. **Select an appropriate tool** from the ones available to it.
4. **Provide structured arguments** to that tool — not free text, but properly shaped parameters.
5. **Execute the tool** and get a result back.
6. **Receive the result** in a form it can reason about.
7. **Record what happened**, in order, so a human can audit it afterwards.
8. **Use the new information** to decide what to do next — this is the part that makes it an agent.
9. **Perform multiple steps** when one source is not enough.
10. **Recover from tool failures** rather than collapsing when something breaks.
11. **Stop** — either because it has enough evidence, or because it has hit a limit we imposed.
12. **Produce a final answer grounded in the evidence it collected**, not in things it invented.
13. **Leave behind an ordered, inspectable trace** of the entire run.

## 1.2 The thing that makes this hard

Look closely at capability 8. That is where the difficulty lives.

If we knew in advance which four tools to call and in what order, we would not need an agent at all. We would write four lines of code in sequence and be done. That program would work perfectly — for this one question.

But consider what actually happens in a real investigation:

> To search the logs, you need a **time window**. You do not know the time window until you have found out when the deploy happened. So the arguments to the third call are derived from the result of the first call.

That single dependency kills the fixed-sequence approach. You cannot write down the third step in advance because part of the third step is data that does not exist until the first step finishes.

And it compounds:

- If metrics show **no spike at all**, the whole investigation should pivot — maybe the alert was wrong. A fixed script would blindly continue anyway.
- If the logs show a **database timeout** rather than a payment-gateway timeout, the knowledge-base query should be different.
- If a tool is **down**, a fixed script simply breaks. An agent should carry on with what it has and say what it is missing.

So the program must be able to **choose its next action based on everything it has learned so far.** That is the defining property of an agent loop, and it is the whole reason this system exists in the shape it does.

## 1.3 A loop, not a pipeline

Two ways to organise the same four tool calls:

```
A PIPELINE — decided in advance, before any facts exist

   Objective
      ↓
   get_service_status  ──▶ result
      ↓
   get_metrics         ──▶ result
      ↓
   search_logs         ──▶ result
      ↓
   search_kb           ──▶ result
      ↓
   Answer

   The arrows are written by the programmer.
   Nothing that happens can change what comes next.
```

```
A LOOP — decided during the run, one step at a time

   Objective
      ↓
   ┌──────────────────────────────────┐
   │  What do I know so far?          │
   │  What is still missing?          │  ◀──┐
   │  What should I do about it?      │     │
   └──────────────┬───────────────────┘     │
                  ↓                         │
          one action, executed              │
                  ↓                         │
          a new fact is learned ────────────┘
                  │
                  ↓ (only when nothing important is missing)
               Answer

   The arrows are written at runtime, by the model,
   informed by everything collected so far.
```

The pipeline would produce a correct-looking demo for this one question and would be the wrong answer to the assignment. The brief asks for *"a real control loop rather than one hard-coded sequence or a single prompt."* More importantly, the pipeline would fall apart the moment anything unexpected happened — a missing tool, a surprising result, a different question.

**But a loop introduces a new problem that a pipeline never had.** In a pipeline, the programmer decides everything and the program does exactly that. In a loop, something else is deciding — and that something is a language model, which is fallible, occasionally inventive, and entirely capable of asking for the same tool four hundred times in a row.

Which brings us to the idea the entire architecture is built on.

---

# 2. The one idea the whole system is built on

If you remember one sentence from this document, make it this one:

> ### The model decides what *should* happen next. The runtime decides what is *allowed* to happen.

Everything else in the architecture — every check, every limit, every validation step, every recorded event — is an elaboration of that single split.

## 2.1 Why the split has to exist

It is tempting to think of the language model as the program. It is not. **The model is a proposer, not an executor.**

What the model actually produces is a *suggestion*: "I would like to call `search_logs` with these parameters." That suggestion arrives as text, from a statistical system, over a network, and it can be wrong in any of the following ways:

- It can name a tool that does not exist.
- It can send parameters of the wrong shape, or missing required fields.
- It can ask for a tool that has already failed twice in a row.
- It can propose an action that costs money, forever, in a tight loop.
- It can produce output that is not even well-formed.
- It can assert a conclusion and cite evidence it never collected.

The useful mental model is one every engineer already has:

> **Treat the model's output exactly as you would treat user input from the internet: as untrusted data that happens to be shaped like instructions.**

You would never take a string from a web form and execute it. You validate it, you check it against what is permitted, you bound how much work it can cause, and you log what you did. The model gets exactly the same treatment — not because it is malicious, but because *correctness should not depend on it behaving well.*

## 2.2 What each side owns

```
╔════════════════════════════════════════════════════════════════╗
║  THE MODEL OWNS — judgement                                    ║
║                                                                ║
║  • Which tool is appropriate for what I still need to know     ║
║  • What arguments to give it                                   ║
║  • Whether I have enough evidence to conclude                  ║
║  • What the evidence means                                     ║
║                                                                ║
║  These are questions with no mechanical answer. They need      ║
║  judgement about an open-ended problem. That is what the       ║
║  model is for, and nothing else in the system can do it.       ║
╚════════════════════════════════════════════════════════════════╝
                              │
                              │  proposes an action
                              ▼
╔════════════════════════════════════════════════════════════════╗
║  THE RUNTIME OWNS — authority                                  ║
║                                                                ║
║  • Whether this run may continue at all                        ║
║  • Whether that tool exists and is currently available         ║
║  • Whether those arguments are acceptable                      ║
║  • How long any single operation may take                      ║
║  • What happens when something fails                           ║
║  • When the run stops, and why                                 ║
║  • What gets written down                                      ║
║                                                                ║
║  These are questions with mechanical answers. They must be     ║
║  decided the same way every time, and they must not depend     ║
║  on the model cooperating.                                     ║
╚════════════════════════════════════════════════════════════════╝
                              │
                              │  dispatches, if permitted
                              ▼
╔════════════════════════════════════════════════════════════════╗
║  THE TOOLS OWN — capability                                    ║
║                                                                ║
║  • Doing one specific thing and returning a structured result  ║
║                                                                ║
║  A tool has no opinion about the investigation. It does not    ║
║  know what question is being answered or what was called       ║
║  before it. It is a capability, not a participant.             ║
╚════════════════════════════════════════════════════════════════╝
```

## 2.3 The test this split has to pass

Here is the standard the architecture holds itself to, and it is worth stating plainly because it explains a lot of later decisions:

> **A model that behaves badly should produce a bad answer — never a broken system, an unbounded bill, or a corrupted record of what happened.**

A model that asks for a nonexistent tool gets told so and gets a chance to correct itself. A model that never stops asking for tools gets stopped. A model that invents a citation has its answer rejected. In every one of those cases, the run ends in a structured, explainable state with an audit trail — not a crash, not an infinite loop, not a confident lie.

That is what "observable agent loop" means. Not that the agent is clever. That its behaviour is **bounded and inspectable** regardless of how clever it is.

---

# 3. The system at a glance

Here is the whole system, conceptually. No file names, no function names — just what happens and in what order.

```
                        ┌─────────────────────┐
                        │   USER OBJECTIVE    │
                        │  "Why did errors    │
                        │   spike at 14:00?"  │
                        └──────────┬──────────┘
                                   ▼
        ╔══════════════════════════════════════════════════════╗
        ║                  AGENT RUNTIME                       ║
        ║         (starts a run, owns the whole loop)          ║
        ╚══════════════════════════════════════════════════════╝
                                   │
          ┌────────────────────────▼─────────────────────────┐
          │                                                  │
          │   ①  MAY THE RUN CONTINUE?                       │
          │       Have we used too many steps, too many      │
          │       tool calls, or too much time?              │
          │                                                  │
          └───────┬──────────────────────────────┬───────────┘
                  │ No                           │ Yes
                  ▼                              ▼
          ┌───────────────┐        ┌─────────────────────────────┐
          │ STOP.         │        │ ② BUILD THE MODEL'S VIEW    │
          │ Report the    │        │    objective + what we've   │
          │ reason and    │        │    learned + which tools    │
          │ the evidence  │        │    are available + budget   │
          │ collected     │        │    left                     │
          │ so far.       │        └──────────────┬──────────────┘
          └───────────────┘                       ▼
                                   ┌─────────────────────────────┐
                                   │ ③ ASK THE MODEL:            │
                                   │    what should happen next? │
                                   └──────────────┬──────────────┘
                                                  ▼
                                   ┌─────────────────────────────┐
                                   │ ④ IS THE ANSWER WELL-FORMED?│
                                   │    (the model is untrusted) │
                                   └───────┬──────────────┬──────┘
                                    No     │              │ Yes
                                           ▼              ▼
                            ┌──────────────────┐   ┌─────────────────┐
                            │ Tell the model   │   │ What did it     │
                            │ what was wrong.  │   │ ask for?        │
                            │ Let it retry,    │   └────┬────────┬───┘
                            │ but not forever. │        │        │
                            └──────────────────┘   TOOL │        │ FINAL
                                       ▲               │        │ ANSWER
                                       │               ▼        ▼
                                       │   ┌────────────────┐  ┌──────────────┐
                                       │   │ ⑤ GUARD        │  │ ⑦ CHECK THE  │
                                       │   │  Does the tool │  │   ANSWER IS  │
                                       │   │  exist?        │  │   GROUNDED   │
                                       │   │  Is it still   │  │   Does every │
                                       │   │  available?    │  │   claim cite │
                                       │   │  Are the args  │  │   evidence we│
                                       │   │  valid?        │  │   really     │
                                       │   │  Budget left?  │  │   collected? │
                                       │   └───┬────────┬───┘  └───┬──────┬───┘
                                       │   No  │        │ Yes   No │      │ Yes
                                       └───────┘        ▼       ───┘      ▼
                                                 ┌────────────┐      ┌─────────┐
                                                 │ ⑥ RUN THE  │      │ DONE.   │
                                                 │   TOOL     │      │ Produce │
                                                 │   (bounded │      │ the     │
                                                 │   in time) │      │ result. │
                                                 └─────┬──────┘      └─────────┘
                                                       ▼
                                          ┌────────────────────────┐
                                          │ result, or a failure   │
                                          └───┬────────────────┬───┘
                                              │                │
                     ┌────────────────────────▼──┐   ┌─────────▼──────────────┐
                     │ ⑧ RECORD IT                │   │ ⑨ ADD IT TO WHAT THE  │
                     │   Write the full truth     │   │   MODEL KNOWS          │
                     │   into the authoritative,  │   │   (a short summary,    │
                     │   ordered record.          │   │    not the raw data)   │
                     └────────────────────────────┘   └─────────┬──────────────┘
                                                                 │
                                                                 └──▶ back to ①
```

## 3.1 What each numbered box is for

**① May the run continue?** — Before we spend anything, we ask whether we are allowed to. This is checked *before* talking to the model, so that when a limit is reached the run stops without one more expensive call. It is the first thing inside the loop for exactly that reason.

**② Build the model's view.** — The model does not see the system's internal state. We construct a deliberate, bounded picture for it: the objective, a condensed history of what has been learned, the list of tools currently available, and how much budget remains. Section 6 explains at length why this is a constructed view rather than the real state.

**③ Ask the model.** — The only point in the entire system where judgement happens. Everything before it is preparation; everything after it is verification and execution.

**④ Is the answer well-formed?** — The model's reply is text arriving from outside the program. Before anything acts on it, we check that it is structurally what we asked for. If not, we tell the model what was wrong and let it try again — a bounded number of times, because a model that cannot produce valid output will not become able to on the fiftieth attempt.

**⑤ Guard.** — Four separate questions, each of which can independently block a tool call: does this tool exist, is it still available, are the arguments valid, and is there budget left. Section 7 walks through each.

**⑥ Run the tool.** — The only point where the system reaches outside itself. Bounded in time, so a hanging tool cannot freeze the run.

**⑦ Check the answer is grounded.** — When the model says it is finished, we verify that every factual claim points at evidence we actually collected. A claim citing something that does not exist is rejected.

**⑧ Record it.** — Everything that happened goes into the run's authoritative record: ordered, append-only, and never edited once written. This is what "observable" means. (The record lives with the run and can be exported to a file; shipping it to a durable store is a production step, not something the prototype does.)

**⑨ Add it to what the model knows.** — A *short summary* of the result goes back into the model's view. Not the raw data. Section 6.3 explains why this distinction matters more than it first appears.

Then back to ①, and around again, until the model concludes or the runtime stops it.

---

# 4. One complete investigation, narrated

This is the most important section in the document. Everything above is structure; this is the system actually running.

---

> ### ⚠ A convention you must understand before reading this section
>
> Each step below is presented in two clearly separated parts:
>
> **Model's observable decision** — what the model actually emitted. This is real. It is a structured action request, it is recorded in the trace, and it is exactly what the runtime acts on.
>
> **Why this decision makes sense** — *illustrative decision rationale.* This is a human-readable explanation of why a competent agent would plausibly choose this action given the information available at that moment. It is written by me, for you, to make the run comprehensible.
>
> **It is not a captured chain-of-thought, and it is not in the system's trace.**
>
> This distinction is not pedantry — it is a design property. The system deliberately records **operational events** (what was decided, called, returned, failed) plus an optional one-line statement of intent capped short enough that it cannot become a monologue. It never requests, parses, stores, or renders the model's internal reasoning. The brief forbids it, and it is the right operational call anyway: reasoning text is long, unstable between runs, and makes records harder to compare.
>
> So when you read *"why this decision makes sense"*, read it as **a reconstruction of the decision's logic from the outside** — the kind of explanation an engineer would write in a post-incident review — not as something the system knows or could show you.
>
> If you describe this system to someone else, be precise about it: **we know what the agent did and what it had available when it did it. We do not know why it did it, and we deliberately chose not to find out.**

---

**The objective:** *"Why did checkout-api error rates spike around 14:00 UTC on 2026-09-15?"*

**The four available tools:**

| Tool | What it can tell you |
| --- | --- |
| `get_service_status` | Deployment history and current health of a service |
| `search_logs` | Individual log lines, filtered by service, time window, and text |
| `get_metrics` | Numeric time series — error rates, latency percentiles |
| `search_kb` | A small runbook wiki of known issues and past incidents |

As you read, keep asking the question the whole architecture is organised around: **why does the agent do something different after each new fact?**

---

## Step 0 — What the agent knows before it starts

Almost nothing. It has the objective sentence, and it has four short descriptions of what each tool can do. It has **zero facts about the world.** It does not know whether the spike is real, what was deployed, what the errors say, or whether anyone has seen this before.

This is worth pausing on. The agent is not starting with a hypothesis it needs to confirm. It is starting from ignorance, and its first decision is not *"how do I prove the deploy caused this"* — it is *"what is the cheapest thing I can learn that will tell me where to look next?"*

---

## Step 1 — First decision: establish what changed

```
   MODEL'S OBSERVABLE DECISION
   → call get_service_status { service: "checkout-api" }

   WHAT IT HAD AVAILABLE WHEN IT DECIDED
   → the objective sentence
   → four tool descriptions
   → no facts

   WHY THIS DECISION MAKES SENSE  (illustrative rationale — not a trace record)
   → It needs deployment and onset information before it can choose a
     more specific investigation path. Service status is the broadest,
     cheapest orienting question available, and deploys are the most
     common cause of sudden change.
```

**Why this one first, and not the logs?** Because you cannot search logs usefully yet. A log search needs a time window and a search term, and the agent has neither. It knows "around 14:00" from the objective, which is vague, and it has no idea what text to search for. Starting with logs would mean guessing.

Service status, by contrast, needs only the service name — which the objective supplies. It is the question with the lowest information requirement, which makes it the right first move.

```
   The runtime checks: tool exists ✓  available ✓  arguments valid ✓  budget ✓
                  ↓
   Tool executes against the deployment records
                  ↓
   RESULT →  deploy v2.14.0 at 13:58 UTC
             health: degraded since 14:01 UTC

   Recorded in the run's authoritative trace and assigned evidence ID E1.
```

### What changed in the agent's knowledge

Before: a vague claim about a time.
After: **a candidate trigger** (a deploy) and **a precise onset** (14:01 UTC).

Notice the three-minute gap between the deploy and the degradation. That is suggestive — but suggestive is not proof. Deploys happen many times a day and most of them break nothing. And critically, **the agent still has not verified that error rates actually rose.** It has only been told that health is "degraded," which is a coarse signal.

---

## Step 2 — Second decision: verify the premise, quantitatively

```
   MODEL'S OBSERVABLE DECISION
   → call get_metrics {
       service: "checkout-api",
       metrics: ["error_rate", "p99_latency_ms"],
       from: "13:30Z", to: "14:30Z"
     }

   WHAT IT HAD AVAILABLE WHEN IT DECIDED
   → the objective
   → E1: deploy at 13:58, degraded since 14:01
   → three remaining tools

   WHY THIS DECISION MAKES SENSE  (illustrative rationale — not a trace record)
   → Two weaknesses remain. Nothing has yet shown that errors actually
     increased — the objective asserted it and "degraded" is a coarse
     signal, but no numbers exist. And a deploy three minutes before
     degradation could be coincidence. Metrics address both at once.
```

**Note what just happened to the arguments.** The time window `13:30–14:30` was *chosen by the agent*, bracketing the onset it learned in step 1. At step 0 it could not have produced that window. This is the first concrete demonstration that a fixed pipeline could not do this job.

**Why verify a premise the user already asserted?** Because an agent that builds a causal story on an unchecked assumption is doing bad work. If metrics showed no spike, the correct answer would be *"the alert appears to be wrong"* — a completely different conclusion. Good investigation confirms the phenomenon before explaining it.

```
   RESULT →  error_rate:      0.2%  →  8.4%   (step change at 14:01)
             p99_latency_ms:  180   →  2400   (same moment)

   Recorded as evidence item E2.
```

### What changed in the agent's knowledge

Three things, and the third is the interesting one:

1. **The premise is confirmed and quantified.** Errors rose forty-fold. This is real.
2. **The onset is now independently corroborated.** Status said degraded at 14:01; metrics show the step change at 14:01. Two separate sources agree, which is much stronger than either alone.
3. **There is a clue in the latency.** The p99 latency jumped thirteen-fold *at the same moment* as the errors. That pattern is informative. A straightforward code bug usually fails *fast* — it throws immediately and latency stays flat or drops. Errors and latency rising together suggests something is **waiting** — queueing for a resource, or blocked on something downstream.

The agent now has a shape for the failure, but not its identity.

---

## Step 3 — Third decision: find the failure mechanism

```
   MODEL'S OBSERVABLE DECISION
   → call search_logs {
       service: "checkout-api",
       from: "13:50Z", to: "14:20Z",
       query: "error"
     }

   WHAT IT HAD AVAILABLE WHEN IT DECIDED
   → E1: deploy at 13:58, degraded 14:01
   → E2: error rate 0.2% → 8.4%, p99 180ms → 2400ms at 14:01

   WHY THIS DECISION MAKES SENSE  (illustrative rationale — not a trace record)
   → It now knows THAT the service broke, WHEN, and HOW BADLY — but not
     HOW. Metrics are aggregates: they count failures, they never
     describe them. Explaining the mechanism requires the actual failure
     text. And the window to search is now known, which it was not before.
```

**Why not go straight to the knowledge base?** Because the agent does not yet know what to look up. The knowledge base is searched by symptom, and "errors went up" is too generic to match anything useful. It needs the specific failure signature first.

```
   RESULT →  47 matching entries
             41 of them contain:
             "payment-gateway: connection pool exhausted"

   Recorded as evidence item E3.
```

### What changed in the agent's knowledge

Now it has the **mechanism**, and the mechanism *explains the earlier clue*:

- Connection pool exhausted → requests wait for a free connection → **latency climbs**.
- Waits eventually time out → **errors climb**.
- Both happen at once → which is precisely the pattern E2 showed.

The story is now internally coherent. Three pieces of evidence that were collected independently all point the same way. That coherence is itself meaningful — the agent is no longer looking at three unrelated observations.

But one thing is still missing, and it is the most important thing.

---

## Step 4 — Fourth decision: find the causal link

```
   MODEL'S OBSERVABLE DECISION
   → call search_kb { query: "connection pool exhausted deploy" }

   WHAT IT HAD AVAILABLE WHEN IT DECIDED
   → E1: a candidate trigger (the deploy)
   → E2: confirmed magnitude and onset
   → E3: a failure mechanism (pool exhaustion)

   WHY THIS DECISION MAKES SENSE  (illustrative rationale — not a trace record)
   → It has a trigger and a mechanism but no LINK between them. Why would
     deploying new code exhaust a connection pool? Without that link, the
     strongest available statement is "these two things happened near each
     other" — correlation, not explanation. The knowledge base is where
     past incidents and known failure patterns are recorded.
```

**This is the sharpest decision in the run.** The agent already has enough material to write a confident-sounding answer. A weaker agent would stop here and say *"the deploy caused pool exhaustion"* — which sounds authoritative and is, at this point, an unsupported leap.

Instead it recognises a specific gap in its own reasoning and goes looking for the missing link. It is also asking a question with real decision value: **is this a novel problem or a recurring one?** The answers lead to completely different recommendations.

```
   RESULT →  KB-014: "Pool exhaustion following deploys"
             Connection pool size is configured per-replica and is not
             scaled with replica count. Deploys that increase replica
             count have previously exhausted the shared upstream pool.
             Known recurring regression.

   Recorded as evidence item E4.
```

### What changed in the agent's knowledge

The missing link is supplied, and the entire chain now connects:

```
   deploy v2.14.0  ──▶  replica count increases
                            │
                            ▼
                    per-replica pool config unchanged
                            │
                            ▼
                    aggregate pressure on the shared
                    upstream payment-gateway pool
                            │
                            ▼
                    pool exhausted  ──▶  waits  ──▶  latency ↑ and errors ↑
```

Every link in that chain is now supported by collected evidence, and the final step is corroborated by a documented prior occurrence.

---

## Step 5 — Fifth decision: stop

```
   MODEL'S OBSERVABLE DECISION
   → produce the final answer

   WHAT IT HAD AVAILABLE WHEN IT DECIDED
   → E1–E4, and budget still remaining

   WHY THIS DECISION MAKES SENSE  (illustrative rationale — not a trace record)
   → Premise confirmed. Onset established by two independent sources.
     Trigger identified. Mechanism identified. Causal link documented as
     a known pattern. Every remaining question is either answered or not
     answerable from the available sources. Further calls would add
     detail, not change the conclusion.
```

**Why does it stop here rather than using its remaining budget?** Because a good investigator stops when additional evidence stops changing the answer, not when they run out of tools. The agent had budget left. Spending it would have produced more data and the same conclusion.

This matters architecturally: **stopping is a decision, not an exhaustion.** Stopping because you ran out of steps is a different, worse outcome — and the system distinguishes the two explicitly (Section 8, Story 10).

---

## Step 6 — The answer, and the check on the answer

The model submits its answer in two structurally distinct parts. This separation is enforced by the system, not left to the model's good manners.

**Findings — statements of fact, each of which must point at evidence:**

```
   F1  checkout-api deployed v2.14.0 at 13:58 UTC and
       entered a degraded state at 14:01 UTC.                      [E1]

   F2  Error rate rose from 0.2% to 8.4% and p99 latency from
       180 ms to 2400 ms, both at 14:01 UTC.                       [E2]

   F3  41 of 47 error log entries in the window carry the
       signature "payment-gateway: connection pool exhausted".     [E3]

   F4  A documented recurring regression (KB-014) describes pool
       exhaustion after deploys that increase replica count.       [E4]
```

**Conclusion — the agent's own inference, clearly labelled as such:**

```
   The deploy is the most likely trigger. The failure signature [E3]
   matches the documented regression pattern [E4], and onset follows
   the deploy by roughly three minutes [E1], corroborated independently
   by the metric step change [E2]. Recommended immediate action is
   rollback or a pool-size increase proportional to replica count.
```

### The check

Before that answer is accepted, the runtime verifies something specific:

```
   Every citation in every finding  ─────▶  does this evidence
                                            actually exist in
                                            the record of this run?
                                                    │
                          ┌─────────────────────────┴──────────┐
                          │ Yes                                │ No
                          ▼                                    ▼
                   Answer accepted                   Answer REJECTED.
                                                     Tell the model which
                                                     ids are invalid and
                                                     which are real.
                                                     Let it correct itself,
                                                     once.
```

If the model had written *"a similar incident occurred last Tuesday [E7]"* — and no E7 was ever collected — that answer would be rejected. The agent **cannot cite evidence it did not gather.**

There is also a rule the model cannot route around: **a finding must carry at least one citation.** There is no way to express an uncited finding at all. If the model wants to say something it cannot support, it has exactly one honest option: put it in the conclusion, where it is visibly labelled as inference rather than fact.

And if the evidence genuinely is not enough, the model can say so — declaring the investigation inconclusive and explaining what is missing. That is treated as a **successful run with an honest outcome**, not a failure. The system never forces the agent to manufacture findings.

---

## 4.1 What this run teaches about the architecture

Five things, and they justify most of the design:

**The order was not predetermined.** A different model might reasonably have searched logs before metrics. The architecture does not mandate a sequence — it only guarantees that each decision is informed by everything collected so far.

**Arguments were derived from earlier results.** The log-search window came from the deploy timestamp. This is the concrete reason a pipeline cannot do this job.

**Each step closed a specific, identifiable gap.** Temporal context → quantitative confirmation → failure mechanism → causal link. The agent was not collecting data; it was answering successive questions, each raised by the previous answer.

**The agent verified the premise it was handed.** It did not assume the user was right.

**Evidence and inference never mixed.** The four findings are things tools returned. The conclusion is the agent's reading of them. A reader can accept the findings and dispute the conclusion, which is exactly what you want from an investigative tool.

---

# 5. The seven responsibilities

Now that you have seen a run, here is the system organised by *what each part is responsible for*. Conceptual names first; the blueprint's technical names appear at the end of each part, once the idea is already clear.

## 5.1 The Orchestrator — owns the investigation

**What it does.** Runs the loop. Decides when to ask the model, when to dispatch a tool, when to stop, and why. It is the only part of the system with authority over sequencing.

**What it does not do.** It does not decide *which* tool is appropriate — that is judgement, and it belongs to the model. It does not know how any tool works internally. It does not format output for humans.

**Why the authority lives here.** If more than one component could influence what happens next, you could not answer "why did the run do that?" by reading one place. Concentrating sequencing authority in a single loop is what makes the run's behaviour explainable — and explainability is the entire point of the exercise. It is also why the loop is deliberately written as one readable piece rather than spread across a network of handlers: the control flow must be *locatable*.

*In the blueprint: `agent/loop.ts`, the `run()` function.*

## 5.2 The Model — owns judgement

**What it does.** Receives a bounded picture of the investigation and answers one question: what should happen next? Either "call these tools with these arguments" or "I am ready to conclude, and here is my answer."

**What it does not do.** It never executes anything. It never touches a tool directly. It has **no authority over limits and cannot extend or bypass them**, and it has no visibility into the runtime's internal bookkeeping. It proposes; it does not act.

Be precise about the limits, because two different things are easily confused. The model *is told* how much budget remains — steps, tool calls, and time — deliberately, so it can choose to conclude early with partial evidence rather than being cut off mid-investigation. What it cannot do is change that budget or continue past it. **Knowing a boundary and having authority over it are different things**, and the model has only the first.

**Why it is behind a boundary.** Two reasons, and both matter.

The first is *testability*. If the loop called a real language model directly, no test could be deterministic — the same input could produce different output, tests would need network access and an API key, and the whole suite would be slow and flaky. With the model behind a boundary, tests substitute a scripted stand-in that returns exactly the decisions the test wants. Every failure path becomes reproducible on demand.

The second is *portability*. Swapping providers, or running the model on a different machine, touches one component and nothing else.

*In the blueprint: the `ModelClient` interface, with `ScriptedModel` and `AnthropicModel` behind it.*

## 5.3 The Tool System — owns capability

**What it does.** Each tool exposes one capability: describes what it can do, declares what shape of input it accepts, performs the operation, and returns a structured result plus a short human-readable summary of that result.

**What it does not do.** A tool has no opinion about the investigation. It does not know what question is being answered, what was called before it, or what should be called next. It cannot trigger another tool.

**Why tools are dumb on purpose.** If a tool could decide what happens next, investigation strategy would be scattered across every tool and the orchestrator would no longer be the single authority. Adding a fifth tool would then mean reasoning about how it interacts with the other four. Keeping tools inert means adding one is a purely local change.

There is a second responsibility worth separating out. Something has to decide whether a *proposed* tool call is acceptable — does the tool exist, is it available, are the arguments the right shape, did it return what it promised. That checking does not belong inside the tool (a tool cannot be trusted to validate itself) and it does not belong in the loop (which would bloat with per-tool detail). It is its own boundary, sitting between them.

*In the blueprint: the `Tool` contract, `ToolRegistry` as a catalogue, and `executeToolCall()` as the checking boundary.*

## 5.4 State — owns memory

**What it does.** Remembers the investigation between model calls.

**Why this is necessary at all.** Each model call is independent. The model has no memory of previous calls — if you asked it the same question twice with nothing in between, it would have no idea it had answered before. Continuity is not a property of the model; it is something the system must supply.

So after every step, the system must remember:

```
   the original objective                    (never changes)
   what tools were called, with what args    (so it doesn't repeat itself)
   what each one returned, in brief          (the accumulated knowledge)
   what failed and how                       (so it can route around problems)
   which tools have been withdrawn           (so it stops offering broken ones)
   how much budget has been consumed         (so limits can be enforced)
   how many correction attempts have occurred (so corrections stay bounded)
```

Look at the last three lines. Those are **not for the model** — they are the runtime's private bookkeeping. This is the origin of one of the most important boundaries in the system, and Section 6.2 is entirely about it.

*In the blueprint: `RunState`, held by the orchestrator.*

## 5.5 Control and Policy — owns boundaries

**What it does.** Enforces everything the model is not permitted to override:

- How many times the model may be consulted.
- How many tool dispatches may be attempted.
- How long the whole run may take.
- How long any single operation may take.
- What happens after a failure, and when a failing tool stops being offered.
- How many times a malformed response may be corrected before the run gives up.

**Why the model cannot own this.** Because the limits exist *precisely for the case where the model is misbehaving.* A limit the model could extend is not a limit. This is the same reason a process does not enforce its own memory quota.

**The most important structural detail in the entire system:** the "may we continue?" check happens ***before*** each expensive operation, never after. Before asking the model. Before dispatching each tool. The ordering is what makes the guarantee real — when the limit is reached, the run stops having made *zero* further calls, not one more.

*In the blueprint: `Budget` and its `check()`, plus the failure policy.*

## 5.6 Trace — owns the record

**What it does.** Records everything that happened, in order: what the model was asked, what it decided, what tools were called with what arguments, what came back, what failed, what limits fired, and how the run ended.

**Be precise about what "authoritative" means here.** The properties that matter are that the record is **ordered, append-only, and immutable once written** — it is the single source of truth about the run, and nothing can rewrite history. What it is *not*, in this prototype, is durably persisted: it lives with the run and can be exported to a file. Shipping it to a log store or database is a production evolution. Saying "permanent" would overclaim; the architectural property being relied on is authority and immutability, not durability.

**Why this is different from state.** This is a distinction people miss, and it is worth being precise about:

```
   STATE                              TRACE
   ─────                              ─────
   What the agent needs in order      What an observer needs in order
   to continue working.               to understand what happened.

   Condensed — summaries only.        Complete — full payloads.

   Overwritten and updated as         Append-only. Never edited,
   the run proceeds.                  never reordered, never deleted.

   Exists to serve the next           Exists to serve a human reading
   decision.                          it afterwards.

   Discarded when the run ends.       Is the output — exportable,
                                      and the thing a human reads.
```

They hold overlapping information for entirely different consumers. State is a working set; trace is history. Merging them would mean either polluting the model's context with audit detail, or losing audit detail to keep the context small.

**Why all recording goes through one place.** Four properties have to hold for every recorded event: correct ordering, immutability, redaction of anything secret-shaped, and bounded size. If events could be written from several places, each one would have to implement all four correctly — and the first place that forgot would be a silent hole. One doorway means those properties are enforced once and cannot be bypassed.

*In the blueprint: `TraceRecorder`.*

## 5.7 Finalization — owns final assembly and grounding validation

**What it does.** Assembles the collected evidence and the model's answer into the final result. Specifically: it derives the evidence list from the record, checks every citation against it, keeps what tools found structurally separate from what the agent inferred, and reports what is missing.

**What it does not do — and this is why it is not called "owns the verdict".** Finalization does not decide the conclusion. It has no opinion about what the evidence means. The verdict is the model's; finalization only checks that the verdict is *accountable to the record* and packages it. Naming it otherwise would quietly break the boundary the rest of this document establishes: judgement is the model's, authority is the runtime's, and a runtime component that decided conclusions would be doing both.

**The one absolute rule.** Finalization **never calls the model and never dispatches a tool.** It is pure assembly over things that already happened.

**Why that rule matters so much.** Consider a run stopped because it hit its step limit. It has partial evidence and no conclusion. The tempting move is to ask the model for one last summary of what was found.

That would violate the promise the system makes: when the limit is reached, **nothing further happens.** "Nothing further except one more model call" is not a limit. So the partial result is assembled mechanically — the evidence is listed, the reason for stopping is stated, and no judgement is invoked. The guarantee is kept because finalization is structurally incapable of breaking it.

*In the blueprint: `buildEvidence()`, `validateFinal()`, and `finalize()`.*

---

# 6. Data flow is not control flow

These are two different questions about the same system and it is worth separating them deliberately.

**Control flow** asks: *who decides what happens next?* That shape is a loop — ask, check, act, repeat.

**Data flow** asks: *what information moves where, and in what form?* That shape is not a loop. It is a fan-out with an asymmetry at its centre.

## 6.1 The path information takes

```
   OBJECTIVE
   (the user's question, fixed for the whole run)
       │
       ▼
   ┌─────────────────────────────────────────────────────┐
   │  RUN STATE — everything the system remembers         │
   │  objective · history · failures · counters · budget  │
   └────────────────────────┬────────────────────────────┘
                            │
                            │  a deliberately reduced view
                            │  is constructed for the model
                            ▼
   ┌─────────────────────────────────────────────────────┐
   │  MODEL'S VIEW                                        │
   │  objective · condensed history · available tools ·   │
   │  budget remaining                                    │
   │                                                      │
   │  NOT included: failure counters, retry counts,       │
   │  internal identifiers, raw tool payloads             │
   └────────────────────────┬────────────────────────────┘
                            ▼
                    MODEL'S DECISION
                    (untrusted until checked)
                            │
                            ▼
                    checked, then permitted
                            │
                            ▼
                      TOOL RESULT
                            │
              ┌─────────────┴──────────────┐
              │                            │
              ▼                            ▼
   ┌────────────────────┐      ┌──────────────────────────┐
   │  THE RECORD        │      │  THE MODEL'S VIEW        │
   │                    │      │                          │
   │  the FULL result   │      │  a SHORT SUMMARY of it   │
   │                    │      │                          │
   │  for a human       │      │  for the next decision   │
   │  auditing the run  │      │                          │
   └─────────┬──────────┘      └─────────────┬────────────┘
             │                                │
             │                                ▼
             │                        next decision
             │
             ▼
   ┌────────────────────────────────────────────────────┐
   │  EVIDENCE — derived from the record at the end      │
   │  (not a separate store; a view over what happened)  │
   └────────────────────────────────────────────────────┘
```

## 6.2 Why the model sees a constructed view rather than the real state

Four reasons, and they are all load-bearing.

**Some of the state is nobody's business but the runtime's.** The count of how many times a tool has failed, how many correction attempts have occurred, how many model retries are left — these are policy internals. Showing them to the model invites it to reason about the harness rather than the incident. Worse, an agent that can see its own correction budget is an agent that can start optimising against it.

**Context is expensive and finite.** Every previous result is re-sent with every subsequent request. If raw payloads accumulated in that view, the cost of step five would be dominated by data collected at step one.

**A constructed view can be tested.** Because building it is a self-contained transformation from state to view, you can test exactly what the model would see in any situation — including "does a withdrawn tool disappear from the list?" That question has a mechanical answer, which is worth a great deal.

**It is already the right shape for sending elsewhere.** If this agent ever ran on a remote machine, the view is what would cross the wire. It was designed as a boundary, so it is already one.

## 6.3 Why the result splits in two — and why the halves differ

This is the asymmetry mentioned above, and it is the most consequential data-flow decision in the system.

```
   A tool returns 47 log entries.

   TO THE RECORD  ──▶  all 47 entries, complete

                       Because an auditor may need to see exactly what
                       the agent saw. Completeness is the whole value.

   TO THE MODEL   ──▶  "47 matches; 41 contain 'payment-gateway:
                        connection pool exhausted'"

                       Because the model needs to know what the logs
                       MEANT, not to re-read them. One line carries
                       the decision-relevant content.
```

If the full 47 entries went into the model's view, they would be re-sent on every subsequent step, and a tool returning ten thousand entries would make the run unaffordable. The summary makes the model's context grow with the *number of steps taken*, not with *the size of the data examined* — which is the difference between a system that scales and one that does not.

Each tool writes its own summary, because only the tool knows what matters about its own output. And because a tool's promise is not a guarantee, the runtime enforces a maximum length mechanically rather than trusting the tool to be brief.

## 6.4 Why evidence is derived from the record rather than kept separately

The obvious design is to maintain an evidence list as you go: each time a tool succeeds, append to it.

That creates two stores holding overlapping information that must agree. The record says a tool returned something; the evidence list says the same thing. Two sources of truth about one fact.

**Any two stores that must agree will eventually disagree**, and when they do, one of them is a bug — and you will not know which. Every future change has to remember to update both.

Instead: the record is already authoritative, ordered, and append-only. Evidence is simply *the successful tool results in that record, read back*. It is a view, not a store. There is nothing to keep in sync, nothing to drift, and no way for the evidence to claim something the record does not contain.

This is also what makes the grounding check meaningful. When the model cites evidence item three, the system is not consulting a list it maintained — it is checking against **what actually happened.**

---

# 7. At every step: what can happen?

The run is a sequence of decision points. At each one, several things can happen and the system has a defined response to all of them. This section walks through every branch and says *why the decision lives where it does*.

## 7.1 Before asking the model

```
                Are we allowed to continue?
                          │
        ┌─────────────────┴─────────────────┐
        │                                   │
    No — a limit                        Yes — budget
    is reached                          remains
        │                                   │
        ▼                                   ▼
   Stop the run.                      Build the model's
   Record which limit.                view and ask it.
   Assemble the evidence
   collected so far.
   Never call the model.
```

**Why this check is the very first thing in the loop.** If it came after the model call, "limit reached" would mean *one more call already happened*. Placing it first is the difference between a limit and a suggestion. It is also why the check is repeated later before each tool dispatch — an expensive operation should never occur on the far side of an exhausted budget.

**Which limits are checked.** Number of model consultations, number of tool dispatches, and total elapsed time. They bound genuinely different costs, which is why they are separate rather than one number.

## 7.2 After the model responds

```
                  Is the response well-formed?
                          │
        ┌─────────────────┴──────────────────┐
        │ No                                 │ Yes
        ▼                                    ▼
   Explain what was wrong.          What did it ask for?
   Let it try again.                        │
   Count the attempt —              ┌───────┴────────┐
   after two, give up and           │                │
   end the run honestly.       Tool calls      Final answer
        │                           │                │
        └──▶ back to the top        ▼                ▼
                              go to §7.3       go to §7.5
```

**Why correction rather than immediate failure.** A single malformed response is often recoverable — the model can see what was wrong and fix it. Ending the run on the first mistake would be needlessly brittle.

**Why the corrections are counted.** Because a model that *cannot* produce valid output will not become able to on the fiftieth attempt. Unbounded correction is an infinite loop wearing a helpful expression.

## 7.3 Before dispatching tools

Four independent questions, in this order. Each can block on its own.

```
   ① Do all the named tools exist?
      Checked for the WHOLE batch, before ANY of them runs.
          │
          ├─ No  ──▶ Reject the entire decision. Execute nothing.
          │           Tell the model which names were wrong and
          │           list what is actually available.
          │           Count it as a malformed decision.
          │
          └─ Yes ──▶ continue

   ② Is there budget left?          [checked before EVERY individual call]
          │
          ├─ No  ──▶ Stop the run. This tool is never dispatched.
          │
          └─ Yes ──▶ continue

   ③ Is this tool still available?
          │
          ├─ No  ──▶ Reject this one call. Tell the model it was
          │           withdrawn. The other calls proceed.
          │
          └─ Yes ──▶ continue

   ④ Are the arguments the right shape?
          │
          ├─ No  ──▶ Reject this one call. Tell the model exactly
          │           which field was wrong and why.
          │           The other calls proceed.
          │
          └─ Yes ──▶ EXECUTE
```

**Why check ① covers the whole batch but ④ checks one call at a time.** This asymmetry is deliberate and it is a genuine trade-off.

A nonexistent tool name means the model has misunderstood what is available — a problem with the *decision as a whole*. If we executed the valid calls first and only then noticed the bad name, we would have caused side effects on the strength of a decision we were about to reject. So the whole batch is rejected before anything runs.

A bad *argument*, by contrast, is a local mistake in one call. Rejecting the entire batch for it would throw away two perfectly good calls because of one typo — worse behaviour, not better.

The cost of this choice is worth naming: **a decision is not an all-or-nothing unit.** Name checking is the only all-or-nothing gate. If a batch has three valid calls and one with bad arguments, you get three results and one error — not zero results.

## 7.4 When a tool runs

```
              Tool executes, bounded in time
                          │
        ┌─────────────────┼──────────────────┐
        │                 │                  │
   Returns          Throws an            Never finishes
   a result         error                (hangs)
        │                 │                  │
        ▼                 ▼                  ▼
   Is it the        Record the         Stop waiting
   promised         failure.           after the time
   shape?           Continue.          limit. Continue.
        │
   ┌────┴────┐
   │ No      │ Yes
   ▼         ▼
 Treat as   Summarise it, record the full result,
 a broken   add the summary to what the model knows,
 tool       and assign it an evidence identifier.
```

**Why the returned value is checked even though the tool is our own code.** Because a tool is code, and code has bugs. If a tool returns something misshapen, that corruption would flow into the record, into the evidence, and into the model's view. Checking it at the boundary means a broken tool produces a *clean, visible failure* instead of quiet contamination.

**Why a hanging tool is handled differently from a slow one.** A slow tool eventually returns. A hanging tool never does, and no amount of waiting fixes it. The system stops waiting after a bounded time and carries on.

It is worth being honest about what that does and does not mean. The system stops *waiting*; it cannot stop the tool. Abandoned work may still be running. This is safe here because every tool is a read against fixture data with no side effects — and it is called out explicitly rather than glossed over, because on real tools with real side effects it would need a process boundary to be genuinely safe.

## 7.5 When the model says it is finished

```
              Does every citation point at real evidence?
                          │
        ┌─────────────────┴─────────────────┐
        │ No                                │ Yes
        ▼                                   ▼
   Reject the answer.              Accept it.
   Name the invalid ids            Assemble the final result:
   and list the real ones.         findings, conclusion,
   Let it correct itself —         evidence, and gaps.
   once.
        │
        └──▶ if it fails again, end the run
             honestly rather than accept an
             ungrounded answer
```

**Why only one correction here, when malformed decisions get two.** A malformed decision is usually a formatting slip. An invalid citation is a *grounding* problem — the model is asserting something it cannot support. One chance to correct is generous; more would be arguing with it about reality.

---

# 8. When things go wrong: eleven stories

Failure handling is where this system earns its keep, so it deserves to be told as scenarios rather than as a list of categories. Each story follows the same shape:

```
   What happened  →  Who owns the problem  →  What the runtime does
                  →  What the model sees   →  How the run continues
```

The question **"who owns the problem?"** is the key to the whole section. Once you know whose fault something is, the correct response is usually obvious — and much of this design is the consequence of answering it carefully.

---

## Story 1 — The model sends bad arguments

**What happened.** The model asked to search logs but supplied a start time in a format the tool does not accept.

**Who owns the problem.** The **model**. The tool is perfectly healthy — it never even ran.

**What the runtime does.** Rejects the call before dispatch. Records the rejection. Critically: **it does not count this against the tool.**

**What the model sees.** The specific field that was wrong and why: *"`from` must be an ISO-8601 timestamp."* Not a generic "invalid arguments" — the actual detail it needs to fix the mistake.

**How the run continues.** The model tries again, usually correctly. Nothing is withdrawn.

**Why this distinction matters enormously.** An earlier draft of this design counted *every* tool-call failure against the tool, including this one. That meant a model sending bad parameters twice would cause a perfectly healthy tool to be removed from the investigation. The tool was blamed for the model's mistake. Getting "who owns the problem" right is not pedantry — it directly changes whether the system behaves sensibly.

---

## Story 2 — A tool crashes once

**What happened.** `get_metrics` threw an error.

**Who owns the problem.** The **tool**, or whatever it depends on.

**What the runtime does.** Records the failure. Notes that this tool has now failed once. **Leaves it available.**

**What the model sees.** That the call failed, with the error message.

**How the run continues.** The model can retry it, or route around it, or proceed with what it has.

**Why not disable it immediately?** Because one failure is weak evidence. It might be a transient blip, or a specific query the tool could not handle while other queries work fine. Withdrawing a tool on one error would make the system fragile in exactly the situation where you want it robust — and it would throw away a capability the investigation may still need.

---

## Story 3 — A tool fails twice in a row

**What happened.** `get_metrics` failed again, immediately after the first failure.

**Who owns the problem.** The **tool**. Two consecutive failures is meaningfully different from one.

**What the runtime does.** Withdraws the tool for the remainder of this run. From the next step onward, **it is removed from the list of tools the model is shown.**

**What the model sees.** A note saying the tool is unavailable for the rest of the run, and — more importantly — it simply *stops appearing in the available tools*. Its option set shrinks.

**How the run continues.** The agent works with what remains and states the resulting gap in its answer: *"metrics could not be retrieved; the error-rate magnitude is unconfirmed."*

**Why remove it from the list rather than just rejecting calls to it?** Because shrinking the option set is more effective than punishing a choice. If the tool is not offered, the model will not ask for it, and the failure mode disappears rather than repeating. Rejecting calls would let the model keep reaching for a dead tool and burning budget on rejections.

**Two things the wording is careful about.** The tool is described as *unavailable for this run* — never as *broken*. Two consecutive failures might have unrelated causes, and the system is not qualified to diagnose the tool's health. This is **budget protection, not a medical opinion.**

And the counter **resets on success.** A tool that fails, succeeds, then fails again has not failed twice *consecutively* — it is flaky, not dead, and it stays available.

**What survives withdrawal.** Evidence the tool already produced remains completely valid and citable. Availability is a statement about *future* calls. A result collected at step two does not become false because the tool broke at step five.

---

## Story 4 — A tool returns something misshapen

**What happened.** The tool ran without error but returned a structure that does not match what it promised to return.

**Who owns the problem.** The **tool's code**. This is a defect, not a transient failure.

**What the runtime does.** Rejects the result — it never becomes evidence — and withdraws the tool **immediately**, on the first occurrence.

**What the model sees.** A note that the tool is unavailable.

**How the run continues.** Degraded, same as Story 3.

**Why immediate, when a crash gets two chances?** Because the two failures are fundamentally different in nature. A crash *might* be transient. A tool that returns the wrong shape has a bug in its code, and running it again will produce the identical wrong shape. Retrying a deterministic defect is pure waste. **The number of retries should match how likely a retry is to help** — and here it is zero.

---

## Story 5 — A tool hangs forever

**What happened.** The tool started and never finished.

**Who owns the problem.** The **tool**.

**What the runtime does.** Stops waiting after a bounded time and treats it as a failure.

**What the model sees.** A timeout.

**How the run continues.** Normally. Two consecutive timeouts withdraw the tool, same as crashes.

**What the system promises, and what it does not.** It promises that *the run* is not stuck. It does **not** promise that the tool stopped — abandoned work may still be running in the background. Truly stopping it would require running tools in a separate process. That is the right answer for real tools with real side effects, and it is deliberately out of scope here because every tool is a harmless read against fixture data.

This is stated rather than hidden because a reviewer will ask, and "we bounded the harness, not the tool" is a much better answer than an overclaim.

---

## Story 6 — The run's total time expires while a tool is mid-flight

**What happened.** The run has a sixty-second overall limit. At 58.9 seconds a tool was dispatched. At sixty seconds the overall clock expired while that tool was still working.

**Who owns the problem.** **Nobody — and specifically not the tool.** The tool was working normally. The *harness* ran out of time.

**What the runtime does.** This is the subtle one, and it is the reason this story exists as its own entry.

The tool is aborted, which looks exactly like a timeout. The naive implementation records it as a timeout, and the failure policy — seeing a timeout — counts a strike against the tool. Do that twice and **the harness's own clock withdraws a perfectly healthy tool.**

So the system distinguishes the two. A tool aborted because *the run* ended is marked as such and **takes no strike**. The run then stops at the next check with the reason "out of time" — not "tool failure."

**What the model sees.** Nothing. The run is over.

**How the run continues.** It does not. It ends with the correct reason recorded, and the tool's record stays clean.

**Why this deserved its own story.** It is the same class of mistake as Story 1 — blaming a tool for something that was not its fault — arriving from a completely different direction. It was caught only on the third review pass, and it is a good example of how the "who owns the problem" question keeps earning its keep.

---

## Story 7 — The model asks for a tool that does not exist

**What happened.** The model proposed calling `search_twitter`. There is no such tool.

**Who owns the problem.** The **model**. It was given the list of available tools and asked for something not on it.

**What the runtime does.** Treats it as a malformed decision rather than a tool failure — because no tool was involved. The **entire batch is rejected** and nothing executes, even if other calls in it were valid.

**What the model sees.** Which names were wrong, and the list of what actually exists.

**How the run continues.** The model corrects itself. If this happens repeatedly, the run ends — a model that keeps inventing tools after being shown the real list is not going to converge.

**Why reject the whole batch here, when bad arguments only reject one call?** Because inventing a tool name suggests the model has misunderstood what is available — a problem with the decision as a whole, not with one parameter. And rejecting *before* anything runs means no side effects occur on the strength of a decision we are about to throw away.

**Why count it once per decision, not once per bad name?** Because a decision naming four nonexistent tools is *one* misunderstanding, not four. Counting per name would exhaust the correction budget from a single mistake.

---

## Story 8 — The model returns something unparseable

**What happened.** Instead of a structured decision, the model returned prose, or malformed output, or a shape that does not match what was asked for.

**Who owns the problem.** The **model**.

**What the runtime does.** Rejects it and describes exactly what was structurally wrong.

**What the model sees.** The specific structural complaint.

**How the run continues.** One or two corrections. Then the run ends honestly, reporting that the model could not produce a usable decision — which is a far better outcome than crashing, and far more diagnosable.

---

## Story 9 — The model cites evidence that was never collected

**What happened.** The final answer contains a finding citing evidence item seven. Only four items were ever collected.

**Who owns the problem.** The **model**. This is the failure mode the grounding check exists for.

**What the runtime does.** Rejects the answer outright. The run does not complete on an ungrounded result.

**What the model sees.** Which identifiers are invalid, and which ones actually exist.

**How the run continues.** One correction. If it fails again, the run ends without a final answer — the evidence is still reported, and the reason for stopping is recorded.

**Why this is worth building at all.** Without it, "grounded in collected evidence" is a *hope* expressed in a prompt. With it, it is a **checked property**. The agent cannot cite something it did not gather, because the system verifies every reference against what actually happened.

**What it does not prove.** That the cited evidence *supports* the claim attached to it. The system checks that E2 exists, not that E2 means what the finding says it means. Verifying that would need a second model pass or a human, and neither is in scope. This limit is stated plainly rather than papered over — claiming more would be exactly the kind of overreach the whole design is organised against.

---

## Story 10 — The model never stops

**What happened.** Step after step, the model keeps requesting tools and never concludes. Perhaps it is stuck in a pattern; perhaps the objective is unanswerable.

**Who owns the problem.** Nobody needs to own it. **This is the case the limits exist for.**

**What the runtime does.** At the top of each iteration, before consulting the model, it checks whether the budget is exhausted. When it is, the run stops — and stops *there*, having made no further call of any kind.

**What the model sees.** Nothing. It is not consulted again.

**How the run continues.** It does not. The result reports: the run stopped because it reached its step limit; here is the evidence collected; there is no conclusion.

**The detail that makes this real.** The partial result is assembled **mechanically** — the evidence is enumerated and the stopping reason is stated. No model call is made to summarise. Asking the model for one last summary would be very tempting and would break the guarantee, because "no further calls, except one" is not a guarantee at all.

This is why finalization is structurally incapable of calling the model: the rule is enforced by what the component *can* do, not by remembering not to.

---

## Story 11 — The model service itself fails

**What happened.** The request to the model provider failed — rate limited, network dropped, service error.

**Who owns the problem.** The **provider**, or the network.

**What the runtime does.** Records the failure, and the response depends on how the failure was classified — the classification is made by the provider-facing component, which is the only part that understands provider-specific errors.

```
   Was this failure classified as retryable?
      (rate limit, transient network error, service error)
              │
      ┌───────┴────────┐
      │ No             │ Yes
      ▼                ▼
   End the run      Is the run's single retry still unused?
   immediately         │
   with "model     ┌───┴────┐
   error" as       │ No     │ Yes
   the reason.     ▼        ▼
                End the   Retry once.
                run.
```

A non-retryable failure — a malformed provider response, a refusal, a request the provider rejects outright — ends the run straight away. Retrying it would be waiting for a different answer to the same question.

**What the model sees.** On a retry, the designed behaviour is that a short note is added to the model's view stating that the previous call failed and is being retried. This is genuinely part of the design rather than an assumption — the note enters the same model-visible history that tool results and corrections use.

**How the run continues.** Either the retry succeeds and the investigation proceeds, or the run ends with "model error" as the reason, still producing a complete result containing whatever evidence was collected.

**Two deliberate details.** The retry is a **whole-run budget** — one retry per run, not one per call, and it is never reset by a subsequent success. A per-call allowance would silently multiply into many retries across many steps, and a bounded escape hatch would quietly become a retry subsystem, which is not what this is.

And a failed call **does not consume a step**, because the model never got to make a decision. Charging it a step would punish the investigation for the provider's problem.

---

## 8.1 The pattern across all eleven

Read them together and one rule emerges:

> **A tool is penalised only for its own misbehaviour.**

Bad arguments are the model's fault. A run-deadline abort is the harness's fault. Neither counts against the tool. Only genuine tool failures — crashes, hangs, misshapen output — affect a tool's standing.

And a second, broader rule:

> **Every failure path leads somewhere defined.** Never a crash, never an infinite loop, never a silently wrong answer. Either the investigation continues in a degraded state with the gap recorded, or the run ends with a reason and whatever evidence exists.

The honest version of that promise, stated precisely: **expected** failures become structured outcomes. If the harness itself has a genuine bug, it surfaces as a crash with a stack trace — deliberately, because disguising a real defect as a plausible degraded run would destroy the observability the system exists to provide.

---

# 9. Why does this feature exist, and where does it belong?

Every mechanism below is introduced the same way: the problem that forced it, what has to be true to solve it, where that responsibility belongs, and only then the name the blueprint gives it.

---

### Memory between steps

**The problem.** Each model call is independent. The model does not remember the previous call. Without something holding continuity, step two would begin from the same ignorance as step one and the agent could never build on what it found.

**What has to be true.** Something must accumulate the objective, what has been called, what came back, what failed, and how much budget is left — and it must survive from one iteration to the next.

**Where it belongs.** With the orchestrator. It is the only component present for the whole run.

**Called:** `RunState`.

---

### A separate view for the model

**The problem.** The stored state contains things the model must not see — failure counters, retry budgets, internal identifiers — and things it *should* not see, like raw tool payloads that would balloon the context on every step.

**What has to be true.** The model's input must be *constructed*, not handed over. A deliberate subset, shaped for decision-making.

**Where it belongs.** Alongside the state, as a transformation of it — so it can be tested on its own.

**Called:** `project()`.

---

### Checking the model's answer

**The problem.** The model's reply is text arriving from outside the program. Acting on it unchecked means executing whatever a statistical system happened to produce.

**What has to be true.** Before anything acts on a decision, the decision must be confirmed to be structurally what was asked for — right shape, right fields, right types.

**Where it belongs.** In the orchestrator, at exactly one point. If several places could interpret a decision, each would need its own check, and the weakest one would define the system's actual safety.

**Called:** validating against `ModelDecisionSchema`.

---

### Limits on how much may happen

**The problem.** A loop driven by a model can run indefinitely. Each iteration costs money and time. Without a bound, a confused model produces an unbounded bill.

**What has to be true.** Limits must be owned by the runtime, checked *before* expensive operations, and impossible for the model to influence.

**Where it belongs.** In the runtime, consulted at exactly two places: before each model call and before each tool dispatch.

**Called:** `Budget` and its `check()`.

---

### A bound on any single operation

**The problem.** Limits on *counts* do not bound *time*. A single call that hangs would freeze the run forever while the step counter sits untouched — the check only runs between operations, so it can never observe an operation that never ends.

**What has to be true.** Every individual call must have a maximum duration, enforced by the caller rather than trusted to the callee.

**Where it belongs.** Wrapped around the two places the system waits for something outside itself: the model call and the tool call.

**Called:** `withDeadline()`.

---

### Distinguishing whose clock ran out

**The problem.** When an operation is cut short, two very different things might have happened: this operation took too long, or the whole run ran out of time. They look identical at the moment of abort — but one is the tool's fault and one is not.

**What has to be true.** The system must know which clock fired, and only the first kind may count against the tool.

**Where it belongs.** In the time-bounding mechanism, which is the only thing that knows which timer fired.

**Called:** the `causedByRunDeadline` flag.

---

### Shrinking summaries of tool results

**The problem.** Tool results can be large. Everything in the model's view is re-sent on every subsequent step, so a large result is paid for repeatedly.

**What has to be true.** The model must receive a condensed version while the full result is preserved elsewhere. And the condensation must be enforced, not requested — because a tool promising to be brief is not the same as a tool being brief.

**Where it belongs.** Written by each tool, because only a tool knows what matters about its own output. Enforced by the runtime, because promises are not guarantees.

**Called:** the tool's `summarize()`, with a mechanical clamp in the execution boundary.

---

### An authoritative record of what happened

**The problem.** Without a record, nobody can answer "what did the agent do?" And the observability the whole exercise is about would not exist.

**What has to be true.** Every significant occurrence recorded, in order, append-only, and immutable once written. Durable storage is a separate concern and a production step — the architectural requirement is that history cannot be rewritten, not that it outlives the process.

**Where it belongs.** In one component that everything writes through. One doorway means ordering, immutability, redaction, and size limits are enforced once and cannot be bypassed.

**Called:** `TraceRecorder`.

---

### Hiding secret-shaped values

**The problem.** Values that look like credentials could end up in the record, or in the text sent back to the model.

**What has to be true.** Anything that looks like a secret is masked before it is written anywhere or shown to anyone.

**Where it belongs.** At two chokepoints: where events enter the record, and where text enters the model's view. The second is necessary because masking the record does nothing about text already handed to the model.

**Called:** redaction, applied in the recorder and in the model-turn construction.

---

### Citations that must exist

**The problem.** A model can assert anything, including references to evidence it never collected.

**What has to be true.** Two guarantees: every factual finding must carry at least one citation, and every citation must refer to something actually collected. The first is enforced by making an uncited finding *impossible to express*; the second by checking against the record.

**Where it belongs.** The first in the answer's required shape. The second in finalization, where the record is available.

**Called:** the required `citations` field, and `validateFinal()`.

---

### Evidence as a view, not a store

**The problem.** Maintaining an evidence list alongside the record means two things holding the same facts. They will eventually disagree, and then one is a bug you cannot identify.

**What has to be true.** One source of truth. Evidence read back from the record rather than accumulated separately.

**Where it belongs.** In finalization, computed from the record on demand.

**Called:** `buildEvidence()`.

---

### Assembling the result without asking anyone

**The problem.** When a run stops at a limit, the natural move is to ask the model to summarise what was found — which would break the promise that nothing further happens.

**What has to be true.** The final assembly must be structurally incapable of calling the model or a tool.

**Where it belongs.** In a component that is not given access to either. The guarantee is enforced by what it *can* do, not by remembering not to.

**Called:** `finalize()`.

---

### A written-down behavioural contract

**The problem.** Much of the agent's behaviour — cite your findings, treat tool errors as gaps, say when evidence is insufficient — comes from its instructions. If those are an incidental string, nobody can tell which version of them produced a given run.

**What has to be true.** The instructions are a versioned artifact, and every run records which version it used.

**Where it belongs.** Its own component, with its version stamped into the record at the start of every run.

**Called:** the versioned agent instructions.

---

# 10. Why here and not there?

Specific placement questions, and the reasoning behind each. These are the questions most likely to be asked about the design.

### Why doesn't the model enforce the execution limit?

Because **the limit exists for the case where the model is misbehaving.** A model that cannot decide when to stop is exactly the model you need a limit for — and a limit it could choose to ignore is not a limit. Enforcement must sit with something that cannot be talked out of it.

### Why doesn't a tool decide whether another tool should be called?

Because investigation strategy and capability are different things. A tool is a capability: it does one thing and returns a result. Strategy — *what should we look at next given what we now know* — is judgement about the whole investigation, and belongs to the model with the orchestrator enforcing the boundaries.

If tools could trigger tools, strategy would be scattered across every tool. Adding a fifth would mean reasoning about how it interacts with the other four, and "why did the run do that?" would no longer have a single answer.

### Why doesn't the renderer own the record?

Because display should never be part of what is true. The record is the system's history; a renderer is one way of looking at it. Two renderers exist — one for humans, one for machines — and they are *views over identical data*. If the renderer owned the data, the two views could disagree, and "what happened" would depend on how you looked.

### Why is evidence derived from the record instead of maintained separately?

Because two stores that must agree will eventually disagree. The record is already authoritative and ordered; reading evidence back from it makes drift structurally impossible. It also means the grounding check is verifying claims against **what actually happened**, not against a parallel list that could itself be wrong.

### Why is all recording funnelled through one place?

Because four properties must hold for every event: ordering, immutability, redaction, and bounded size. With one doorway, each is implemented once and cannot be bypassed. With several, every writer must get all four right, and the first one that forgets is a silent hole — most likely in redaction, which is the one where a mistake is a disclosure rather than a visible bug.

### Why are tools executed one at a time rather than in parallel?

Parallel execution would be faster when a decision requests several independent lookups. It was considered and rejected:

- **Traces become non-deterministic.** With parallel execution, event ordering depends on which call finishes first, so the record differs between runs. That makes the record harder to reason about and impossible to compare against a known-good baseline in tests.
- **Failure handling gets much harder.** If two calls fail simultaneously, the "two consecutive failures" rule needs a definition of "consecutive" that no longer obviously exists.
- **The benefit is close to zero here.** The tools read local fixture data and return in milliseconds.

So: a real cost (complexity, non-determinism) against a negligible benefit (latency on operations that are already instant). The decision shape still permits several calls in one step — they are simply run in order — so nothing structural would need to change if parallelism were ever worth it.

### Why several tool calls in one decision at all, then?

Because that is what real model providers produce. A model asked to do several independent lookups returns several requests in one response. Designing for one call per response would mean the system could not faithfully represent what providers actually send — the abstraction would leak on first contact with a real model.

### Why not use an agent framework?

Because **the control loop is the thing being demonstrated.**

The brief asks for "a real control loop rather than one hard-coded sequence," and says reviewers will examine how the loop terminates, how failures are classified, and how limits are enforced. A framework would provide all of that — and hide it. The submission would demonstrate the ability to configure someone else's loop, which is a different skill and not the one being assessed.

This is not a general argument against frameworks. On a product team with delivery pressure, using one is often correct. It is an argument about *this* task: when the mechanism is the deliverable, importing the mechanism answers a different question.

The specific thing frameworks cost here is **locatability**. The control flow must be readable in one place, top to bottom. A framework distributes it across handlers, callbacks, and configuration, and "what happens next?" stops having a single place to look.

### Why a command-line tool rather than a web interface?

Because a UI would not touch a single acceptance criterion. Every required behaviour — tool selection, multi-step investigation, the ordered record, failure handling, limits, grounded answers — is fully demonstrable in a terminal. The brief explicitly says visual polish earns nothing.

A command line is also *better* for this purpose: it is scriptable, it makes the record trivially inspectable as a file, and in a demo video the reviewer sees the actual events rather than a rendering of them.

### Why is the answer split into findings and a conclusion?

Because the brief requires that evidence be distinguishable from the agent's inferences, and there are two ways to achieve that. You can ask the model nicely to label them — or you can make them structurally separate so they *cannot* be conflated.

The second approach makes the property checkable. Findings are the part that must cite evidence; the conclusion is the part that is explicitly labelled inference. A reader can accept the findings and reject the conclusion, which is exactly the right relationship to have with an investigative tool.

### Why is the conclusion allowed to be uncited when findings are not?

Because a conclusion is *by definition* an inference that goes beyond the evidence. Requiring a citation on it would produce decorative citations — references attached to satisfy a rule rather than to support a claim, which is worse than none because it looks like grounding.

The honest arrangement: findings must cite, the conclusion is clearly marked as inference, and it may cite supporting evidence where genuine support exists.

---

# 11. Things we could have done differently

Every significant decision, with the alternative taken seriously.

---

### Sequential versus parallel tool execution

**Chosen:** one at a time, in the order requested.
**Alternative:** run independent calls concurrently.
**Why the alternative was viable:** calls in one decision are independent by contract, so concurrency is safe in principle and would reduce latency.
**Why we chose sequential:** deterministic record ordering, which makes runs comparable and testable against a baseline; simpler failure accounting; and near-zero real benefit against tools that return instantly.
**Trade-off accepted:** we give up parallel speed-up we do not currently need. The decision shape still allows several calls per step, so adopting parallelism later would not require rethinking the design.

---

### A runtime boundary versus direct model-to-tool execution

**Chosen:** every proposed call passes through checks before dispatch.
**Alternative:** let the model's request go straight to the tool.
**Why the alternative was viable:** simpler, less code, and most provider SDKs make it easy.
**Why we chose the boundary:** it is where nearly all the system's guarantees live. Without it there is no argument validation, no availability check, no time bound, no place to enforce limits. The boundary *is* the reliability.
**Trade-off accepted:** more code, and one more hop between intent and execution.

---

### Evidence derived from the record versus a separate store

**Chosen:** derived.
**Alternative:** accumulate an evidence list as the run proceeds.
**Why the alternative was viable:** it is the obvious design and marginally cheaper to read.
**Why we chose derivation:** two stores holding the same facts will eventually disagree, and then one is a bug you cannot identify. Derivation makes that impossible by construction.
**Trade-off accepted:** the list is recomputed when needed rather than kept ready. Irrelevant at this scale.

---

### A hand-built loop versus a framework

**Chosen:** hand-built.
**Alternative:** an existing agent framework.
**Why the alternative was viable:** it would have been faster to write and would supply tool-calling, retries, and tracing out of the box.
**Why we chose hand-built:** the loop is the deliverable. A framework would hide exactly the behaviour under evaluation, and would cost the property that matters most here — the control flow being readable in one place.
**Trade-off accepted:** roughly a hundred and fifty lines of orchestration we maintain ourselves, and no framework ecosystem. Both are the right price for this task; neither would be on a product team with delivery pressure.

---

### Run-local memory versus a database

**Chosen:** everything held in memory for the duration of one run; the record optionally written to a file.
**Alternative:** persist state and history to a database.
**Why the alternative was viable:** it would enable resuming an interrupted run and querying history across runs.
**Why we chose run-local:** neither capability is required, and a database would add setup the reviewer must perform before seeing anything work — against a brief that asks for a ten-minute setup.
**Trade-off accepted:** no resume, no cross-run queries. Both are *reachable* rather than merely aspirational, because the state was deliberately kept in a form that can be written out and read back — so persistence is a layer to add rather than a redesign.

---

### A terminal interface versus a streaming web UI

**Chosen:** command line.
**Alternative:** a web interface streaming the run live.
**Why the alternative was viable:** watching an investigation unfold is genuinely compelling, and would demo well.
**Why we chose the terminal:** it satisfies every acceptance criterion, needs no server, and shows the reviewer the actual events rather than a rendering of them. The brief explicitly awards nothing for visual polish.
**Trade-off accepted:** a less impressive demo, in exchange for effort spent on behaviour that is actually scored.

---

### Synthetic fixtures versus real integrations

**Chosen:** four tools reading local synthetic data.
**Alternative:** connect real logging, metrics, and deployment systems.
**Why the alternative was viable:** it would prove the tools work against real systems.
**Why we chose fixtures:** the brief places real data out of scope, and real integrations would make the whole thing unrunnable for a reviewer without credentials. Fixtures also make the demo reproducible, which matters more.
**Trade-off accepted:** we do not prove real-world integration. The tool boundary is defined so that swapping in real implementations changes no other component — which is the property that actually needed proving.

---

### Recording operational events versus recording the model's reasoning

**Chosen:** operational events only — decisions, calls, results, errors — plus a short optional one-line statement of intent.
**Alternative:** capture the model's full internal reasoning.
**Why the alternative was viable:** more detail about why the agent chose what it chose.
**Why we chose operational:** the brief explicitly forbids exposing hidden reasoning. And operationally it is the right call anyway — reasoning text is long, unstable between runs, and makes records harder to compare. What an operator actually needs is *what was done*, not a monologue about it.
**Trade-off accepted:** less insight into the model's internal process. The one-line intent, capped short enough that it cannot become a monologue, recovers most of the practical value.

---

### A real model adapter, or only a scripted one

**Chosen:** both. Scripted is the default and drives every test; the real adapter exists and is exercised once.
**Alternative:** scripted only.
**Why the alternative was viable:** every acceptance criterion is satisfied without ever contacting a real model, and the tests must not depend on one.
**Why we built both:** a boundary with only one implementation behind it is always suspect — you cannot tell whether it is a genuine seam or a shape drawn around the test double. A second implementation proves the interface was not reverse-engineered from the fake. It is also the only evidence in the submission that tool *selection* happens at all, since a scripted model is told what to select.
**Trade-off accepted:** extra code on a path that no test exercises. Bounded by keeping the adapter to translation only — no retry logic, no validation, no provider abstraction layer.

---

# 12. How the system grew from dumb to reliable

The fastest way to understand why each mechanism exists is to watch the system fail and get fixed, one problem at a time. Nothing here is architecture invented in advance; each piece is a response to something that broke.

---

### Stage 1 — The simplest thing that could work

```
   Objective ──▶ Model picks a tool ──▶ Run it ──▶ Show the result
```

**Does it work?** For one lookup, yes.
**What breaks:** it cannot do a second step. The result is shown to the user and then discarded. The agent learns nothing.

---

### Stage 2 — Loop it

```
   Objective ──▶ Model picks ──▶ Run ──▶ Result ──▶ Model picks again ──▶ ...
```

**What this fixed:** multiple steps are possible.
**What breaks:** the model has no memory. Each call starts fresh, so it might request the same tool forever, never building on anything.
**Belongs to:** the orchestrator.

---

### Stage 3 — Remember

Keep the objective and everything learned so far, and pass it along each time.

**What this fixed:** the agent can now build on what it found — the log search can use the window discovered by the status lookup. **This is the point where it becomes an agent.**
**What breaks:** nothing stops it. A model that never concludes will loop forever, costing money on every iteration.
**Belongs to:** state, owned by the orchestrator.

---

### Stage 4 — Stop it

Count the steps. Stop at a maximum.

**What this fixed:** the run terminates.
**What breaks:** the check was placed at the *end* of the loop, so "limit reached" meant one more call had already happened. Moving it to the top — before the model is consulted — is what makes the limit actually a limit.
**Belongs to:** the runtime. Never the model, because the limit exists for the case where the model is misbehaving.

---

### Stage 5 — Check what the model asks for

Validate the arguments against what the tool accepts, before dispatching.

**What this fixed:** malformed requests are caught at the boundary with a specific, correctable error, instead of causing a crash deep inside a tool.
**What breaks:** the returned value is still trusted. A buggy tool can return the wrong shape and that corruption flows into everything downstream.
**Belongs to:** the tool execution boundary.

---

### Stage 6 — Check what the tool returns

Validate the result too.

**What this fixed:** a broken tool produces a clean, visible failure rather than silent contamination.
**What breaks:** a tool that *hangs* still freezes the run. The step counter never advances because it is only checked between operations — so a single stuck call defeats every count-based limit.
**Belongs to:** the same boundary. Input and output are checked in the same place, though — importantly — with *different policies*, since a bad argument is a correctable mistake and a bad result is a code defect.

---

### Stage 7 — Bound each operation in time

Give every call a maximum duration, enforced by the caller.

**What this fixed:** no single operation can hang the run.
**What breaks:** total time is still unbounded. Six operations at four seconds each is twenty-four seconds, and nothing notices.
**Belongs to:** a wrapper around the two places the system waits on the outside world.

---

### Stage 8 — Bound the whole run

Add an overall time limit, checked at the same points as the counters and enforced *during* long calls, not only between them.

**What this fixed:** total elapsed time is bounded even when the time is spent inside a call.
**What breaks:** a subtle one. When the overall clock cuts short a tool that was working fine, it looks exactly like a tool timeout — and the tool gets blamed for the harness's own deadline.
**Belongs to:** the time-bounding mechanism, which is the only thing that knows which clock fired.

---

### Stage 9 — Work out whose fault it was

Classify every failure by who owns it: the model, the tool, or the harness. Only the tool's own failures count against the tool.

**What this fixed:** the agent degrades sensibly. A bad argument is corrected, a tool error is surfaced and the investigation continues, an overall timeout ends the run without libelling a healthy tool.
**What breaks:** a tool that fails *every* time is retried until the budget is gone. The run ends with nothing to show, when it could have degraded gracefully and produced a partial answer.
**Belongs to:** the failure policy.

---

### Stage 10 — Stop relying on what keeps failing

After two consecutive genuine failures, remove the tool from the list the model is shown.

**What this fixed:** a broken dependency produces a *degraded answer that names its gap* instead of an exhausted budget with no answer at all.
**What breaks:** nobody can see any of this happening. The agent is behaving well and there is no way to demonstrate it.
**Belongs to:** the failure policy, with the effect visible through the model's shrinking tool list.

---

### Stage 11 — Write down what happened

Record every event in order, append-only, immutable once written.

**What this fixed:** the run becomes inspectable. You can see what was asked, what was decided, what was called, what came back, what failed, and why it ended.
**What breaks:** the final answer can still assert things no tool ever returned. The record proves what *happened*; it does nothing about what is *claimed*.
**Belongs to:** one recorder that everything writes through.

---

### Stage 12 — Make the answer accountable to the record

Require every finding to cite evidence. Check every citation against what was actually collected. Separate findings from conclusions structurally.

**What this fixed:** grounding stops being a hope expressed in a prompt and becomes a checked property.
**What breaks:** anything secret-shaped in a tool result flows into both the record and the model's context.
**Belongs to:** the answer's required shape, and finalization.

---

### Stage 13 — Mask anything sensitive

Redact secret-shaped values at both points where data leaves the system's control: entering the record, and entering the model's view.

**What this fixed:** the record is safe to show and safe to ship.

---

### The shape of the whole progression

```
   works once
        ↓  loop it
   works repeatedly
        ↓  remember
   builds on what it learns          ◀── becomes an agent here
        ↓  bound the counts
   always terminates
        ↓  check inputs, then outputs
   safe at the tool boundary
        ↓  bound the time, per call then overall
   always terminates, in time too
        ↓  attribute failures correctly
   degrades instead of dying
        ↓  withdraw what keeps failing
   degrades without waste
        ↓  record everything
   observable
        ↓  require and verify citations
   grounded
        ↓  redact
   safe to show
```

**Every stage was forced by the failure of the stage before it.** That is the honest account of how this architecture came to be, and it is why nothing in it is decorative — each piece has a specific failure it was built in response to.

---

# 13. The architecture in six pictures

Six views, progressively more detailed. The first is the one to memorise.

---

## Picture 1 — The one-line mental model

```
   Objective ──▶ Model ──▶ Tool ──▶ Result ──▶ Model ──▶ … ──▶ Final answer
                   ▲                              │
                   └──────────────────────────────┘
                     each result informs the next decision
```

If someone asks what the system is, this is the answer. Everything else is the machinery that makes this loop safe, bounded, and inspectable.

---

## Picture 2 — The major parts

```
                     ┌──────────────────────┐
                     │    ORCHESTRATOR      │
                     │  owns the loop and   │
                     │  all sequencing      │
                     └──────────┬───────────┘
                                │
          ┌─────────────────────┼─────────────────────┐
          │                     │                     │
          ▼                     ▼                     ▼
   ┌─────────────┐      ┌──────────────┐      ┌──────────────┐
   │   MODEL     │      │    TOOLS     │      │    TRACE     │
   │             │      │              │      │              │
   │  judgement  │      │  capability  │      │   history    │
   │  what to do │      │  do one      │      │  what really │
   │  next       │      │  thing well  │      │  happened    │
   └──────┬──────┘      └──────┬───────┘      └──────┬───────┘
          │                     │                     │
          └─────────┬───────────┘                     │
                    ▼                                 │
            ┌───────────────┐                         │
            │    STATE      │                         │
            │               │                         │
            │  memory       │                         │
            │  between      │                         │
            │  steps        │                         │
            └───────┬───────┘                         │
                    │                                 │
                    │        ┌────────────────┐       │
                    └───────▶│    POLICY      │       │
                             │                │       │
                             │  what is       │       │
                             │  allowed       │       │
                             └────────┬───────┘       │
                                      │               │
                                      ▼               ▼
                             ┌──────────────────────────────┐
                             │        FINALIZATION          │
                             │  derives evidence, validates  │
                             │  citations, assembles the     │
                             │  result from the record.      │
                             │  Decides nothing. Calls no    │
                             │  model, dispatches no tool.   │
                             └──────────────────────────────┘
```

---

## Picture 3 — A successful run, end to end

```
   "Why did checkout-api errors spike at 14:00?"
                    │
                    ▼
   ┌───────────────────────────────────────────────────────────┐
   │ STEP 1   budget ok → ask model                            │
   │          model: "establish what changed"                  │
   │          → get_service_status                             │
   │          ← deploy v2.14.0 at 13:58, degraded 14:01   [E1] │
   └───────────────────────────┬───────────────────────────────┘
                               │ now knows: candidate trigger, precise onset
                               ▼
   ┌───────────────────────────────────────────────────────────┐
   │ STEP 2   budget ok → ask model                            │
   │          model: "confirm the spike is real, and measure"  │
   │          → get_metrics (window derived from E1)           │
   │          ← 0.2% → 8.4%; p99 180ms → 2400ms          [E2] │
   └───────────────────────────┬───────────────────────────────┘
                               │ now knows: premise confirmed, onset
                               │ corroborated, latency clue suggests waiting
                               ▼
   ┌───────────────────────────────────────────────────────────┐
   │ STEP 3   budget ok → ask model                            │
   │          model: "metrics count failures, they don't       │
   │                  describe them — I need the signature"    │
   │          → search_logs (window derived from E1)           │
   │          ← 41/47 "connection pool exhausted"        [E3] │
   └───────────────────────────┬───────────────────────────────┘
                               │ now knows: the mechanism — and it
                               │ explains the latency clue from E2
                               ▼
   ┌───────────────────────────────────────────────────────────┐
   │ STEP 4   budget ok → ask model                            │
   │          model: "I have a trigger and a mechanism but not │
   │                  the link between them"                   │
   │          → search_kb                                      │
   │          ← KB-014, known post-deploy regression     [E4] │
   └───────────────────────────┬───────────────────────────────┘
                               │ now knows: the causal link. Chain complete.
                               ▼
   ┌───────────────────────────────────────────────────────────┐
   │ STEP 5   budget ok → ask model                            │
   │          model: "further calls would add detail, not      │
   │                  change the conclusion" → FINAL           │
   │                                                            │
   │          every citation checked against E1–E4  ✓          │
   └───────────────────────────┬───────────────────────────────┘
                               ▼
              FINDINGS (cited) · CONCLUSION (inference)
              EVIDENCE · GAPS
```

---

## Picture 4 — A run where a tool breaks

```
   STEP 1   get_service_status  ✓  ────────────────────▶  [E1]

   STEP 2   get_metrics  ✗  error
                │
                ├─ Whose fault? The tool's.
                ├─ Failure count for this tool: 1
                └─ Still available. Investigation continues.

   STEP 3   get_metrics  ✗  error again
                │
                ├─ Failure count: 2 — consecutive
                ├─ WITHDRAWN for the rest of this run
                └─ Removed from the tool list the model sees

   STEP 4   the model's available tools have visibly shrunk:
                before:  [status, logs, metrics, kb]
                now:     [status, logs, kb]
                          ▲ this change, visible in the record,
                            is the clearest evidence that failure
                            handling is real

            model works with what remains
            search_logs  ✓  ──────────────────────────▶  [E2]

   STEP 5   search_kb  ✓  ────────────────────────────▶  [E3]

   STEP 6   FINAL — grounded in E1, E2, E3

            GAPS OBSERVED BY THE HARNESS
              get_metrics withdrawn after 2 consecutive failures
              error-rate magnitude was never confirmed

            GAPS REPORTED BY THE AGENT
              quantitative confirmation of the spike is missing

   The run SUCCEEDS. The answer is weaker, and says so.
```

Note the two separate gap lists. One is what the system observed; one is what the agent noticed. Keeping them apart means you can compare them — and a gap the harness saw that the agent did not mention is itself informative about the agent.

---

## Picture 5 — A run that hits its limit

```
   Limit configured: 2 model consultations

   STEP 1   budget check: 0 of 2 used  →  ALLOWED
            model → get_service_status  ✓  ──────────▶  [E1]

   STEP 2   budget check: 1 of 2 used  →  ALLOWED
            model → search_logs  ✓  ─────────────────▶  [E2]

   STEP 3   budget check: 2 of 2 used  →  EXHAUSTED
                │
                ├─ Record: stopped before the model call
                ├─ NO model call is made
                ├─ NO tool call is made
                └─ Stop
                        │
                        ▼
            Assemble the result MECHANICALLY:

               stopped because: step limit reached
               evidence collected: E1, E2 (listed)
               conclusion: none

            No model is asked to summarise. That would be
            one more call, and "no further calls except one"
            is not a limit.
```

---

## Picture 6 — The boundary that defines the system

```
   ╔═══════════════════════════════════════════════════════════╗
   ║  MODEL                                                    ║
   ║                                                           ║
   ║  DECIDES — with judgement, and without authority          ║
   ║    which tool · what arguments · when to conclude         ║
   ║    what the evidence means                                ║
   ║                                                           ║
   ║  Its output is a PROPOSAL. It executes nothing.           ║
   ╚═══════════════════════════════════════════════════════════╝
                              │
                              │  proposes
                              ▼
   ╔═══════════════════════════════════════════════════════════╗
   ║  RUNTIME                                                  ║
   ║                                                           ║
   ║  VALIDATES — is this well-formed? does the tool exist?    ║
   ║              are the arguments acceptable?                ║
   ║                                                           ║
   ║  CONTROLS  — may this run continue? is there budget?      ║
   ║              how long may this take? what happens on      ║
   ║              failure? when do we stop, and why?           ║
   ║                                                           ║
   ║  RECORDS   — what was asked, decided, called, returned,   ║
   ║              failed, and concluded                        ║
   ║                                                           ║
   ║  Has AUTHORITY, and exercises no judgement.               ║
   ╚═══════════════════════════════════════════════════════════╝
                              │
                              │  dispatches, if permitted
                              ▼
   ╔═══════════════════════════════════════════════════════════╗
   ║  TOOLS                                                    ║
   ║                                                           ║
   ║  EXECUTE — one capability each, structured in and out     ║
   ║                                                           ║
   ║  No judgement, no authority. Capability only.             ║
   ╚═══════════════════════════════════════════════════════════╝
```

Read top to bottom: **judgement without authority, then authority without judgement, then capability with neither.** That separation is the architecture. Everything else is detail.

---

# 14. How I would explain this in an interview

A natural spoken explanation, roughly two to three minutes. Not a script to memorise — the shape of the argument, in the order that makes it land.

---

**Start with the problem.**

> "The task was to build an agent that investigates an incident — something like *why did checkout-api error rates spike at 14:00* — where the answer isn't in any one place. It has to gather evidence from several sources and produce a grounded answer.
>
> The thing that makes it interesting is that you can't write it as a fixed sequence of calls. To search the logs you need a time window, and you don't know the window until you've looked up when the deploy happened. So the third step's arguments come out of the first step's results. That's what forces a loop rather than a pipeline — the next action has to be a function of everything learned so far."

**State the core idea.**

> "Once you have a loop driven by a language model, you have a new problem: something fallible is deciding what your program does next. So the architecture is built on one split — **the model decides what should happen next, and the runtime decides what's allowed to happen.**
>
> I treat the model's output the way I'd treat user input from the internet: untrusted data that happens to be shaped like instructions. It proposes an action; it never executes one. Everything between the proposal and the execution is the runtime's job."

**Walk the loop.**

> "The loop is: check whether we're allowed to continue, build a bounded view of the investigation for the model, ask it what to do, validate what it says, and if it wants a tool, check the tool exists, check the arguments, check the budget, then run it with a time bound. The result goes two places — the full result into an ordered append-only record, and a short summary into what the model knows. Then round again."

**Name the parts.**

> "There are seven responsibilities. The orchestrator owns sequencing and is the only thing with authority over what happens next. The model owns judgement. Tools own capability and are deliberately inert — they don't know what investigation they're part of. State is memory between steps, because the model has none. Policy owns the limits. The trace owns history. And finalization turns evidence plus the model's answer into the result.
>
> One detail I'd point at: finalization is structurally incapable of calling the model. That matters, because when a run stops at its limit the tempting thing is to ask the model for a closing summary — and that would break the promise that nothing further happens. The guarantee holds because of what that component *can't* do, not because someone remembered not to."

**The bit worth dwelling on — failure handling.**

> "The rule I kept coming back to is: **who owns the problem?** If the model sends bad arguments, that's the model's fault — the tool never ran, so the tool takes no penalty. If a tool crashes twice in a row, that's the tool, and we withdraw it for the rest of the run and remove it from the list the model sees, so it stops reaching for something broken.
>
> The one that took me three passes to get right: if the *overall* run clock expires while a tool is mid-flight, that looks exactly like a tool timeout — and the naive version blames the tool for the harness's own deadline. Two of those and you've withdrawn a perfectly healthy tool for something that was never its fault. So the system distinguishes which clock fired.
>
> And the philosophy throughout is degrade rather than die. When a tool goes away, the agent continues with what's left and the answer explicitly names the gap. An answer that says *metrics were unavailable, so this rests on logs and deploy timing alone* is far more useful than either a crash or a confident answer over missing evidence."

**Grounding.**

> "For the evidence requirement I wanted a checked property, not a prompt instruction. So the final answer is structurally two parts: findings, which must each cite at least one piece of collected evidence, and a conclusion, which is explicitly labelled as the agent's inference. An uncited finding is literally unrepresentable, and every citation is verified against the record of what actually happened — so the agent can't cite evidence it didn't gather.
>
> I'd also say what it *doesn't* prove: it checks the evidence exists, not that it supports the claim. Verifying that needs a second model pass or a human, and I left it out rather than overclaiming."

**Close on the trade-offs.**

> "Two decisions I'd defend. First, no agent framework — the control loop is the thing being demonstrated, and a framework would hide exactly the behaviour under evaluation. The specific thing it costs is locatability: I want *what happens next* to be readable in one file, top to bottom.
>
> Second, evidence is derived from the trace rather than kept as its own store. Two stores holding the same facts will eventually disagree, and then one is a bug you can't identify. Deriving it makes drift impossible.
>
> If I summed the whole thing up: the model is the least interesting part of the system. What I built is the loop around it — and the loop is what makes an unreliable component safe to hand real work."

---

## 14.1 Follow-up questions to be ready for

**"What if the model calls tools forever?"**
The budget is checked before every model call and before every tool dispatch. When it's exhausted the run stops there, having made zero further calls. The partial result is assembled mechanically, with no model involved.

**"How would you add a tool that does something dangerous — one needing human approval?"**
Mark the tool as requiring approval, and add a paused state to the loop. When a call to it is proposed, the run checkpoints and waits. The state was deliberately kept in a plain serializable form, so pausing and resuming doesn't need new machinery. I designed it and didn't build it, because none of the four synthetic tools is consequential enough to justify it.

**"How would you run many of these at once?"**
Each run is fully isolated already — no shared mutable state — so a worker pool over a queue would need no change to the loop. What I'd add is persisting the state per step, shipping the record to a collector instead of stdout, and a per-tool concurrency cap so one slow dependency can't starve the pool.

**"What would you keep for debugging and cost analysis?"**
The record already holds everything: what was asked, decided, called, returned, and why it ended. I capture token usage per model call but deliberately don't enforce a budget on it — that couldn't be tested deterministically on the scripted path, and untested scaffolding is worse than an honest omission. The pieces I'd watch in production are the rate of runs ending at a limit — the agent getting stuck — and the rate of tool withdrawals, which is a dependency degrading.

**"What did you deliberately not build?"**
Parallel tool execution, because it makes the record non-deterministic for no real gain here. Durable resume, because it exercises no requirement. A human-approval gate, designed but not built. And semantic verification of citations, because it needs a second model pass — I check that evidence exists, not that it supports the claim, and I say so rather than implying otherwise.

**"What's the weakest part?"**
Two things. A timed-out tool is abandoned, not killed — in-process I can stop waiting but I can't stop the work. That's safe here only because every tool is a side-effect-free read; real tools would need a process boundary. And my tests use a scripted model, so they prove the harness handles decisions correctly, not that the model *chooses* well. The single real-model run shows selection is well-formed on this objective — it doesn't establish general reliability, and I wouldn't claim it does.

---

## 14.2 The five sentences worth remembering

If everything else falls out of your head under pressure, these five carry the architecture:

1. **The model decides what should happen next; the runtime decides what is allowed to happen.**
2. **Treat model output the way you'd treat user input from the internet — untrusted data shaped like instructions.**
3. **A tool is penalised only for its own misbehaviour — never for the model's mistake or the harness's clock.**
4. **Evidence is derived from the record, because two stores that must agree will eventually disagree.**
5. **The model is the least interesting part of the system; what was built is the loop around it.**

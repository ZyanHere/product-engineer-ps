# STAGES — the build journey

**This file is the implementation order.** What we build now, what we break next, what we learn from the break, and the smallest thing that fixes it.

It is deliberately **not** [BUILD_PLAN.md](BUILD_PLAN.md), which is the destination: the list of what must eventually be true. Two different jobs, kept in two different files, because confusing them is exactly what went wrong the first time.

| | |
| --- | --- |
| **STAGES.md** | the journey — *what do we do next, and why* |
| **BUILD_PLAN.md** | the destination — *what must ultimately be true* |
| ARCHITECTURE.md · CORRECTNESS_MODEL.md · ANALYSIS.md | the reasoning we will converge on, unchanged |

---

## The loop

```
   build something small that works
            |
            v
   run it  --  really run it, not in your head
            |
            v
   ask what is wrong with it
            |
            v
   reproduce the failure  --  make it happen on purpose
            |
            v
   understand the root cause, in plain words
            |
            v
   only now: name the concept
            |
            v
   add the smallest mechanism that fixes it
            |
            v
   write the test that fails without that mechanism
            |
            v
   ask what breaks NOW
            |
            v
   repeat
```

### Five rules this file holds itself to

| | |
| --- | --- |
| **1** | **Nothing is built before the failure that motivates it.** If the only reason for a column, an index, a constraint or a predicate is "Stage 13 will need it", it does not go in. |
| **2** | **The problem comes before the vocabulary.** A stage never opens with *"now we learn about leases."* It opens with *"two workers both sent it — why?"* The term is introduced only once the failure is understood. |
| **3** | **Rework is the point, not a cost.** A simple `last_error` column is replaced by attempt history. A boolean claim flag is replaced by a lease, then by fencing. Building the wrong thing first and feeling why it is wrong is the mechanism by which the final shape becomes explainable. |
| **4** | **Every stage ends with something that runs.** Not a refactor, not scaffolding — a capability you can demonstrate from a terminal. |
| **5** | **One problem per stage.** If a stage fixes two unrelated things, it is two stages. |

### What this costs, stated up front

This order builds several things twice. That is deliberate. The alternative — writing the final schema on day one — produces code that is correct and unexplainable, which is the failure we are resetting from.

The rework is listed per stage so it is never a surprise.

---

## Stage dependency map

```
  STAGE 1   a reminder fires                         in memory, tick(now)
      |     -- restart it --
      v
  STAGE 2   it vanished                              sqlite
      |     -- create one from another terminal --
      v
  STAGE 3   it reads its own memory                  ask the store, due_at <= now
      |     -- stay down past the due time --
      v
  STAGE 4   it only runs when you tell it to         a loop, and a clock
      |     -- ask someone in another timezone --
      v
  STAGE 5   whose 9am?                               local time + IANA zone
      |     -- pick a DST boundary --
      v
  STAGE 6   that local time does not exist           detect + classify
      |     -- make the destination fail --
      v
  STAGE 7   silent failure, hammered destination     record it, back off
      |     -- make the failure permanent --
      v
  STAGE 8   it retries forever                       budget + terminal failed
      |     -- kill the process mid-send --
      v
  STAGE 9   did it send?  and they got two           attempt record + key
      |     -- crash mid-send three times --
      v
  STAGE 10  the crash was free                       spend the budget at the try
      |     -- run two workers --
      v
  STAGE 11  both of them sent it                     claiming
      |     -- kill the worker holding it --
      v
  STAGE 12  stuck, and a record with no ending       expiry + close what was left
      |     -- make the send slower than the claim --
      v
  STAGE 13  the slow one overwrote the new one       a rising claim number
      |     -- edit while it is sending --
      v
  STAGE 14  it delivered the old message             version + immutable facts
      |     -- cancel while it is sending --
      v
  STAGE 15  cancelled, history hangs open            cancel + a sweep
      |
      v
  STAGE 16  something other than a CLI needs it      HTTP API
      |
      v
  STAGE 17  all of it at once                        benchmark + submission
```

Notice two things about the order.

**Timezones come early.** They are a *product* complaint — "my reminder arrived at the wrong time" — not a distributed-systems concern. A user hits that long before they hit a concurrency bug.

**Retry causes the duplicate.** Stage 7 adds retry to stop losing reminders. Stage 9's duplicate exists *because* retry exists. And Stage 10 exists because Stage 9 added a way to die that routes around Stage 8's budget. The chain is causal, not curated: each fix creates the next problem.

---

## Stage 0 does not exist

An earlier draft opened with a clock: a `Clock` port, a `SystemClock`, a `ManualClock` that wakes sleepers in deadline order, and a syntax-tree scanner banning real time everywhere else. Four hundred lines, before a single reminder existed.

It was justified as "the microscope" -- the instrument you need to observe the product, rather than a feature of it. That argument is half right and was used to smuggle in three-quarters of a mechanism.

**What Stage 1 actually needs is a parameter.** `tick(now)`. Time is an argument, not a service. Nothing sleeps, so nothing needs a clock.

**When the port genuinely arrives:** the first time the system has to *wait on its own* -- a loop that polls, rather than a command you invoke once. Until then, every experiment here is driven by passing a different `now` on the command line, which is both simpler and a more honest proof, because the value is visible in the shell history.

**When the elaborate `ManualClock` arrives:** later still. Deadline-ordered release with a yield between wakeups solves a problem that only exists with **several concurrent sleepers**, which is Stage 11, when two workers run at once.

So the retrofit that was supposed to be expensive is: a loop takes a clock, calls `now()`, and passes the result into the `tick()` that already accepts it. Small.

### What survives from day one is a rule, not code

> **Time never enters implicitly.** No function reads the current time; it is passed in. Every experiment states the instant it ran at.

That is one line in a document. At sixty lines of code you do not need a syntax-tree scanner to know you have followed it -- and when the codebase is large enough that you do, that is the failure which earns the scanner.

*(The removed work is in git at `b4e3e64` if any of it is worth pulling back when the clock is genuinely due.)*

---

# STAGE 1 — Can we have a reminder at all?

| | |
| --- | --- |
| **Capability at the end** | create a reminder, tick past its time, watch it fire |
| **Before this stage** | nothing |
| **Traces to** | AC1 (the naive half) |

### What we are trying to do

The smallest thing that could be called a reminder service. A list, a loop, a printed message.

**Deliberately absent, and all of it fine:** no database, no timezones, no retry, no history, no concurrency, no idempotency. Those are not bugs to prevent. They are the next sixteen stages, each one discovered by breaking what came before.

### Build

- **1.1** `Reminder` — an id, a UTC instant, some text, a `done` flag. Nothing else.
- **1.2** an in-memory list
- **1.3** `create(when, text)` — appends to the list
- **1.4** `tick(now)` — walk the list; anything due and not done, print it and mark it done
- **1.5** a two-command CLI: `create`, `tick --now <instant>`
  - **1.5.1** `--now` is not a test hook. It is how the thing is driven: **time is a parameter**, never something a function reaches out and reads. Every experiment in this file states the instant it ran at, and that instant is visible in the shell history

Times are UTC instants supplied by the caller. **Not because that is right** — it is not, and Stage 5 is where that becomes obvious — but because nobody has complained about it yet.

### Persistence · state · transactions

None. There is no database. `done` is a boolean in memory.

### Tests

- a reminder created for T does not fire before T
- it fires at T
- it fires exactly once — a second `tick(now)` does nothing

### Still broken

Almost everything, and that is the honest position. The list of known holes is the table of contents for this file.

### Next question

The simplest thing anyone would try: **restart it.**

---

# STAGE 2 — It vanished

| | |
| --- | --- |
| **Capability at the end** | a reminder survives the process being killed |
| **Before this stage** | reminders fire, in memory |
| **Traces to** | CORRECTNESS_MODEL I-1 · ARCHITECTURE D1 |

### Break it

```
create a reminder for 12:00
stop the process
start it again
```

### What happens

The reminder is gone. Not late, not failed — **gone**, with nothing anywhere indicating it ever existed.

### Why

The list lived in the process. When the process ended, so did the list.

This is worth sitting with for a moment, because it is the whole problem in miniature. A reminder is a **promise**, and a promise that only exists while a program happens to be running is not a promise. The user said "remind me at noon" and the system agreed — then quietly forgot, with no error and no trace.

### The concept

**Durability.** The commitment has to be written somewhere that outlives the process.

That immediately raises a second question we should answer now rather than later: written *where*, and *when*? If we write it after telling the user "done", there is a window where they believe they have a reminder and we do not. So the write happens first, and "created" means "written", not "accepted".

### Build

- **2.1** SQLite, one file, one table
  - **2.1.1** `id`, `due_at`, `text`, `done` — four columns, matching Stage 1's object exactly
  - **2.1.2** nothing else. No indexes, no constraints, no pragmas beyond opening the file. None of that has a reason yet.
- **2.2** `create` writes a row and returns only once the write has committed
- **2.3** at startup, load every row into memory; `tick()` works on that list as before; write `done` back when it fires

Step 2.3 is the naive move, and it is the natural one — *"read it in, work on it, write it out."* Stage 3 is where it falls over.

### Persistence

**New.** One table:

```
reminder(id, due_at, text, done)
```

*Why we need it:* the process dies and takes the promise with it.
*Smallest change that fixes it:* write the row before acknowledging.
*What may force a change later:* almost every stage from here.

### Tests

- create, discard the entire program, rebuild it against the same file — the reminder is still there
- it still fires after the restart
- it does not fire twice across a restart

### Still broken

Everything else. And one new thing we cannot see yet: the loop is now working from a **copy** of the database taken at boot.

### Next question

What if something changes the database while the loop is running?

---

# STAGE 3 — The loop is reading its own memory

| | |
| --- | --- |
| **Capability at the end** | the loop reacts to reminders it did not create itself |
| **Before this stage** | reminders survive restart |
| **Rework** | Stage 2's "load everything at boot" is **deleted** |
| **Traces to** | ARCHITECTURE §8.1, D4 |

### Break it

Two terminals.

```
terminal A:  start the loop, leave it running
terminal B:  create a reminder due in one minute
terminal A:  advance the clock past it
```

### What happens

Nothing. The reminder sits in the database, due, and the loop never touches it.

### Why

The loop is not looking at the database. It is looking at a **snapshot** of the database taken when it started, and that snapshot is now wrong.

The database was supposed to be where the truth lives. Instead it has quietly become a backup of a list that lives in memory — which is Stage 1's problem wearing a disguise.

### The concept

**The store is the source of truth, not a cache you load at boot.** Every cycle asks the database what is due. Nothing about the schedule lives in memory between cycles.

That has a consequence worth naming now, because the rest of this file leans on it: if nothing required for correctness lives in memory, then throwing the whole program away and rebuilding it is a no-op. Restart stops being a special case.

### Build

- **3.1** delete the boot-time load entirely
- **3.2** `tick()` asks the database directly for what is due
  - **3.2.1** the obvious first query: *what has become due since my last check?* — a window between the previous tick and now
- **3.3** fire, then write `done` back immediately

Step 3.2.1 is the naive query, and it is what most people write first, because the loop is thinking in ticks. Stage 4 is where that assumption breaks.

### Persistence · state · transactions

No schema change. The change is *who reads it and when*.

### Tests

- a reminder created by a different connection, while the loop is running, still fires
- the loop keeps no schedule state between cycles: tick twice with nothing due and nothing changes
- discarding and rebuilding the entire object graph mid-run changes nothing

### Still broken

The window in 3.2.1 assumes the loop has been running continuously.

### Next question

What if it has not been? **Stay down past a reminder's due time.**

---

# STAGE 4 — It only runs when you tell it to

> **Corrected while building.** This stage was planned as *"work due during
> downtime is lost"* — on the assumption that Stage 3 would reach for a
> windowed query (*"what became due since my last check?"*) and lose anything
> that fell between two ticks.
>
> It did not, and would not have. A window is only tempting when you have no
> way to stop a reminder re-firing; we have had a `done` flag since Stage 1, so
> the natural query was `done = 0 AND due_at <= now` — which handles overdue
> work as a side effect rather than as a feature. Writing the window anyway
> would have been staging a failure rather than finding one.
>
> So the overdue case is settled, pinned by two tests in Stage 3 (six hours
> late, and six months late — the second is the one a window would fail). What
> is genuinely still missing is this stage.

| | |
| --- | --- |
| **Capability at the end** | it runs on its own and fires things without being asked |
| **Before this stage** | the store is the truth; any program sees any other's work |
| **Rework** | `tick(now)` gains a caller that is not a human |
| **Traces to** | ARCHITECTURE §3.6, §8.5 · ANALYSIS §9.9 |

### Break it

Create a reminder. Walk away. Come back.

### What happens

Nothing. It is still waiting.

The reminder is correct, durable, visible to every program — and it will sit
there forever, because **the only thing that ever fires anything is a human
typing `tick`.**

### Why

There is no service. There is a library and a prompt.

Everything so far has been driven one instant at a time, by hand, which was
exactly right while every question was *"what does it do at this moment?"*
Nothing has ever had to decide **when to look next**.

### The concept

Something that wakes up, asks the store, and goes back to sleep.

The moment that exists, two new things are true, and the second is the one
that matters here:

- the loop has to **wait**, and how long it waits is a choice with consequences
- it has to wait *on something we control*, or every test that involves the
  passage of time has to pass real seconds to run

That second point is what finally earns a clock. Until now `now` has been a
parameter typed into a prompt — simple, honest, and completely sufficient.
A loop cannot take `now` as a parameter; it has to ask. And if it asks the
operating system, then *"does a reminder fire six months late?"* becomes a
test that waits six months.

So: a `Clock` with `now()` **and** `sleep()`. Both, because the waiting is the
part that has to be controllable — a clock that only tells the time leaves the
poll interval running on real seconds, and advancing a fake clock by six hours
would cause no polling at all.

### Build

- **4.1** a `Clock` — `now()` and `sleep()`
  - **4.1.1** a real one, and a fake one that advances only when told
  - **4.1.2** deliberately *not* built yet: anything to do with several sleepers
    at once. Deadline-ordered wakeups solve a problem that appears at Stage 11,
    when two workers run together
- **4.2** a loop: ask the store, fire what is owed, sleep, repeat
- **4.3** the loop takes a clock; `tick(now)` keeps taking a parameter, and the
  loop is simply the caller that supplies it
- **4.4** the prompt keeps working, because driving it by hand is still the
  clearest way to demonstrate a single instant

### Tests

- a reminder fires with nobody typing anything
- advancing the fake clock past the due time is enough; one second short is not
- the loop keeps no schedule state — stopping and rebuilding it mid-run changes
  nothing
- a whole year of waiting runs in milliseconds, because nothing waits on real
  time

### Still broken

The notion of *when* is still "a UTC instant somebody worked out". No human
thinks that way.

### Next question

**Show it to someone in another city.** What does "9am" mean?

---

# STAGE 5 — Whose 9am?

| | |
| --- | --- |
| **Capability at the end** | "9am in New York" and "9am in Kolkata" are different moments, and both are right |
| **Before this stage** | reminders fire reliably, at a UTC instant the caller supplied |
| **Rework** | `due_at` stops being the thing the user gives us |
| **Traces to** | ANALYSIS §9 · CORRECTNESS_MODEL I-14 |

### Break it

Give the thing to a second person in another country and ask them both to set a 9am reminder.

```
person in New York:   9am        ->  they type 14:00Z
person in Kolkata:    9am        ->  they type 03:30Z
```

Then have either of them try it in July.

### What happens

Either the user does the timezone arithmetic in their head — which is not a product — or they type their local time and it fires at the wrong moment for everyone.

And the one that really stings: a New Yorker who worked out `14:00Z` in January finds their 9am reminder arriving at 10am in July.

### Why

We asked the user for a UTC instant, which is not a thing people have. People have *"9am on Tuesday"*.

And here is the part that matters more than it first appears: **"9am on Tuesday" is not a moment in time at all.** It becomes one only when you apply a set of rules — and those rules depend on where you are and change twice a year.

So the January arithmetic was not merely inconvenient. It was *wrong for July*, because the user did not want an instant. They wanted 9am.

### The concept

**Store the intent, and store what it resolves to.**

Three things, not one:

```
   local_datetime   "2026-03-09 09:00"     what the user actually said
   iana_zone        "America/New_York"      which rulebook applies
   due_at           "2026-03-09T13:00:00Z"  where those two land
```

Why all three:

- **only the instant** — the intent is gone forever; you can never re-derive 9am
- **only local + zone** — "is it due?" becomes a calculation that can move under you
- **both** — the intent is authoritative, the instant is a computed index for the query

And why an IANA *name* rather than an offset: `-05:00` is the **answer** in January, not the **rule**. New York is `-05:00` in winter and `-04:00` in summer. Store the offset and you have kept one moment's answer and thrown away everything needed to compute any other.

### Build

- **5.1** `resolve(local_datetime, zone) -> instant`, a plain function
  - **5.1.1** it takes **no clock** — it must give the same answer today and in five years, or a stored instant is not reproducible
- **5.2** `create` takes a local time and a zone; resolves once; stores all three
- **5.3** the query still compares `due_at` — instants only, never local time
  - **5.3.1** *why never local time:* during a daylight-saving fall-back, local wall time runs **backwards** — 01:30 happens, then 01:00 happens again. A due-check against local time can fire twice or go back on itself. Instants only move forward.

### Persistence

**Changed.** `due_at` stays; `local_datetime` and `iana_zone` are added beside it.

*What failure made us need this:* the user's intent was unrecoverable and wrong six months later.
*Smallest change:* two columns and one function.
*What may force a change later:* Stage 14 - editing.

### Tests

- 09:00 New York and 09:00 Kolkata on the same date are different instants
- 09:00 New York in January and in July are different offsets — the reason the zone name is stored
- the resolver is pure: same inputs, same output
- a caller who supplies an offset is rejected — the zone is a separate field, not something smuggled in

### Still broken

The resolver is quietly wrong twice a year, and it will not tell you.

### Next question

**Pick 2026-03-08 and ask for 02:30 in New York.**

---

# STAGE 6 — That local time does not exist

| | |
| --- | --- |
| **Capability at the end** | the system knows which daylight-saving case it hit, and says so |
| **Before this stage** | local time and zone resolve to an instant |
| **Traces to** | **AC7** · CORRECTNESS_MODEL I-15, §20 decisions 1–2 · ANALYSIS §2.9 |

### Break it

```
create  02:30  2026-03-08  America/New_York    (clocks jump 02:00 -> 03:00)
create  01:30  2026-11-01  America/New_York    (clocks jump 02:00 -> 01:00)
```

### What happens

Both are accepted. Both produce a confident, plausible instant. **Neither raises anything.**

The first local time never occurs that day. The second occurs twice, an hour apart. The library picked something in both cases and did not mention it.

### Why

Local time is not continuous. Twice a year it has a hole in it and a fold in it. The library resolves both silently because there is no answer it could give that is obviously correct, and picking quietly is easier than explaining.

The consequence is a reminder that arrives an hour off, with **nothing anywhere in the system indicating anything unusual happened.** No error, no log line, no column. Just a user asking why.

### The concept

Three separate things, and only the first is the one people think of:

1. **A policy** — what *should* 02:30 mean on a day when it does not exist?
2. **Detection** — knowing you are in that case at all, since nothing tells you
3. **A record** — storing which case it was, so the answer is auditable rather than lucky

The third is the one that matters most here. Our chosen policies happen to coincide with what the library silently does anyway, so the *instant* barely changes. What changes is that the system can now **show it knew**.

### Build

- **6.1** detection, because nothing raises
  - **6.1.1** does it exist? convert to UTC and back; if the local time came back different, it never occurred
  - **6.1.2** is it ambiguous? ask for the offset under both readings; if they differ, it occurs twice
  - **6.1.3** **order matters:** in a gap *both* checks trip, so existence must be tested first or a gap gets labelled an ambiguity — a record that describes a policy we did not apply
- **6.2** the policies, chosen and written down
  - **6.2.1** does not exist → **shift forward by the gap** (02:30 → 03:30). Never early; a reminder arriving before you asked for it is a worse failure than one arriving after
  - **6.2.2** happens twice → **take the first**. Earliest moment matching the request
- **6.3** store the classification: `exact` / `gap_shifted` / `overlap_first`
- **6.4** return it from `create`, so the user is told at the time rather than surprised later

### Persistence

**Changed.** One non-nullable column: `resolution_class`.

*Why non-nullable:* a nullable one would let the record be silently skipped, which is the exact failure being fixed.

### Tests

- gap: 02:30 → 07:30Z, classified `gap_shifted`
- overlap: 01:30 → 05:30Z, classified `overlap_first`
- a gap is **not** classified as an overlap — the mutation is to swap 6.1.1 and 6.1.2
- an ordinary time is `exact`
- **`Asia/Kolkata` has the same offset in January and July.** This is a negative control, not filler: Kolkata is `+05:30` all year, so it is possible to pass "two IANA zones" while never touching a transition. This test asserts the DST tests are not passing for the wrong reason

### Still broken

Delivery is assumed to work. It prints to a terminal and that always succeeds.

### Next question

**Make the destination fail.**

---

# STAGE 7 — Silent failure, and a hammered destination

| | |
| --- | --- |
| **Capability at the end** | a failed delivery is visible, and the retry does not hammer |
| **Before this stage** | correct scheduling, correct times |
| **Traces to** | **AC3** (first half) · ANALYSIS §10 |

### Break it

Replace the destination with one that refuses, then watch for a minute.

### What happens

*Corrected after building it.* The predicted failure was hammering. The first
thing the experiment actually produced was worse, and the hammering only showed
up behind it.

```
send failed: connection refused
destination hit 1 time(s) in that minute
row: done=True
```

**One hit, and the row says delivered.** Because delivery was never inside the
system: `tick` marked the row and handed the object back, and whether anything
reached a human happened somewhere else entirely. So `done` did not mean
*delivered*, it meant *we got as far as returning it* — and a down destination
produced a silently discarded reminder with a successful-looking record behind
it.

Move delivery inside, so `done` depends on the outcome, and the predicted
failure appears:

```
destination hit 30 times in one minute
row: done=False
what the database can say about why it has not arrived: nothing
```

Now two things, and only one is obvious.

The obvious one: the destination is hit **once per poll, forever**. When it
recovers, the reminder is delivered — so the system "works", in the sense that
a self-healing infinite retry loop works.

The non-obvious one: **there is no trace of any of it.** No error recorded, no
count, no timestamp. If the destination never recovers, the reminder sits in
`not done` forever and nothing in the database explains why.

### Why

We treated "not delivered" as the same thing as "not tried". The row has one bit — done or not — and a failure looks identical to a reminder whose time has not come.

That is the actual defect: **the data model has no way to express "we tried and it did not work."**

### The concept

Two mechanisms, one problem:

- **Record the outcome.** A failed attempt is an event that happened. If it leaves no trace, the question *"why did this not arrive?"* has no answer anywhere in the system.
- **Wait longer each time.** Retrying instantly, forever, is not persistence — it is a denial-of-service against something that is already unwell.

And the delay has to come from the database, not a timer. A timer lives in the process, and Stage 2 already settled what happens to things that live in the process.

### Build

- **7.0** delivery becomes something the system does, rather than something the caller does afterwards — the step the plan had missed, and the one the other two depend on
- **7.1** the destination can fail: outcome becomes success-or-failure rather than nothing
- **7.2** record the last failure on the row: what went wrong, and when
- **7.3** back off: on failure, set the next time to try
  - **7.3.1** the reminder is simply **not due** until then. *As built:* the query grew one `COALESCE`, not a retry queue — `COALESCE(next_attempt_at, due_at) <= now`. What the plan meant, and what held: **no new state, no `retrying`, no second query, no scheduler.** A reminder waiting out a backoff is an ordinary owed one whose "not before" moved, which is why Stage 3's restart recovery keeps working without being told retries exist
  - **7.3.2b** `due_at` is **never rewritten**. Overwriting it would have made the backoff free — one column instead of two — and erased how late the delivery actually was, which is the one number anybody asks about afterwards
  - **7.3.2** the delay is computed from stored values, never from an in-process timer

### Persistence

**Changed.** `last_error`, `attempted_at`, `next_attempt_at`.

*Why the existing model is insufficient:* one boolean cannot distinguish "not yet" from "tried and failed".
*What may force a change later:* **Stage 9 replaces all three.** `last_error` keeps only the most recent failure, and we are about to need every attempt. Flagged now so the replacement is expected rather than a surprise.

### Tests

- a failing send does not mark the reminder done
- the failure and its time are recorded
- it is not retried immediately — the next attempt is in the future
- fail twice, then succeed: it is delivered, and the failures are still on the row
- the backoff grows

### Still broken

The retry has no end.

Two more, found while building rather than planned. **An unexpected exception
takes down the whole poll** — only `DeliveryError` is treated as a refusal, so a
bug in our own code escapes loudly instead of being retried for a week, and
reminders behind it wait for a restart. That is the right trade at this size and
Stage 12's problem later. And **there is no jitter**: a crowd of reminders that
failed together will march back in lockstep. Nothing here has ever had a crowd,
so there is no failure to fix yet — Stage 17 is where the lockstep becomes
visible.

### Next question

**Make the failure permanent.** Point it at a recipient that will never be valid, and leave it overnight.

---

# STAGE 8 — It retries forever

| | |
| --- | --- |
| **Capability at the end** | a reminder that cannot be delivered reaches a visible, final state |
| **Before this stage** | failures are recorded and backed off |
| **Traces to** | **AC3** (second half) · CORRECTNESS_MODEL I-17, §15 · ANALYSIS §10.4 |

### Break it

```
create a reminder for an address that is permanently invalid
leave it running
```

### What happens

*Measured, not imagined.* Three days of virtual time against a permanently invalid recipient:

```
three days later
  attempts:            81
  state the user sees: waiting
  last error:          no such recipient: nobody@invalid
  next try:            2026-03-12T13:25:15+00:00
```

Still trying, and the user's view has said *waiting* the whole time — so the system's honest summary of something impossible is *"still coming"*.

And a second, sharper observation, visible in the same output: the very first failure already told us everything. "That recipient does not exist" is not going to become true in thirty seconds. The answer on attempt 1 was the answer on attempt 81, and it was still scheduled to ask again.

### Why

Two different things were conflated into one word, *failure*:

- **the world is unwell** — unreachable, timed out, overloaded. Might differ in thirty seconds.
- **the request is wrong** — invalid recipient, malformed content. Will be identical on every retry.

Retrying the first is correct. Retrying the second is pure waste — and worse, it *delays the moment the user finds out*, because the system keeps hoping instead of reporting.

And separately: even legitimate retrying has to stop somewhere, or *pending* is a state a reminder can occupy permanently.

### The concept

**A budget, and a terminal state.**

- Count attempts. When the count is spent, stop and say so.
- Classify the failure. A request-shaped problem does not get a budget at all; it ends immediately.
- Being wrong slowly is worse than being wrong quickly. A reminder nobody can deliver should *say so*, not hover.

One detail that is easy to get wrong and expensive later: the budget must be **spent at the same moment the decision is made**. If the attempt count and the state are set by two separate writes, there is an instant where the count is spent but the reminder still looks schedulable — and it gets picked up forever. One write, or the loop comes back.

### Build

- **8.1** classify the outcome: *retryable* or *permanent*
- **8.2** `attempt_count`, a `max_attempts`
- **8.3** on a retryable failure: if budget remains, back off; otherwise stop
- **8.4** on a permanent failure: stop immediately — do not spend the remaining budget
- **8.5** a real terminal state, `failed`, with a reason recorded
  - **8.5.1** *why a reason:* "out of retries" and "never going to work" need different responses from whoever reads the report
- **8.6** the close and the decision are a **single write**
  - **8.6.1** *as built:* no behavioural test can see between two commits, so this one is checked by watching the SQL the store actually runs — `Store.trace()` exists for exactly that and nothing else. The mutation that splits the statement in two is caught, which is the only evidence that matters
- **8.7** *added while building:* `max_attempts` is **copied onto the row**, not read from the constant when a failure happens
  - **8.7.1** the plan listed the column without saying why one was needed. The reason is a deploy: lower a shared default from five to three and every reminder already on its fourth attempt becomes `failed` the moment the new process starts — a terminal decision about somebody's reminder, taken by a config change nobody connected to it. On the row, a reminder is judged by the rules it was made under

### Persistence · state

**Persistence:** `attempt_count`, `max_attempts`, `failure_reason`.

**State:** the machine grows for the first time.

```
   before:   scheduled -> delivered
   after:    scheduled -> delivered
             scheduled -> failed
```

*Why the transition exists:* a reminder that cannot be delivered must stop being scheduled, or it is polled forever.
*What enforces it:* the write that sets `failed` also spends the last of the budget.

`done` is **deleted**, not kept alongside. It could express "it worked" and "not yet"; there is now a third thing a reminder can be, and a boolean forced that third case to hide inside "not yet" — which is precisely how an invalid recipient spent three days looking like it was still coming.

One consequence worth stating: `state = 'scheduled'` enters the due-query here and *not earlier*. A `done = 0` test said the same thing while there were two outcomes. Now there are three, two of them endings, and the same clause excludes a `failed` row as excludes a delivered one — so "it is never picked up again" is a property of the query rather than something the caller has to remember.

### Tests

- three retryable failures → `failed`, reason `retries_exhausted`, exactly three attempts
- one permanent failure → `failed` immediately, reason `permanent_error`, **one** attempt
- a `failed` reminder is never picked up again
- there is no moment where the budget is spent and the state is still scheduled
- two retryable failures then a success → delivered, with the failures still in the record

### Still broken

All of this assumes the process survives long enough to write down what happened.

Three smaller holes, named rather than fixed:

- **Nothing ties `failure_reason` to `state = 'failed'`.** The code only ever writes them together, but the schema would accept a `scheduled` row with a reason on it. A `CHECK` constraint is the fix and it has no failure behind it yet.
- **`attempt_count` counts what *we* did, not what the destination saw.** They are the same number only while every send either clearly worked or clearly did not. Stage 9 introduces the third answer.
- **The poll interval is still only latency.** A long interval delays the moment the budget runs out but cannot change *which* terminal state is reached, because the budget is a count and not a deadline. Stage 13 is where a timing parameter first decides an outcome.

### Rework this stage caused

Three Stage 7 tests now pass an enormous `max_attempts`. They were written when the retry had no ending, so they never said how long they expected it to go on — and without the override they would be measuring the *budget* instead of the *backoff*. This is rule 3 working as intended: the retrofit is small and it makes each test say out loud what it is actually asking.

One incidental lesson, worth keeping because it will happen again: the first draft of the Stage 8 tests ran to a three-day horizon at a one-second poll, because that is what the break script did. That is 259,000 laps of a loop whose answer was already `[]`, and one test took 32 seconds. **The fake clock makes a long span possible, not free.**

### Next question

**Kill it mid-send.**

---
# STAGE 9 — Did it send? And why did they get two?

| | |
| --- | --- |
| **Capability at the end** | every send has a record written *before* it, and a repeat cannot become a second notification |
| **Before this stage** | bounded retries, recorded failures |
| **Rework** | Stage 7's `last_error` / `attempted_at` are **replaced** by an attempt table |
| **Traces to** | **AC4** · CORRECTNESS_MODEL I-4, I-5, I-16 · ANALYSIS §5 |

### Break it

```
make the destination slow
kill -9 the process while the send is in flight
start it again
```

### What happens

*Measured.* `kill -9` mid-send, then a restart on the same file:

```
process 1: killed mid-send
  the notification is on their phone: ['Call the clinic']
  what our database says:  state=scheduled  attempts=0

process 2: restarted. It has to decide what to do with that row.
  state=delivered  attempts=1

what is on their phone now: ['Call the clinic', 'Call the clinic']
```

Two problems from one experiment, and they are genuinely separate.

**One:** did the notification go out or not? Look anywhere you like — there is no answer. And it is worse than the plan predicted: `attempts=0`. The crash left **no trace whatsoever**, so process 2 could not have known there was anything to be careful about.

**Two:** because we do not know, we retry. **The user gets it twice.**

### Why

The send leaves our world. A database transaction covers database rows; it does not cover a message already sitting on somebody's phone. Rolling back does not un-send anything, and keeping a transaction open across a network call just holds locks while the network is slow.

So there is a gap between "we sent" and "we recorded it", and after a crash in that gap, **three different worlds look identical from inside our database:**

```
   A   the request never arrived
   B   it arrived, and the acknowledgement was lost
   C   it arrived and was acknowledged, and we died before writing
```

No amount of looking at our own storage separates them. This is not an implementation weakness to engineer away — it is the actual shape of the problem.

Given that, there are only two strategies, and the choice is forced:

| | covers | risks |
| --- | --- | --- |
| don't retry | no duplicates | world A — the promise is silently broken |
| **retry** | world A | worlds B and C — a duplicate |

**For a reminder, a duplicate beats a silent loss.** So we retry — which means retrying has to be made safe. And we just proved the safety cannot come from our side.

### The concept

Two mechanisms, and they solve different halves.

**Write down that we are about to try, before we try.** This does not remove the uncertainty. It converts an unknown into a *known* unknown: after a crash, an attempt record with no ending means precisely *"a send may have happened"*. That is the difference between a system you can operate and one you cannot.

**Give the thing a name the far side recognises.** If both presentations carry the same identifier, the destination can see the second as a repeat. We cannot stop sending twice; we can stop the second one from *counting*.

Which leads to the only hard question in this stage: **the identifier for what, exactly?**

- the *attempt*? No — attempts are what multiply. Every retry gets a new one, so every retry becomes a new notification, and the retry mechanism becomes the source of the duplication it exists to survive.
- the *message text*? No — two genuinely different reminders that happen to say the same thing would collapse into one.
- **the reminder** — the thing the far side should act on once. Yes, for now.

That last answer is correct today and gets revisited at Stage 14, when the user edits.

### Build

- **9.1** an attempt record, replacing the `last_error` columns
  - **9.1.1** opened **before** the send, with nothing filled in for the outcome
  - **9.1.2** closed after, with what happened
  - **9.1.3** an attempt left open means *"a send may have occurred"* — the record's whole purpose
- **9.2** ordered history: every attempt, not just the last one
  - **9.2.1** `list attempts` on the CLI, because *"why did this not arrive?"* is the question this table exists to answer
- **9.3** a stable key on the send
  - **9.3.1** derived from the reminder, stored once, **never recomputed at send time** — a later "improvement" to the derivation would silently change keys mid-retry
- **9.4** a destination that recognises a repeat and reports it as one
- **9.5** and separately, a destination that recognises **nothing**
  - **9.5.1** *why both:* proving "we do not duplicate" against a destination that deduplicates proves nothing — the double absorbs exactly the bugs we are looking for. Our half of the claim is *"every presentation carried the same key"*, and that must hold against something that collapses nothing
- **9.6** **no transaction is open across the send.** Commit, send, commit. This is a rule the shape of the code has to enforce, because no predicate can
  - **9.6.1** *as built:* Stage 8 got its atomicity from a single `UPDATE`. Settling now touches two tables, so the same guarantee has to come from a single **transaction** — with an explicit `BEGIN`, because leaving the most important write in the project relying on sqlite3's implicit-transaction default is not a guarantee
- **9.7** *added while building:* `PRAGMA foreign_keys = ON`
  - **9.7.1** the `REFERENCES` clause on its own is a comment. SQLite honours it only with the pragma, and the pragma is off by default **per connection** — demonstrated: with it off, an attempt naming reminder 999 is accepted silently, and the history it belongs to can never be found again
- **9.8** *rework not in the plan:* the backoff is rewritten to take a **failure count** instead of the previous delay
  - **9.8.1** Stage 7 recovered the delay by subtracting `next_attempt_at - attempted_at`, which was a workaround for not having a count, and needed a guard because doubling a zero result stays zero forever. Stage 8 introduced a real count and this stage deletes the timestamps into `attempt`, so the workaround lost both its input and its reason to exist

### Persistence

**Changed, and this is the first real rework.** `last_error` / `attempted_at` are deleted; an `attempt` table replaces them.

*Why the existing model is insufficient:* one column holds the most recent failure. We need every attempt, including ones that never finished.
*What may force a change later:* Stage 12 adds a third way for an attempt to end; Stage 14 adds *which version* it belonged to.

### Tests

- **kill mid-send, restart → one notification** (the headline)
- every presentation of one reminder carries an identical key — asserted against the **non**-deduplicating destination
- an attempt record exists with no outcome after a crash
- the record is written before the send: crash before the send returns, and the record is already there
- ordering is stable — many attempts created in the same instant still read back in order

### Still broken

Something we have not looked at: the budget from Stage 8 is counted somewhere, and we just added a way to die without reaching that somewhere.

Three more, named rather than fixed:

- **Nothing ever closes an open attempt.** The restart opens a *second* attempt and leaves the first as it found it — deliberately, because from inside the database "abandoned" and "still in flight" are the same row, and closing it would be a guess written down as a fact. Stage 12 is where something can tell them apart.
- **An unexpected exception now leaves an open attempt too.** Correct, and worth noticing: a bug that escaped mid-send is indistinguishable from a power cut mid-send, and both mean *a send may have happened*.
- **`SimulatedCrash` cannot simulate a lost write.** It stops the process; it does not stop a write already in the operating system's buffer from failing to reach the disk. SQLite's own durability is assumed here, which is a real limit on these experiments.

### Rework this stage caused

Nine tests across Stages 7 and 8 broke, all of them on `last_error` / `attempted_at`, which no longer exist. Each now reads its evidence out of the attempt history — the same question asked of a better record. The Stage 8 single-write test kept its assertion and changed its mechanism: one `UPDATE` became two inside one transaction, and the property checked is still *exactly one commit*.

One test needed more than retrofitting. `test_ordering_is_stable...` asserted that attempts created in one instant read back in order — and a mutation to `ORDER BY started_at` **did not fail it**, because with identical timestamps SQLite happens to return rowid order anyway. It was passing by luck. The replacement uses timestamps that go *backwards*, which is not contrived: `SystemClock` reads a wall clock, wall clocks get corrected, and an NTP step between two attempts puts the later one earlier. Ordering a history by a value the outside world can move reports the sequence of events wrongly at exactly the moment somebody is reading it to work out what happened.

And one guard was wrong the moment it was written: `next_delay` had a hand-picked ceiling of 64 doublings, which a test asking for `next_delay(60)` walked straight past into `2 ** 60` seconds and an `OverflowError` from C. It is now derived from the two delay constants — `(MAX_DELAY // FIRST_DELAY).bit_length()`, which is 10 — so it cannot drift out of step with them.

### Next question

**Do the same crash three times in a row.**

---

# INTERLUDE after Stage 9 — the files, not the behaviour

Not a stage. Nothing here changes what the system does, and the whole suite was
green before and after. It is recorded because "when do you stop adding to a file
and split it" is a real decision and this is where the answer stopped being *not
yet*.

### What prompted it

A measurement, not a feeling. After Stage 9:

| | |
| --- | --- |
| `store.py` | 286 lines of code, **17 methods, two tables**, two schemas, two column lists, two row mappings |
| `core.py` | the data types **and** the service that operates on them |
| tests | **seven** copies of `DUE_AT`, seven of `naive()`, no shared module at all |

The middle one had a symptom worth naming: `store.py` imported types from
`core.py` while `core.py` imported `Store` back under a `TYPE_CHECKING` guard.
That cycle worked, and it worked because a guard was hiding it.

### What changed

```
   core.py        ->   model.py      the data. Depends on nothing but stdlib
                       service.py    the one place that decides anything

   store.py       ->   store/reminders.py   statements against `reminder`
                       store/attempts.py    statements against `attempt`
                       store/__init__.py    the connection, the schema, the commits

   (nothing)      ->   tests/shared.py      two instants, one helper, one destination
```

The import cycle is now a line: `model <- store <- service`, with no
`TYPE_CHECKING` guard holding it together.

### The seam that matters

The store did **not** split along "one file per table" for tidiness. It split
along the **transaction boundary**, and the rule that fell out is the useful
part:

> The table modules issue statements and **never commit**. The `Store` owns the
> connection and every commit.

That rule exists because of Stage 8 and Stage 9 together. Settling a delivery has
to close an attempt *and* move the reminder with no instant in between where a
reader sees one and not the other. Stage 8 got that from a single `UPDATE`; two
tables means it has to come from a single transaction — and a transaction is not
something either table can own on its own. Putting the commits in one place makes
that structural rather than remembered.

### Why not earlier, and why not later

Earlier would have been guessing. At Stage 2 the store was one table and thirty
lines; splitting it would have been a prediction about a shape that had not
appeared yet, which is rule 1 in this document violated with better intentions.

Later would have cost more. Stage 10 reworks *when the budget is spent*, which
touches both the reminder writes and the attempt writes — doing the split
afterwards means doing that merge twice.

### `tests/shared.py`, and why it is not a `conftest.py`

A conftest exists for fixtures and for pytest to find without being asked.
Nothing extracted here is a fixture: `DUE_AT` as a fixture would be an instant you
have to go and look up, written in a way that suggests it is doing something.

It is also deliberately thin — two instants, one three-line helper, one
destination. Shared setup is the easiest place in a test suite to hide something,
and setup a reader cannot see is how a test ends up asserting what nobody
intended. **Each stage keeps its own wiring visible in its own file**, because in
this project the wiring is frequently the thing under test: which store, which
destination, which clock, and in what order they were opened.

### What was left alone, and why

- **`cli.py` is the largest file now, at 225 lines of code**, most of it in one
  `_prompt` loop of 59 statements. It is a `match` over commands: flat, boring,
  and read top to bottom. Splitting it would trade something obvious for
  something indirected. It gets revisited at Stage 16, when an HTTP API needs the
  same commands and the duplication becomes real.
- **The failing destinations still ship in `delivery.py`.** `refusing`,
  `invalid`, `crash` and `flaky` are wired into the CLI on purpose: an outage you
  can only reproduce inside pytest is one nobody looks at. `NullDestination` is
  the exception — it exists only so Stage 1 to 6 tests do not print — and that is
  a fair criticism, not a defence.
- **`Store.trace()` is a test seam in the production API.** Two of this
  project's claims are about the *shape* of the writes rather than their effect,
  and no behavioural test can see between two commits. Named as a cost rather
  than justified away.
- **The prose ratio.** 878 lines of executable code carry roughly 1,400 of
  explanation. That is deliberate for an assessment, where the reasoning is the
  deliverable. On a team most of it would be commit messages and ADRs instead.

---

# STAGE 10 — The crash was free

| | |
| --- | --- |
| **Capability at the end** | a crash costs an attempt, so a crashing system still terminates |
| **Before this stage** | attempt records, stable keys, a retry budget |
| **Rework** | *where* the budget is spent moves |
| **Traces to** | CORRECTNESS_MODEL **F4**, **I-21** |

### Break it

```
max attempts is 3
kill -9 mid-send
restart.  kill again.  restart.  kill again.
```

### What happens

*Measured.* Budget of 3, killed mid-send ten times, each restart a fresh store on the same file:

```
after crash  1: state=scheduled attempt_count=0/3  attempt rows=1  presented=1
after crash  2: state=scheduled attempt_count=0/3  attempt rows=2  presented=2
after crash  3: state=scheduled attempt_count=0/3  attempt rows=3  presented=3
after crash 10: state=scheduled attempt_count=0/3  attempt rows=10  presented=10
```

It is still going. Ten crashes later, it is still going.

Check the attempt count: **zero.** The budget from Stage 8 has not moved once, while the destination has been presented with the same reminder ten times — and the tell is in the same line, because Stage 9's table gives it away: **ten attempt rows against a count of nought.** Two numbers that describe the same thing, disagreeing by ten.

### Why

Stage 8 spent the budget where it seemed natural: at the point we *record what happened*. If we never reach that point, nothing is spent.

Which is fine when a send fails — you get an answer, you write it down, the count goes up. It stops being fine now that there is a way to **die between trying and recording**, and Stage 9 is what introduced it.

Sit with the shape of this for a second, because it generalises. Stage 8 bounded the retries and the bound was real. Stage 9 added a path that skips the accounting, and in doing so quietly un-bounded them again. **A mechanism can be correct and still be defeated by a later one that routes around it.**

### The concept

**Spend the budget when you commit to trying, not when you find out how it went.**

The attempt record is already written *before* the send — that is Stage 9's whole point. Spend the budget in the same breath. Then dying mid-send costs exactly what failing mid-send costs, and a crash loop runs out of road.

There is a price, and it is worth naming rather than discovering later: a crash between opening the record and the send actually leaving burns an attempt **for a send that never happened**. We cannot tell that case apart from a crash after the send left — they look identical in the database — so we charge for both.

That is the conservative direction, chosen deliberately:

> **Over-counting terminates. Under-counting loops forever.**

Paying for a send that did not happen costs one wasted retry. Not paying for a send that did happen costs a loop with no end.

### Build

- **10.1** spend the budget in the same write that opens the attempt record
- **10.2** remove the spend from the close path entirely — one place, not two
- **10.3** the number of attempt records and the count must agree; nothing else may move either
- **10.4** *added while building:* the budget is checked **before** the attempt is opened, as well as after the send
  - **10.4.1** charging at the open creates a row shape Stage 8's invariant had ruled out: `scheduled` with the budget already gone. The write that used to spend the last attempt also closed the reminder; a crash spends it and never reaches that write. So the loop now meets a reminder it must neither send nor leave
  - **10.4.2** `Store.abandon` closes it with **no attempt row and no charge**, because nothing was attempted. Without it the loop would have to open an attempt just to have something to close — presenting the reminder once more than its budget allows, and charging for the privilege

### Persistence

No new columns. The same counter, moved.

### After

```
after crash  1: state=scheduled attempt_count=1/3  attempt rows=1  presented=1
after crash  2: state=scheduled attempt_count=2/3  attempt rows=2  presented=2
after crash  3: state=scheduled attempt_count=3/3  attempt rows=3  presented=3
after crash 10: state=failed    attempt_count=3/3  attempt rows=3  presented=3
```

Ten presentations became three — the budget, exactly. The fourth poll finds a reminder with nothing left, sends nothing, and closes it.

### Tests

- **crash mid-send three times → the reminder reaches `failed`, not an infinite loop** (the headline; the mutation is to move the spend back to the close)
- a crash before the send leaves burns an attempt — asserted deliberately, because it is a cost we chose
- an ordinary failure still spends exactly one
- the count and the number of attempt records never disagree

### Still broken

All of this still assumes **one** process doing the work.

And one honest limit on the claim in the title. A destination that crashes on
*every* send never terminates, and no budget can fix that: closing a reminder
requires a write, and the process dies before every write. What the charge buys is
narrower and still worth having — **the moment one attempt completes, the
accounting is already correct**, so it stops immediately instead of starting over.

### Rework this stage caused

Four tests. Two asserted `attempt_count == 0` after an unexpected exception, which
is now 1 and is the point of the stage: a way of failing that charges nothing is a
way of retrying forever. Two watched the SQL and looked for the *first* `BEGIN` of
a tick; a tick now opens two transactions, so they look for the last.

`attempts_left(after_this_one=True)` became `attempts_left(charged_since=1)`. The
old boolean asked "count the one about to be made", which stopped being the right
question once the row was charged before the send. The new name says what it
means: attempts charged since this snapshot was taken.

### Next question

**Run two.**

---

# STAGE 11 — Both of them sent it

| | |
| --- | --- |
| **Capability at the end** | two workers can run, and only one executes a given reminder |
| **Before this stage** | attempt history, stable keys, a budget that crashes cannot dodge |
| **Traces to** | CORRECTNESS_MODEL I-10 · ANALYSIS §8 |

### Break it

```
start two copies of the loop against the same database
create a reminder
advance past its time
```

### What happens

Both find it. Both send it. *Measured* — two `Store` objects on one file, which is what two processes have:

```
A's poll returns 1 reminder(s)
B's poll returns 1 reminder(s)   <- the same one

presented to the destination: 2 times  ['sent by A', 'sent by B']
attempt rows:                 2  ['delivered', 'refused']
budget:                       2/5  (for one reminder)
state:                        delivered
next_attempt_at:              2026-03-09 13:05:00+00:00
```

A deduplicating destination collapses the two sends, so **the user still gets one message** — Stage 9 already handled the effect. So what is actually wrong?

Wasted work, which is merely annoying. Two of five attempts spent on one reminder, so the budget now drains at a rate set by the size of the fleet. And then read the last two lines together: **`delivered`, with a retry booked for five minutes' time.** A delivered it and said so; B recorded a failure and scheduled another go; B wrote last, so B won. The row asserts two things that cannot both be true.

That is the real damage, and it only appeared because the two workers got *different answers*. With both succeeding it looks fine, which is exactly why this is the kind of bug that ships.

### How the break had to be written

The first version ran `a.tick()` then `b.tick()` and showed **nothing at all** — B's tick re-read the store, found the row already settled, and did nothing. **Sequential calls do not race.** The race is *inside* a tick, between reading and acting, so the interleaving has to be written out step by step in the order two overlapping ticks produce. Deterministic on purpose: a demonstration, not a coin flip.

### Why

Finding work and doing work were never separated. Every worker that *sees* a reminder considers itself entitled to *execute* it.

Note what is **not** the problem: the query returning the same row to both. That is fine and unavoidable — a read cannot exclude anybody. The problem is that nothing happens between reading and acting.

### The concept

**Discovering is not claiming.** A separate step where exactly one worker takes responsibility, and the others are told they did not.

The mechanism is a conditional write: *"mark this as mine, but only if it is not already somebody's."* Both workers try; the database serialises them; one changes a row and one changes nothing. The loser is not an error — it just moves on.

And a worker holding a reminder is a new situation the data model has no word for. It is not `scheduled` any more — nobody else should take it. It is not finished either.

### Build

- **11.1** a `running` state — someone has this
- **11.2** a claim: one conditional write that moves `scheduled` → `running`, only if it is still `scheduled`
  - **11.2.1** the result is *"did I get it?"* — one row changed, or none
  - **11.2.2** the discovery query already excludes `running`, so a claimed reminder disappears from everyone else's view
- **11.3** losing a claim costs nothing: no rollback, no backoff, carry on to the next candidate
- **11.4** on finishing, move out of `running` — **including a retryable failure**
  - **11.4.1** this is the one mistake the stage actually made. `defer()` recorded the backoff and forgot `state = 'scheduled'`, so the row stayed `running`, `due()` excludes `running`, and every retryable failure silently became permanent. Twenty-nine tests went red at once, which is the good version of that mistake
  - **11.4.2** the claim is **released** rather than held across the wait. Holding it would mean one worker owned a reminder for up to an hour of doing nothing, and losing that worker would lose the reminder with it. A released claim costs one conditional write to re-take

### State

```
   scheduled -> running -> delivered
             -> running -> failed
```

*Why:* "somebody is working on this" had no representation.
*What enforces it:* the claim's own predicate — `WHERE state = 'scheduled'`.

### Tests

- two workers, one reminder, **one** claim succeeds and one changes nothing
- one send, not two — asserted against a destination that merges nothing, because a deduplicating one would do our job for us
- a claimed reminder is invisible to the discovery query
- a worker that loses a claim carries on to the next item without error
- one reminder costs one attempt however many workers looked at it
- the row cannot end up saying two things, which is what the break actually produced
- **eight threads racing one claim** — the only test here with real concurrency, and it had to be added because a mutation survived

### Two things mutation testing found that review would not have

**The "loser carries on" test was not testing that at all.** It had worker A claim the middle of three reminders and then let B tick. But a claimed reminder is *invisible* to `due()`, so B never saw it, never lost anything, and changing the loser's `continue` to `break` failed no test. Losing a claim requires the loser to have **already read** the row before it was taken — the actual race — so B's poll is now frozen to a snapshot taken before A acted.

**The reason the claim must be one statement is not the reason you would give.** Every other test here interleaves *between* store calls, which cannot distinguish a single conditional write from a `SELECT state ... then UPDATE`. So eight threads were put on a barrier, and the result was more interesting than expected:

```
['database is locked' x7, True]      read-then-write
[True, False x7]                     one conditional write
```

**The read-then-write version still produces exactly one winner.** SQLite refuses the second write either way, so the invariant was never the thing at risk. What the predicate buys is *how you lose*: a transaction that read first and then tries to write after somebody else committed cannot be allowed to wait — waiting cannot make its snapshot valid again — so it fails immediately with `database is locked`. Seven losers become seven exceptions, and in the loop each one aborts a whole poll and takes every reminder behind it down.

Writing from the start leaves no snapshot to invalidate: the losers match zero rows and get `False`. **Losing becomes ordinary**, which is what 11.3 actually requires. The docstring that said "SQLite serialises them, so one wins" was true and was not the point.

### Still broken

Nothing says how long a claim lasts, and that is not a loose end — **it is a straight regression in a capability Stages 9 and 10 had.** A worker killed mid-send leaves its reminder in `running`, where `due()` cannot see it, by anybody, forever. Stage 11 traded a duplicate for a disappearance.

Eight tests across Stages 9 and 10 state the recovery behaviour that has been lost. They are marked `xfail(strict=True)` rather than rewritten, and the strictness is the whole point: a strict xfail that starts passing is reported as a **failure**, so the moment Stage 12 restores recovery every one of them goes red and has to be un-marked. Rewriting their assertions to match the broken behaviour would have quietly lowered the bar and left nothing to notice when it could be raised again.

`test_stage11.py::test_a_crashed_worker_strands_its_reminder` is the positive half of the same record — the one test in the suite that asserts broken behaviour on purpose.

### Next question

**Kill the worker while it is holding one.**

---

# STAGE 12 — Stuck forever, and a record with no ending

| | |
| --- | --- |
| **Capability at the end** | a reminder abandoned by a dead worker is picked up, and its half-written history is closed honestly |
| **Before this stage** | claiming works |
| **Rework** | the claim gains an expiry; the attempt record gains a third possible ending |
| **Traces to** | CORRECTNESS_MODEL I-2, I-11, I-16, §8 · ANALYSIS §14.3 |

### Break it

```
worker A claims a reminder
kill -9 worker A mid-send
watch worker B
```

### What happens

*Measured.* Worker A claims a reminder, sends, and dies. Worker B is alive, healthy, and polling:

```
worker B at +  0d: due=0  fired=0
worker B at +  1d: due=0  fired=0
worker B at +  7d: due=0  fired=0
worker B at +365d: due=0  fired=0

state:        running
attempt 1:    outcome=None  finished_at=None
```

Two things, one obvious and one only visible if you look at the history.

**The reminder sits in `running` forever.** Worker B never touches it. It never fires and it never fails — it simply stops, in a state that looks like progress.

**And worker A's attempt record has no ending.** It was opened before the send, as Stage 9 requires, and nobody ever closed it. It will sit there, half-written, indefinitely.

### Why

**The claim recorded that *someone took it*. It never recorded that *someone still has it*.**

There is no difference, in the database, between "a worker is actively sending this right now" and "a worker died forty minutes ago". Both look like `running`. This is the worst kind of stuck: the state says work is happening, so nothing raises an alarm and no report counts it as a failure.

The open record is the same problem seen from the history's side. Stage 9 made "we might have sent" visible on purpose — but only the worker that opened it was ever expected to close it, and that worker is gone.

Which forces a question with a genuinely uncomfortable answer: **how do we know the worker is dead?**

We do not. A worker frozen by a long pause and a worker that was killed leave exactly the same trace. Any attempt to tell them apart needs a heartbeat, and a heartbeat can be late for the same reasons the work can be late.

So we stop trying to know. The expiry does not mean "the worker is dead". It means **"we are no longer willing to wait"** — which is a decision we can actually make.

### The concept

**A claim expires**, and **taking over closes what the previous holder left open.**

The second half needs its own honest answer. What outcome do we record for an attempt nobody ever finished? Not success — we do not know that. Not failure — we do not know that either. The truthful answer is a third thing: **we never found out.**

That is not a placeholder to be tidied up later. It is permanent. We will never learn which of Stage 9's three worlds that attempt was in, and a record that later claims otherwise would be inventing knowledge.

One thing that does *not* need doing, and it is a nice payoff from the previous stage: the budget for that abandoned attempt was already spent when it was **opened**. There is nothing to reconcile. Had Stage 10 gone the other way, a takeover would now have to decide whether to charge for it.

### Build

- **12.1** the claim records when it expires
- **12.2** discovery gains a second question: anything `running` whose claim has expired
- **12.3** taking over is the same conditional write, with a different condition
- **12.4** the expiry uses the injected clock, and is **wall-clock time stored in the row** — a duration measured inside one process means nothing to a different process reading that row later
- **12.5** taking over also closes any attempt record the previous holder left open
  - **12.5.1** recorded as *we never found out*, and never revised afterwards
  - **12.5.2** it happens in the same write as the takeover, so there is no moment where the reminder has a new owner and a dangling record from the old one

### Persistence · state

**Persistence:** `claimed_until`, who claimed it, and a third possible outcome on the attempt record.

**State:** no new item state — a takeover is `running` → `running`. Ownership changed; the reminder's own situation did not. Worth noticing, because it is the first hint that *who owns this* and *what state is this in* are two different things.

### Tests

- kill a worker mid-claim → another takes over after the expiry, not before
- a live worker's claim is **not** taken — the expiry is respected in both directions
- **the dead worker's attempt record is closed, as *we never found out*** — the mutation is to skip that write
- the record is never later rewritten to success or failure
- the reminder is eventually delivered despite the crash
- the expiry survives a restart of everything
- a takeover does not touch **another** reminder's history — the mutation is to drop the `reminder_id` clause, which would close every open attempt in the database including the one a live worker is in the middle of
- a fresh claim closes nothing (the negative control, so the test above cannot pass for the wrong reason)
- a reminder crashed all the way through its budget still ends — Stage 10 and Stage 12 have to compose

### The eight xfails come back green

Stage 11 took away crash recovery and eight tests across Stages 9 and 10 were marked `xfail(strict=True)` rather than rewritten. That choice paid for itself here: the markers were simply deleted, and the tests then told me exactly what had changed rather than what had broken.

Two of them needed more than the marker removing, and both are real:

- every restart now has to **wait out the dead worker's claim** before it may take the work. The recovery is the same; it costs one claim window, which is the price of not being able to tell a dead worker from a slow one.
- `test_the_crash_does_not_close_the_attempt_it_interrupted` was renamed. Stage 9 asserted the interrupted record *stays open forever*, because tidying it would have been a guess written down as a fact. Stage 12 changed **when** it is closed, not **what it is allowed to say** — so it is now `test_the_interrupted_attempt_is_closed_only_as_unknown`.

### Where `claimed_by` stands

Recorded, and **nothing compares it.** No decision in this system is made by looking at a worker id; the takeover's conditional write does not mention it. It is there so a takeover has an attributable victim and beneficiary — with more than one worker, *"this reminder was taken from somebody"* is not actionable unless you can see it is always the same somebody.

Said plainly because Stage 13 is where comparing identities stops being observability and starts being correctness, and where a plain identity turns out not to be enough.

### Still broken

We just said the expiry does not mean the worker is dead. So what happens when it is not?

Nothing. The takeover is written entirely in terms of *we are no longer willing to wait*, and that sentence is carefully silent about the other worker, which was honest and is not enough. A worker whose claim expired is not told, cannot find out, and will finish the work it believes it still owns — writing an outcome for an attempt that somebody else has already recorded as `unknown` and replaced.

Two values now exist that were the same thing a stage ago: *what state is this reminder in* and *who owns it*. The takeover made them come apart — `running` → `running`, ownership changed, state did not — and nothing yet uses the difference.

### Next question

**Make the send take longer than the claim lasts.**

---

# STAGE 13 — The slow one came back and overwrote the new one

| | |
| --- | --- |
| **Capability at the end** | a worker that has been replaced cannot change anything |
| **Before this stage** | claims expire; abandoned history is closed |
| **Rework** | every worker write gains a condition |
| **Traces to** | CORRECTNESS_MODEL **F1** (the critical finding), I-12, I-20 · ARCHITECTURE §9, §0.6 |

### Break it

```
claim lasts 30 seconds
make the send take 40
```

### What happens

```
  12:00:00   A claims it, starts sending
  12:00:30   the claim expires        <-- A is ALIVE, still sending
  12:00:31   B takes over, sends, records it delivered
  12:00:40   A finishes, and records it delivered too
```

A wrote over a job that stopped being its own ten seconds earlier. *Measured:*

```
A claims, starts sending      state=running   attempts=[None]
B takes over and delivers     state=delivered attempts=['unknown', 'delivered']
A finishes and writes too     state=delivered attempts=['delivered', 'delivered']
```

Both wrote `delivered`, so the damage looks survivable. Change one variable — A's send *failed* while B's succeeded:

```
after B delivers:  state=delivered  next_attempt_at=None
after A's failure: state=scheduled  next_attempt_at=12:05:00
```

**A turned a delivered reminder back into a scheduled one.** It will be sent again.

And a third fault that was not in the plan, visible in the first block: A's write turned `['unknown', 'delivered']` into `['delivered', 'delivered']`. **Stage 12 promised `unknown` was permanent and never revised.** This is the one way it could be revised — which is why the attempt write now sits *inside* the fenced transaction rather than beside it.

### Why

The expiry was a decision *we* made. Nobody told A. A has no idea it was replaced, and no way to find out, because nothing it does requires it to check.

The claim answered *"may I take this?"* It never answered **"am I still the one holding it?"** — and that is a question every single write needs to ask, not just the first one.

### The concept

**Give each claim a number that only ever goes up.**

```
   A claims   ->   #101
   expires
   B claims   ->   #102        the counter moved
```

Every write a worker makes carries its number, and the write only lands if that number is still the current one. A comes back holding `#101`, the row says `#102`, and **A's write matches nothing.**

A is not notified. It does not need to be. It finds out the only way that is sound: by writing and being told nothing changed.

Two consequences worth stating, because they are the payoff for this whole chapter:

**The question we could not answer stops mattering.** We never needed to know whether A was dead or slow. We needed A's writes to be ignored once it was replaced — a different and answerable thing.

**The claim duration stops being dangerous** — with one caveat, below.

### Build

- **13.1** a number on the row, incremented by every claim
- **13.2** the claim hands that number back to the worker
- **13.3** **every** write a worker makes carries it
  - **13.3.1** enumerate them. Not just "mark delivered" — also marking failed, also putting it back for a retry, also releasing it
  - **13.3.2** the release is the dangerous one, and the least obvious. If a replaced worker can put the reminder back while the current worker is still sending, a **third** worker picks it up — three sends, not two
- **13.4** a worker whose write matches nothing stops quietly and records nothing about the reminder
- **13.5** *ordering found while building:* in `open_attempt` the attempt row is inserted **before** the fence is checked, and the whole thing rolls back on a mismatch
  - **13.5.1** checking the fence first is the obvious order and it silently disarmed the foreign key: an attempt naming a reminder that does not exist stopped raising and started being reported as an ordinary lost claim. A bug that looks like contention is a bug nobody investigates

### The caveat, stated rather than discovered later

It is tempting to conclude *"the claim duration can now be anything."* That is half right, and the half that is wrong matters.

**Safety** is genuinely unaffected: at any duration, no replaced worker can corrupt anything.

**Outcomes** are not. A takeover closes the previous attempt as *we never found out* (Stage 12), and that attempt already spent budget (Stage 10). So a claim shorter than the work it guards burns the budget on takeovers rather than on real failures — and a perfectly healthy reminder can reach `failed` having never had a real problem.

The accurate statement is therefore: **the claim duration is safety-neutral but not outcome-neutral.** What contains it is keeping the send's own deadline comfortably shorter than the claim, so live workers are rarely replaced in the first place.

### Tests

- the replaced worker's `delivered` write changes **nothing**
- the replaced worker's *release* changes nothing — the three-worker scenario
- the current worker's writes all still land
- **run the suite at several claim durations and assert the safety properties hold at every one** — no corrupted state, no second notification
- and, separately, **assert the coupling above rather than denying it**: with a claim shorter than the send and a destination that never fails, a reminder exhausts its budget on takeovers alone
- deleting the number from any one write makes a specific test fail — one mutation per write path

### What the mutations found

Eight were run, one per write path plus the shape of the transaction. Seven were caught by the tests as first written. **One was not**, and it is the one worth recording:

> changing `if settle_delivered(...)` to `if settle_delivered(...) or True` — the service honouring a rejected write and reporting it anyway — **passed the entire suite.**

Every Stage 13 test drove the store directly, so none of them could see whether the *service* respected the answer it got. 13.4 was implemented and untested.

Fixing it turned out to need no new machinery, only a better reading of what a slow send is. `tick()` claims, sends and settles in one breath, so "the claim expired mid-send" seemed inexpressible through it — but **the destination *is* the send**, so a destination that lets another worker take over while it is sending is exactly that scenario. The test now runs end to end: A sends, is replaced while sending, and reports nothing at all.

### Still broken

Every guard so far protects workers from each other. Nothing protects against **the user**.

And one limitation of the *model*, rather than of the system, worth naming because it shaped every test here. `tick(now)` uses a single instant for the claim, the attempt record and the settlement, so `started_at` and `finished_at` on an attempt are always equal and a send that takes real time cannot be expressed through the service. It made no difference to what this stage had to prove — **what matters is the order of the writes, not the seconds between them** — but it does mean an attempt record cannot currently answer "how long did that send take?". Giving the service a clock would fix it and nothing yet needs it to.

### Next question

**Edit the reminder while a worker is sending it.**

---
# STAGE 14 — It delivered the old message

| | |
| --- | --- |
| **Capability at the end** | a reminder can be changed before it fires, safely, even mid-send |
| **Before this stage** | fenced claims; workers cannot overwrite each other |
| **Rework** | the reminder table is **split in two**; the key changes shape |
| **Traces to** | **AC5** · CORRECTNESS_MODEL §3.1, I-7, I-9, I-19 · ARCHITECTURE §4, §7.2 |

### Break it

Three separate experiments; each one breaks something different.

```
a)  worker starts sending  ->  user changes the text  ->  worker finishes
b)  two people open the same reminder and both change it
c)  change only the TEXT, leave the time alone
```

### What happens

*Measured, all three.*

**(a)** The reminder is recorded as delivered — with the text the user just replaced:

```
sent:      'Bring your passport'
row says:  'Bring your DRIVING LICENCE, not your passport'
state:     delivered   (the worker's write landed: True)
```

No claim expired. Nobody was replaced. The worker still holds the current number, so its write is accepted.

**(b)** The second save silently overwrites the first:

```
person 1 saved:  'Call the clinic at 3pm'   (and was thanked)
person 2 saved:  'Call the dentist'
row now says:    'Call the dentist'
```

The first person gets a cheerful confirmation for a change that no longer exists. Nothing errors.

**(c)** The corrected message goes out and the destination says *"already handled."*

```
received:            ['Meeting at 2pm']
ignored as repeats:  ['Meeting MOVED to 4pm']
```

**The correction never arrives**, and this is the worst of the three because it is completely silent — the far side is deduplicating *correctly*.

### Why

**(a) is the important one.** Stage 12's number answers *"was I replaced?"* Nobody replaced A — so it answers *no*, correctly, and lets the write through. It has no opinion about the user, because it never moves when the user does anything.

These two look identical from the outside — a worker finishing late, holding something out of date — and each is caught by a guard that is completely blind to the other:

```
   claim expires, user does nothing      the number moved, nothing else did
   user edits, nobody was replaced       nothing moved except the user's intent
```

They change on **different events**. So there is always a case where one is current and the other is stale, in both directions. Neither can stand in for the other.

**(c) is the same realisation applied to the key.** At Stage 9 we named the key after "the reminder", which was right when a reminder had one meaning forever. Now it has several over time, and the far side needs to tell them apart.

And underneath all three, **(a) has a second half people miss.** Editing does not just make the worker's *intent* stale — it rewrites the row the worker resolved from. The instant moves while a worker is mid-send against the old one.

### The concept

**Intent has a version, and a version's facts never change.**

- a number that moves when the *user* changes something, checked by every worker write alongside the claim number
- an edit must say which version it was working from, and is refused if that is no longer current — otherwise two people editing means one of them silently loses
- the key includes the version, so a new intent is a new thing to deliver
- and the facts belonging to a version — the time, the text, the key — move somewhere **nothing can overwrite them**

That last one is a schema change, and it is the interesting one. The fix for "an edit rewrites the row underneath a worker" is not a rule saying *don't overwrite those columns* — a rule is something a person has to remember. It is to put those facts in a table that **has no update statement anywhere in the codebase**. An edit can then only append a new row and move a pointer.

Which also means older versions survive, and that turns out to matter at Stage 15.

### Build

- **14.1** a `version` on the reminder, starting at 1, incremented by an accepted edit
- **14.2** split the table
  - **14.2.1** `reminder` keeps what changes — state, claim, the version pointer
  - **14.2.2** a new insert-only table keeps what must not — local time, zone, instant, text, key. One row per version
  - **14.2.3** no update statement against it, ever. A test greps for one
- **14.3** `edit` takes the version it is based on, and is refused if that is stale
  - **14.3.1** required, not optional — one caller omitting it reintroduces the silent overwrite for everybody
  - **14.3.2** the refusal comes back with the current version, so the caller can re-read and retry
- **14.4** `version` joins the claim number in every worker write
- **14.5** the key is derived from the reminder **and its version**
- **14.6** an edit resets the retry budget — a new intent gets a fair chance
- **14.7** *corrected while building:* an edit is allowed **from any state**, terminal ones included
  - **14.7.1** the first version of `edit` refused `delivered` and `failed`, on the reasoning that a kept promise should not be reopened. That was a rule invented without a failure behind it, and experiment (c) — *the meeting moved, send a correction* — contradicted it on the first run. So did *fix the recipient on the one that failed*, which is what 14.6 exists for
  - **14.7.2** what makes it safe is the versioning itself, and that is the payoff: version 1's delivery record stays exactly where it is, the correction is version 2 with its own key, and the history shows both. Refusing would have left the user creating a second reminder that nothing connects to the first

### Persistence · state

**Persistence:** one table becomes two. `version` added. The key moves.

**State:** `running` → `scheduled` on edit — a running claim is now for a version that no longer matters.

### Tests

- edit mid-send → the item is **not** marked delivered, and the record shows what was sent
- **the pair that proves neither guard substitutes for the other:**
  - claim expires, nobody edits → the version check would have let it through; the number catches it
  - user edits, nobody replaced → the number check would have let it through; the version catches it
- two concurrent edits → one succeeds, one is refused with the current version
- **a text-only edit is delivered** — the failure from (c). The mutation is to drop the version from the key
- the instant of a superseded version is still readable, unchanged
- a superseded version's successful send can never become this reminder's delivery
- **every** worker write is refused after an edit, enumerated one at a time — deliver, retry, fail, abandon, charge. One unguarded path is the whole hole, exactly as at Stage 13

### What the mutations found

Ten were run. All ten were caught, but one deserves recording because it was *not a real mutation*: `INSERT` → `INSERT OR REPLACE` against `intent` passed the suite, and should have. An edit appends version N+1, which collides with nothing, so `OR REPLACE` never replaces anything. Replacing it with a mutation that genuinely rewrites the current version instead of appending fails **fourteen** tests.

Worth noting because a surviving mutation is only evidence of a gap when the mutation actually changes behaviour. This one did not, and reporting it as a hole would have been noise.

The grep test earns its place separately: inserting a real `UPDATE intent` statement into the source fails exactly one test, which is the only thing standing between "a table nothing updates" and "a table nothing updates *yet*".

### Still broken

If the notification already left before the edit landed, it is **gone**. It is on somebody's phone. No condition in a database reaches into the world and takes it back.

What we guarantee is narrower and worth stating precisely: it is not *recorded* as a delivery of current intent, and the history says exactly what was sent and for which version. The reminder shows as scheduled at the new time, with a successful attempt against the old one in its record.

That gap between what happened and what the item says is not a bug. It is information — and the next stage is where it gets its sharpest test.

### Rework this stage caused

Smaller than expected, and the reason is worth keeping: **`Reminder` stayed flat.** Splitting the table into `reminder` and `intent` changed every read into a join, but the object handed back still has `text`, `due_at` and the rest on it, so only seven tests broke — all of them about the fencing token becoming a two-number `Claim`, none about the schema.

The alternative shape — handing callers a `Reminder` with an `intent` object hanging off it — would have pushed the split into every call site and every test in the suite, to express something no caller needed to know.

### Next question

**Cancel it while a worker is sending.**

---

# STAGE 15 — Cancelled, and the history hangs open

| | |
| --- | --- |
| **Capability at the end** | a reminder can be stopped before it commits, and the record stays truthful |
| **Before this stage** | versioned intent, immutable per-version facts |
| **Traces to** | **AC6** · CORRECTNESS_MODEL I-8, I-16, §16 · ARCHITECTURE §0.5 |

### Break it

```
a)  worker starts sending  ->  user cancels  ->  worker finishes successfully
b)  worker starts sending  ->  user cancels  ->  kill -9 the worker
```

### What happens

*Measured.*

**(a)** The worker succeeds after the cancel lands:

```
A's write landed: True
state:            delivered
```

**Cancelled, and recorded as delivered** — the one outcome a user would call a bug without hesitating.

**(b)** The reminder is cancelled and the killed worker's record is still open:

```
at +  0d:  due=0  open attempts=1
at +  1d:  due=0  open attempts=1
at +365d:  due=0  open attempts=1
```

No ending, and there never will be one.

### Why

**(a)** Every guard so far asks *"is this still current?"* — is my claim current, is my version current. None of them asks *"has this already finished?"* Cancelling makes the reminder finished, and nothing in the worker's write path notices.

Worth being precise about what is achievable here, because it is easy to over-promise. If the send already left, the notification exists. The guarantee is not *"cancelling stops the message"* — nothing can do that. It is *"cancelling stops the message from being recorded as a delivery."*

**(b)** is subtler and much easier to miss. The open attempt is normally closed by whoever takes the reminder over next — that is how every abandoned attempt since Stage 12 gets cleaned up. But a cancelled reminder **can never be taken over again**, by design. So nothing will ever reach that record.

Cancellation turns out to be the only ending that is both caused by somebody other than the worker *and* not preceded by a takeover. Every other path either closes the record itself or leaves the reminder claimable.

### The concept

**Finished means finished, for everybody — and history still has to be closed.**

- a state a worker's writes must never be able to leave. Every worker write starts asking *"is this still running?"*, alongside the two questions it already asks
- **cancelling does not take a version.** An edit is a revision of a specific earlier state, so it needs to know which one. A cancellation is version-free: *"I do not want this, whatever it currently says."* Requiring a version would refuse a legitimate cancellation just because somebody else edited first — the worst possible failure for the one operation whose entire job is to stop a notification
- and something has to close records that no takeover will ever reach — **but not immediately.** The worker may yet come back with a real answer, and that answer is better than a guess. So: wait as long as a takeover would have waited, then close it as *we never found out*

### Build

- **15.1** a `cancelled` state, reachable from `scheduled` and from `running`
- **15.2** cancel is one conditional write: only if it has not already finished
  - **15.2.1** no version required
  - **15.2.2** cancelling an already-cancelled reminder succeeds quietly; a client retrying after a network failure must not be told it failed when its intent is already satisfied
  - **15.2.3** cancelling a delivered one is refused, and says so — that is a *different* ending and hiding it would be the worst possible silence
- **15.3** every worker write adds *"is this still running?"*
- **15.4** a sweep for attempt records nothing will ever reach
  - **15.4.1** which ones: the reminder is finished, **or** the attempt belongs to a version that has been superseded
  - **15.4.2** only after a takeover's worth of time has passed, so the owner keeps its chance to report the truth
  - **15.4.3** closed as *we never found out*, marked with who closed it — a record closed by a sweep means something different from one closed by its owner, and they call for different responses

### State

All five states now exist.

```
   scheduled -> running -> delivered
             -> running -> failed
             -> cancelled
                running -> cancelled
```

### Tests

- cancel before a claim → nothing is ever sent
- cancel mid-send → the item is `cancelled`, the send is recorded as having **happened**, and the item is **not** delivered
- delivered first, then cancel → the cancel is refused
- **no cancelled reminder is left with an open attempt record** — the mutation is to delete the sweep
- the sweep does not pre-empt a worker that comes back in time
- a worker whose reminder was cancelled can still record its own outcome — it just cannot touch the reminder
- **cancelling still works after somebody else edited** — the mutation is to make `cancel` demand a version
- the sweep leaves a live reminder's attempts alone (the negative control: a sweep that closed everything old would pass every other test here and quietly destroy every in-flight record in the system)

### What the mutations found

Thirteen were run. Eleven were caught as written. Two survived the first pass, and only one of them was a real gap:

**A real gap.** Making `cancel` demand a version passed the **entire suite** — because nothing anywhere cancelled a reminder that had moved on since the caller last looked at it. The no-version decision is the single most consequential choice in this stage and it was completely untested. `test_cancelling_still_works_after_somebody_else_edited` now covers it.

**Not a gap.** The "no grace period" mutation was `started_at <= ? || ''` — string concatenation with an empty string, which changes nothing. Rewriting it to remove the clause outright fails the right test. Same lesson as Stage 14: a surviving mutation is only evidence of a hole when the mutation actually changes behaviour.

### Rework this stage caused, and one design change it forced

Every worker write gained a third condition, so a handful of earlier tests that drove the store directly with a **hand-made licence** stopped working — correctly, because you can no longer open an attempt against a reminder you do not hold. They now claim the way a worker does, via a `hold()` helper in `tests/shared.py`.

The design change is more interesting. Until now, a worker whose reminder write was rejected rolled back **everything**, including its own attempt record — which threw away something worth keeping: *the worker knows what its own send did*, and that is better information than the guess a sweep would eventually write. So the two halves now land independently:

- the **reminder** write stays conditional on the full licence
- the **attempt** write lands either way, guarded by `outcome IS NULL` so a worker replaced mid-send still cannot revise the `unknown` its successor wrote

One Stage 14 test changed as a result. It asserted that a superseded version's send left its record open; it now asserts the record says `delivered`, against **version 1**, while the reminder does not. The history gained a fact and the item did not change, which is exactly the shape this whole chapter has been arguing for.

### Where `attempt.version` came from

Stage 14 claimed the history said *what was sent and for which version*. That was half true: `intent` kept every version, but nothing connected an attempt to one — you could infer it from timestamps, which is not the record saying it. The sweep needs it (an attempt against a superseded version is one nobody is coming back to answer for), so it exists now, and Stage 14's claim is true rather than nearly true.

### Still broken

Nothing correctness-shaped that we know of. What is missing is reach: all of this is driven from one terminal.

Two asymmetries are worth carrying forward, because both look arbitrary until you say why:

- **`edit` requires a version; `cancel` does not.** An edit is a revision of a specific earlier state, so it must say which one or two people editing means one silently loses. A cancellation is version-free.
- **`edit` is allowed on `delivered` and `failed`, and refused on `cancelled`.** The first two are endings the *system* arrived at, and correcting them is ordinary. The third is an ending the **user** chose, and editing it would quietly resurrect exactly what they stopped.

### Next question

Something other than a CLI needs to create these.

---

# STAGE 16 — Something other than a CLI needs it

> **Skipped.** Not built, and not pretended otherwise. Everything is driven from
> the CLI; the service layer is already the seam an API would sit on, and nothing
> in Stage 17 needed HTTP to be exercised. `SUBMISSION.md` says so under
> *Limitations* rather than leaving a reviewer to notice.



| | |
| --- | --- |
| **Capability at the end** | create, read, edit, cancel and inspect history over HTTP |
| **Before this stage** | the full correctness model, driven from a terminal |
| **Traces to** | ARCHITECTURE §16 |

### An honest label

**This stage is not failure-driven, and pretending otherwise would be the exact dishonesty we reset to avoid.** No experiment produces a bug that an HTTP API fixes. It is a capability: a conversational companion has to call this from somewhere.

It is late for a practical reason rather than a principled one. Create, edit and cancel changed shape at Stages 5, 8, 13 and 14. An interface built over moving semantics is rebuilt four times; built over settled ones it is mechanical.

One thing here *is* failure-shaped, and it is small: a client whose request times out and retries **creates two reminders**. We spent Stage 9 demanding that the far side recognise a repeat; offering nothing equivalent to our own callers applies the principle in one direction only.

### Build

- **16.1** FastAPI + Pydantic models over the existing services — in-process, no new logic
- **16.2** create · get · edit · cancel · list attempts · list versions
- **16.3** the responses carry what the user needs and would not otherwise learn
  - **16.3.1** creating `02:30` on a spring-forward date returns *which* case it hit and what it became — told at the time, not discovered when it arrives an hour late
  - **16.3.2** a refused edit returns the current version, so a retry is possible
  - **16.3.3** the attempt history is an endpoint, because *"why did this not arrive?"* is the question the whole record exists to answer
- **16.4** optional client-supplied request id, so a retried create returns the original instead of making a second reminder
- **16.5** **no clock-control endpoint.** The clock is a constructor argument; a `/advance-clock` route would be a production backdoor whose existence is itself a defect

### Tests

- each endpoint's success shape
- a refused edit returns 409 and the current version
- a retried create with the same request id returns the original; with a different body, it is refused
- **no route reads or writes the clock**

---

# STAGE 17 — All of it at once

| | |
| --- | --- |
| **Capability at the end** | one command that exercises every mechanism together and reports what happened |
| **Before this stage** | every mechanism built, each proven alone |
| **Traces to** | the brief's verification benchmark · BUILD_PLAN §Definition of done |

### Why this is last, and why it is not where correctness is first tested

Every mechanism arrived with a test that fails without it. This stage tests **interactions** — a takeover during a retry during a cancellation storm — which is a different thing and only possible once the parts exist.

If most of the correctness testing happened here, the previous sixteen stages were a component checklist wearing a story.

### Build

- **17.0** *added while building:* **claiming and opening the attempt become one transaction** (`Store.claim_and_begin`)
  - **17.0.1** they were two commits. A crash between them left a row `running` with no attempt record and **no budget spent** -- reclaimable, but a deterministic crash loop never ran out of road, because nothing was ever charged. Stage 10 bounded the retries and this gap quietly un-bounded them again, which is the same shape as Stage 9 routing around Stage 8
  - **17.0.2** one commit now covers claim, sweep, exhaustion check, charge and attempt row. The earliest crash it can leave behind is *attempt exists, budget spent*, which every stage since 10 already handles
  - **17.0.3** it absorbed `open_attempt` and `abandon` from the delivery path. Both survive as store operations the tests drive scenarios with; neither is called by `Reminders` any more
- **17.1** the acceptance scenarios, end to end
- **17.2** the benchmark: 20+ reminders, two zones, delivered / edited / cancelled / temporarily failing / permanently failing, a **real process kill** partway through, a forced duplicate, the clock advanced until everything settles
- **17.3** the report: counts by final state, attempt outcomes, how many repeats the far side absorbed
- **17.4** the assertion that matters most — **no key ever produced more than one notification**
- **17.5** the honest assertion — count the sends that escaped before a cancel or an edit landed, and check the count is *exactly the ones we arranged*. Not zero. Zero is not achievable, and claiming it would be a lie the record can disprove
- **17.6** re-run at a deliberately terrible claim duration and check the safety properties are unchanged
- **17.7** `SUBMISSION.md` — the decisions, and the limits

### What the benchmark actually reported

```
stage 17 - all of it at once  (claim 300.0s)

  reminders                24
  final state              cancelled=6, delivered=12, failed=6
  attempt outcomes         delivered=18, refused=21, rejected=3, unknown=1
  presentations            43
  notifications            24
  repeats absorbed         19
  escaped before a cancel  3
  escaped before an edit   3
  keys delivered twice     0
```

Forty-three presentations, twenty-four notifications. The repeats come from retries, from an exhausted budget, and from the killed worker's takeover presenting a reminder the user may already have received. The single `unknown` is that worker's abandoned record, closed by its successor.

The far side is a **file** rather than an object, because the killed worker is a real subprocess and "how many notifications did the user get?" has to be a question both processes can answer.

### The one branch that is unreachable, said out loud

`claim_and_begin` reconciles: if a successful attempt for the current version already exists, it commits the delivery instead of sending again. **No path through `Reminders` produces that state** — closing an attempt and settling its reminder are one transaction, so "succeeded but crashed before the terminal commit" is not something this codebase can leave behind. A crash there rolls back both halves and the record is swept to `unknown`, not `delivered`.

Established by instrumenting the branch to raise and running the suite (**zero** tests reach it), then probing every way to construct the state through the public API. Only `close_attempt_only` gets there.

It is kept, because the cost of being wrong is a duplicate notification — the most expensive failure this system has. But the test says so explicitly rather than leaving a reader to assume it fires, and the docstring no longer claims a cause that cannot occur.

### The sentence the submission has to contain

> Execution is at-least-once. The observable effect is exactly-once, enforced by a stable per-occurrence key deduplicated at the delivery boundary. Exactly-once *execution* is not claimed, because it is not achievable across a boundary that cannot participate in our transaction.

---

## What gets built more than once

Listed so it is never a surprise, and so the reason is on the record.

| Built at | Replaced at | Why the first version was worth building |
| --- | --- | --- |
| in-memory list (1) | SQLite (2) | you cannot feel why durability matters until something vanishes |
| load at boot (2) | poll the store (3) | the natural first move, and it teaches that the store is the truth |
| windowed query (3) | `due_at <= now` (4) | "what's new since I last looked" is how a loop thinks, and it is the wrong question |
| a UTC instant from the caller (1) | local time + zone (5) | until two people in two cities try it, the problem is invisible |
| `last_error` column (7) | attempt table (9) | one column is obviously enough, until a crash leaves you needing every attempt |
| the key names the reminder (9) | it names reminder + version (13) | correct until a reminder can mean more than one thing over time |
| claim flag (10) | claim with expiry (11) | it works, right up until the holder dies |
| expiry alone (11) | expiry + number (12) | it recovers from dead workers, and lets live slow ones corrupt things |
| one mutable table (2-13) | reminder + versions (14) | the split is only justified once an edit has to not move the ground |

**None of these is a mistake corrected.** Each is a correct answer to the question that had been asked so far, replaced when a new question arrived. That is the record of how the final architecture is derived rather than copied — and it is what makes it explainable to somebody who was not here.

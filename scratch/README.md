# scratch

Throwaway databases and anything else a run leaves behind. Everything in here
except this file is ignored by git.

The CLI stores nothing on disk by default — `python -m reminders` keeps its
reminders in memory and loses them on exit. A durable run needs a file, and
this is where it goes:

```bash
python -m reminders --db scratch/demo.sqlite3
```

Two workers sharing one database (the crash-and-takeover scenario in
`../SUBMISSION.md`) need the same path passed twice. Delete anything in here at
any time; nothing in the project reads it back.

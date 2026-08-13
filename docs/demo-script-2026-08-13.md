# Demo script — Aug 13 biller meeting (Grace, Johanna, Edgar)

**Total time:** ~12 minutes of demo inside the interview. The demo is the middle of the meeting, not the start — open with their month-end numbers (that's the baseline you need anyway), demo second, then use the demo to drive the §8 questions.

**Everything shown is synthetic.** No PHI, no real patients, no Athena credentials — a mock athenaNet stands in. Say that out loud early; it builds trust and heads off the compliance question.

---

## Before they arrive (2 minutes)

```bash
cd ~/Desktop/Cosmas/Cosmas-CAB-Tool-main/cosmaneura_billing
.venv/bin/python run_chase_demo.py     # dry run once — output should end "Demo complete"
```

Optional second act (provider Billing Tab) needs two things running — start them now if you want it:

```bash
.venv/bin/uvicorn cosmaneura_billing.api:app --port 8000
```

```bash
cd ~/Desktop/Cosmas/Cosmas-CAB-Tool-main/cosmaneura_billing/frontend && PATH="$HOME/.local/node/bin:$PATH" npm run dev
```

Then open http://localhost:5173 and click the **$ Billing tab**. (If either isn't running, skip Act 2 — Act 1 is self-contained.)

---

## Opening (45 seconds — before touching the keyboard)

> "Last week you told me card collection is the thing — Grace, you called it your mantra. And you told me the OCR isn't the problem; getting patients to actually *send the card* is. So instead of coming back with more questions, we built the thing you described and I want you to tear it apart. Everything you'll see is synthetic test data — no real patients anywhere near this."

---

## Act 1 — the insurance-card chase (~6 min)

**Show it in the browser**: http://localhost:5173 → the **Card chase** tab.
This is the billers' own screen — walk it live:

1. **The collated list.** "Nobody built this list — the system watched the
   schedule and the insurance data and collated your problem patients:
   missing cards, expired coverage, inactive eligibility, payer timeouts.
   Sorted by how soon the appointment is."
2. **Work a row.** Click **Approve & email card request** on the top patient
   — the state flips to *emailed* and the actual message shows on screen:
   patient's name, the practice, a secure upload link. Never any card data.
3. **The phone path.** Click **Record card info (call)** on another row:
   "You called, the patient reads you the card — type the payer and the LAST
   FOUR only, click Verify & write. You're the verifier; it writes to Athena
   and the row closes itself." (Do it — the row flips to *closed ✓* with the
   package id.)
4. **The upload path.** Click **Simulate patient upload** on a third row —
   same self-closing loop through the CardCapture link.
5. **The gate.** Scroll to the violet rows: "These two are direct-contract
   patients. The system refuses to chase them — a person decides. That's
   Grace's guarantee, enforced in code."
6. **The metrics strip** updates live as you work — staff touches and
   machine touches counted separately.

Prefer the terminal? The same engine runs as a script (auto-continue mode
included):

```bash
.venv/bin/python run_chase_demo.py
```

Walk the numbered output top to bottom. Beat by beat:

**[1] Synthetic practice seeded** — one line, keep moving:
> "Forty fake patients, stood up to look like a practice's athenaNet."

**[2] The detector pass — THE COLLATED LIST.** Slow down here; this is the heart:
> "Nobody built this list. The system watched the schedule and the insurance data and collated your problem patients on its own: no card on file before an upcoming visit, coverage expired, eligibility checks coming back inactive, even payer timeouts. Sorted by how soon the appointment is. This is the spreadsheet nobody has to keep anymore."

Then point at the two `human_review` rows:
> "And look at the top — two patients the system refuses to chase automatically. Those are direct-contract patients. Grace, that's your guarantee, enforced in code: they get routed to a human, never to an automated sequence. If the system can't even *verify* the list, it blocks rather than guesses."

**[3] Staff-approved outreach:**
> "Nothing goes out without your say-so. A biller approves, then the message sends — and read the message: your patient's name, the practice, and a secure upload link. Never a member ID, never card data. The system physically can't put insurance data in a message; there's a test that fails the build if anyone ever tries."

**[4] The loop closes itself:**
> "Patient taps the link, uploads the card, staff verify what was read off it — a human always confirms before anything is written. Then watch: the card writes to Athena once — resending can't duplicate it — eligibility re-checks ACTIVE, and the queue item closes *itself*. Johanna, that's your Athena-sync ask: the write and the attachment happen in the same motion, nobody re-keys it."

**[4b] Auto-continue — the punchline:**
> "And here's the part that takes the chasing off your plate entirely, if you want it. Flip one setting: you approve a patient once, and the *system* does the follow-ups — a reminder every few days, on whatever cadence you tell us, until the card lands. And when someone ignores every reminder? It doesn't nag forever — it hands them back to you as an escalation. You stay in charge; it does the legwork."

Then the honest caveat, in your voice — it's a decision you want from them today:

> "Right now the safe default is on: a person approves every message. This auto mode is built but switched off, because *you* should decide the rule — approve every message, or approve once per patient and let it run? That's the setting, and I'd rather you pick it than me."

**[5] Metrics:**
> "From day one it counts what you'd judge it by: cards outstanding, completion rate, attempts per card, time to card. And look at the last two lines — staff touches and machine touches are counted *separately*. Eight things a human did, six the system did on its own. When we tell you staff minutes went down, that number won't be hiding machine work inside it. Which is why I need your month-end numbers today — cards chased this month, minutes per card, balances outstanding. Those become the baseline this gets measured against. If it doesn't beat your numbers, it doesn't deserve to exist."

---

## Act 2 (optional, ~3 min) — the provider Billing Tab

Only if the browser is up. Frame it for THEM, not for providers:

> "Your other worry, Grace — 'my gut is it's going to create more time for us.' Fair. So the other half runs *before* claims ever reach you."

In the browser, **$ Billing tab**, encounter ENC-100237:

1. Point at the E/M card: "The provider picked 99213. The documentation supports 99214 — and there's the evidence, quote by quote. Click 'show the facts' — the level is *computed from* what's documented. It structurally cannot go the other way: we fuzz-tested the guardrail with two thousand adversarial cases; it can only hold, downgrade, or remove. Nobody can ever accuse this practice of machine upcoding."
2. Point at the held obesity code and the struck-through family-history code: "It shows the provider what it *didn't* bill and why — documented-but-unaddressed gets held, contradicted gets removed with the patient's own words. What reaches you is already defensible."
3. One line on telehealth: "And for telehealth visits it checks the note *before* submission — modality, minutes, locations — the exact things Optum and UHC have been denying you on. The provider fixes it or overrides with a reason; either way it stops landing in your queue as a denial."

---

## The asks (write their answers down — these configure the system)

1. **Baseline** — month-end close numbers: coder minutes per claim, denial counts by reason, cards chased/month, minutes per card, self-pay balances outstanding.
2. **Card chase today** — who does it, what channels, how many touches, how many cards are outstanding right now?
3. **Channels & consent** — can you text patients? Portal messages? What tone/cadence would you want — and per-message approval, or approve-once-per-patient? *(This one is load-bearing: our written rule today says every message gets human approval, so choosing approve-once means we change that rule deliberately, with you on record.)*
4. **Direct-contract list** — where does it live, who maintains it? And should direct-contract patients ever be card-chased at all, or suppressed entirely?
5. **Self-pay mechanics** — sequencing, tone, escalation, who approves. (The rails are built; the behavior waits on you.)
6. **Modifiers** — the workflow you deferred from last time; walk me through it.
7. **Athena screen-share** — five minutes of your real encounter screen so our note-splitting matches your paste targets exactly.
8. **Would the practice pay** — anchor: $59/user/month. Who's the economic buyer?

## Close

> "One system: it collates the problem patients, chases the cards on your cadence until they're in compliance, writes to Athena once, proves everything with an audit trail — and the claims that reach you were pre-checked before the provider ever hit sign. Your month-end numbers are the bar. Set it."

---

## If they push (Q&A armor)

- **"Is this real or a mock-up?"** — The pipeline is real and tested (226 automated tests). The Athena side is a stand-in until sandbox access; the engine loads real 2026 regulatory data — full ICD-10-CM, NCCI edits, CMS denial statistics with an NH slice.
- **"Will it add review work?"** — Net coder minutes per claim is instrumented from day one; the coder view has a built-in timer. If your minutes go up, we've failed by our own metric.
- **"Can it over-bill / get us audited?"** — Structurally impossible: the compliance gate can only hold, downgrade, or remove codes; every suggestion carries the exact documentation quote. The practice could hand a payer the screen.
- **"What about our direct-contract patients?"** — Hard gate, checked at every entry point, and it fails closed: if the list is missing or unreadable, the system blocks and asks a human rather than guessing.
- **"When can we touch it?"** — Real patient data requires the BAA and the deployment layer first; until then everything runs on synthetic data. Athena integration is gated on their sandbox → Marketplace clock. The mechanics you specify today ship as configuration.

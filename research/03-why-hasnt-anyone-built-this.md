# Why Hasn't Anyone Built This Yet?

> Research note for Project Venessa  
> Date: 2026-11-20

---

## The Question

The competitive research confirmed: no commercial tool exists that simulates failure scenarios from an architecture description before deployment. That's a suspicious finding. Markets are generally efficient. If a real problem exists and someone could profit from solving it, someone usually does.

So why hasn't anyone built this?

The honest answer is: **both good and bad reasons exist simultaneously.** Understanding both is critical before betting a product on the gap.

---

## 🟢 Why It Probably Hasn't Been Built Yet (Good Explanations)

### 1. LLMs Just Made the Hard Part Feasible (2023–2024)

The biggest unsolved friction in building a pre-mortem tool was always the **input problem**.

To simulate a system, you need a formal model of it — nodes, edges, latencies, timeouts, replica counts, dependencies. Getting a startup founder to produce that model was historically a massive barrier:
- Too complex to fill out as a form
- Too technical to express without SRE knowledge
- Too time-consuming to justify for a tool they haven't trusted yet

Large language models changed this. Now you can say *"describe your stack in plain English"* and extract a usable simulation graph from prose. The intake friction that made this product category impractical for small teams essentially collapsed in 2023.

**This is a timing argument, not a market validity argument. The window to build this is now.**

### 2. The Market Discovers the Need Too Late

Startups don't think about resilience until they've been burned. The sequence is always:
1. Build fast, deploy fast
2. Something breaks in production
3. Wake up to the concept of resilience engineering
4. Reach for tools — which are all chaos engineering tools requiring a live system

By the time a startup cares about pre-mortem simulation, their system is already running. They skip the pre-deployment window entirely because they don't know it existed. The problem is real but **invisible to the buyer at the moment they need the solution**.

This is a *customer discovery challenge*, not a market validity problem. It means Venessa needs to reach founders *before* their first outage — at the design or first deployment phase.

### 3. Enterprises Already Built It Internally

Netflix (GameDay), Google (DiRT — Disaster Recovery Testing), Amazon (pre-mortem culture institutionalized by Jeff Bezos's "working backwards" memos) — they all do systematic pre-mortem simulation. They built internal tools, internal processes, and internal culture around this.

But they had **zero incentive to productize it**:
- It's a competitive advantage they'd be giving away
- Their internal tooling is tightly coupled to their own infra (AWS-specific, GCP-internal, etc.)
- Spinning it out as a startup-accessible SaaS doesn't fit their business model

This means the *concept is validated at the highest scale* — the best engineering orgs on earth do this — but it was never translated into a product for the other 99.9% of teams who can't afford to build it internally.

### 4. The "Shift Resilience Left" Movement Is Young

The broader "shift left" movement in software development followed this arc:
- **Testing:** Shift testing left → automated unit/integration tests in CI (mainstream by 2015)
- **Security:** Shift security left → DevSecOps, SAST/DAST in pipelines (mainstream by 2020–2022)
- **Resilience:** Shift resilience left → pre-deployment failure simulation ← **we are here, 2024–2026**

Each wave took the previous wave's tooling maturity as a prerequisite. Resilience-left is the natural next movement. It just hasn't had a product champion yet that made it accessible to small teams.

### 5. Chaos Engineering Was "Close Enough" for Teams That Cared

For the teams that *did* think about resilience pre-deployment, existing chaos tools running against staging environments felt "close enough." LitmusChaos in a staging K8s cluster, Chaos Toolkit in CI — imperfect but workable if you had the DevOps hours to wire it up.

This solved the problem for the top 5% of engineering-mature startups. The other 95% — who don't have staging environments, don't have K8s, don't have DevOps engineers — were left completely unserved. And that 95% is Venessa's market.

---

## 🔴 Why It Might Not Work (Honest Risks)

### 1. The Accuracy Problem — The Core Risk

> *"Closest approximation to the incident is the goal"* — Mentor feedback

How do you validate that the simulation output is correct? This is the fundamental challenge.

A startup runs Venessa, sees "low risk", deploys confidently, and goes down at 2am. That's a **trust-destroying event** — not just for Venessa, but for the entire concept. The simulation is only as good as the model, and real systems have:
- Unknown-unknowns (failure modes nobody thought to model)
- Emergent behaviors from interactions between services that look safe in isolation
- Real-world variability (packet loss, GCP zone behavior, kernel behavior) that discrete-event simulation simplifies

**This is not a reason to not build it. It's a reason to frame the output correctly.**

The output must never be "you will be fine" or "you will fail." It must be:
> *"Here are the top 5 failure modes we found, ranked by estimated severity and likelihood, based on these assumptions. Treat this as a risk map, not a prediction."*

A weather forecast doesn't claim to be certain. A pre-mortem report shouldn't either.

### 2. The Input Accuracy Problem — Garbage In, Garbage Out

The simulation is only as good as what the user tells it. And startup founders, who are new to cloud architecture, will describe their system incorrectly:
- They'll forget about implicit dependencies (the auth service that every microservice secretly calls)
- They'll overestimate their replica counts ("we have autoscaling" ≠ "we have N replicas")
- They'll underestimate their p99 latencies ("it's fast" ≠ a number)

A wrong architecture description produces a wrong simulation produces wrong conclusions.

**Mitigation:** Gemini-powered intake that asks clarifying questions, flags likely gaps, and makes assumptions explicit in the output. "We assumed your database has a max of 20 connections — if this is wrong, the results will differ."

### 3. The "Who Cares Until It's Too Late" Problem

The founder who needs Venessa most is the person *least likely to know they need it*. First-time founders deploying to cloud for the first time don't have the mental model to know what a pre-mortem is or why they should do one.

This is a **marketing and education problem**, not a product problem. But it's real. The go-to-market strategy needs to intercept founders at the right moment — likely:
- When they're first setting up GCP/AWS accounts
- When they're reading "how to deploy to production" guides
- When they're in startup accelerators or programs (like this buildathon)

### 4. The Trust Calibration Problem

Large companies won't trust external tools near their architecture configs because of data sensitivity — the mentor flagged this explicitly. This is correct and mostly fine since they're not the target market.

But even small startups need to trust the tool before they act on its output. Trust is built through:
- **Accuracy over time** — recommendations that turn out to be right
- **Transparency** — showing your work, not just a verdict
- **Known incident library** — "this is similar to the GitHub outage of 2024, which had these characteristics..."

Venessa needs a trust-building strategy built into the product from day one, not bolted on later.

### 5. The "It Already Exists, Just Manually" Problem

The AWS Well-Architected Framework, Google's SRE Book pre-mortem template, GameDay exercises, and paid SRE consultants already do this — with humans, not software. Large companies pay $500/hr consultants to run architecture review sessions.

The question is: *can software do it cheaper, faster, and accessibly enough to unlock a market that currently can't afford the manual version?*

The answer is almost certainly yes. But it means Venessa is competing with "the consultant" and "the manual process" as much as with software tools.

---

## The Verdict

| Factor | Assessment |
|---|---|
| Is the gap real? | ✅ Yes — confirmed by exhaustive research |
| Is the timing right? | ✅ Yes — LLMs solved the input problem in 2023–2024 |
| Is the concept validated? | ✅ Yes — Netflix/Google/Amazon all do this internally |
| Is the accuracy risk real? | ⚠️ Yes — framing the output as a risk map (not a prediction) is non-negotiable |
| Is the customer discovery hard? | ⚠️ Yes — founders don't know they need this until after their first outage |
| Is there a trust problem? | ⚠️ Yes — needs to be designed into the product from day one |
| Is the market suspicious (gap too clean)? | ✅ No — good structural reasons explain why it hasn't been built |

---

## What This Means for Venessa

1. **Frame output as a risk map, not a verdict.** "Top failure modes, ranked by severity" — not "you will fail" or "you're safe."

2. **Make assumptions visible.** Every simulation output should show what it assumed. If the assumption is wrong, the user knows to question the result.

3. **Anchor to known real incidents.** "This failure pattern matches the Cloudflare BGP leak pattern of 2023" builds trust through reference to real events.

4. **Target the interception moment.** Reach founders when they're designing their first production architecture — not after their first outage.

5. **Don't oversell accuracy. Oversell coverage.** The value isn't "we predict your outage." The value is "we surface failure modes you hadn't considered."

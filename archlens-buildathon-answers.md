# ArchLens: Buildathon Application Answers

*Working name. Focus: pre-mortem for payment and transaction systems (fintech). Demo scenario: UPI payment app (lending app and e-commerce checkout are roadmap templates).*

## 1. One-sentence summary
ArchLens is a pre-mortem for payment systems: an AI adversary tries to break your architecture, a deterministic simulator proves which attacks succeed, and you see the failed and double-charged transactions, plus their rupee cost, before launch.

## 2. Main problem
Fintech and payment-heavy startups ship architectures with hidden weaknesses: no idempotency on retries, a single gateway with no fallback, one database serving the ledger and reporting, and no timeouts or circuit breakers. These surface only as outages or double-debits at peak traffic, costing money, customer trust and sometimes regulatory scrutiny. Architecture reviews today are manual, slow and expensive, so early teams usually skip them.

## 3. Typical user / customer
- **Primary:** CTOs, tech leads and backend engineers at Indian fintech startups (payments, lending, neobanks) preparing to launch or scale.
- **Secondary:** E-commerce and quick-commerce teams with payment checkout flows, healthtech platforms, and startups preparing a technical due-diligence report for investors.

## 4. Solution
1. **Model the system:** The user starts from a realistic template (UPI payment app) or pastes a text description. Gemini converts the description into a structured graph of services, databases, queues, caches and the calls between them, including capacity, latency, timeouts, retries and whether each write is idempotent.
2. **Payment-specific static analysis:** Deterministic rules flag missing idempotency on retried writes, single points of failure, retry amplification across layers, retries without backoff or jitter, timeouts that don't fit inside the caller's timeout, shared-database bottlenecks, and gateways with no fallback.
3. **AI Adversary (the core):** Gemini reads the architecture and proposes scenarios designed to break it, for example "PSP latency rises to 8s at evening peak while clients retry 3x". The simulator runs every proposal and keeps only the ones that actually cause failures. Gemini proposes; the simulator confirms.
4. **Pre-mortem simulation:** A discrete-event simulator pushes traffic through the graph with queues, timeouts, retries, health checks, failure injection and restart ordering. It counts failed transactions and **duplicate charges** (a retry hitting a non-idempotent payment write after the first attempt already succeeded). Gemini narrates the result as a step-by-step failure story, for example: "At 10x load the payments DB saturates in 40 seconds, checkout times out, retries triple the load, 18,000 transactions fail and 312 customers are charged twice."
5. **Transparent business impact:** Rupee cost is shown with its formula and inputs, never as a black box: `failed transactions × average ticket size × abandonment rate + duplicate charges × (refund handling cost + support cost)`. Every input is editable.
6. **Fix and re-run:** Gemini explains each finding in plain language and proposes a fix, such as an idempotency key, a queue, a replica or a circuit breaker, mapped to Google Cloud services. One click applies it to the model, the same attacks re-run, and the design is compared before and after (for example "duplicate charges: 312 → 0").
7. **Compliance hints:** A small, defensible checklist covering data residency (DPDP), audit logging on ledger writes, and a single-region dependency warning. These are hints for the team's own review, not a certification.

## 5. Potential business model
- **Free tier:** One review per month using the template library and basic checks.
- **Pay per review:** For startups needing a one-off pre-launch or pre-funding report.
- **Subscription (SaaS):** Per team or project, with unlimited reviews, saved architectures and the full rule library.
- **Enterprise:** Custom rule packs, SSO, and CI/CD integration (a GitHub PR check that flags risky architecture changes) so every change is reviewed automatically.
- **Add-on:** An investor-ready due-diligence report.

## 6. Competitors and differentiation
| Competitor | What they do | How ArchLens differs |
|---|---|---|
| AWS Well-Architected Tool, Google Cloud Architecture Framework | Questionnaire-based reviews | ArchLens analyses the actual design as a graph and simulates it, instead of relying on self-reported answers |
| Diagramming / architecture-as-code tools (Lucidchart, Cloudcraft, Structurizr) | Draw and document architectures | They don't find flaws or test behaviour under load |
| Threat-modeling tools (e.g. IriusRisk, OWASP Threat Dragon) | Security threats at design time | Focus on security, not reliability, load or money lost |
| Chaos engineering tools (Gremlin, Steadybit) | Inject failures into running systems | They need a live system. ArchLens works before anything is built |
| Observability tools (Datadog, New Relic) | Monitor production | They catch problems after launch |
| Generic LLM chat | Gives opinions on a pasted design | Unverified opinions. ArchLens's adversary is checked by a deterministic simulator, so every claim is reproducible |

**Core difference:** It works at design time, goes deep on payment failure modes (double-debits, retry storms, gateway outages), uses an AI adversary whose attacks are verified by simulation, and expresses risk as a failure story with a transparent rupee cost.

**On accuracy:** Before launch, capacity and latency are estimates. ArchLens is built for **comparison**, not prediction: "design B survives 10x traffic, design A fails at 3x" holds up even when the absolute numbers are rough. Later, real metrics can calibrate the model.

## 7. Target countries
- **Primary:** India (large fintech startup base, RBI and DPDP-driven attention on reliability and data handling).
- **Secondary:** Singapore, UAE and Southeast Asia, where fintech regulation and uptime expectations are similar.

## 8. How AI is used
- **Understanding input:** It parses text descriptions into a structured architecture graph.
- **Adversary:** It proposes failure scenarios targeted at the specific design. The simulator decides which ones succeed.
- **Explaining findings:** It turns each rule violation into a plain-language explanation tied to the specific design.
- **Writing the failure narrative:** It converts raw simulation output into a readable step-by-step pre-mortem with a business impact summary.
- **Recommending fixes:** It proposes concrete model changes and maps them to Google Cloud services.
- **Kept out of the AI:** Flaw detection, simulation and cost calculation run as deterministic code, so results are repeatable and verifiable.

## 9. How Google's AI models are leveraged
- **Gemini (via Vertex AI):**
  - Structured output turns text descriptions and adversary scenarios into strict JSON validated against a schema.
  - Reasoning generates targeted attack scenarios, explanations and fix suggestions.
  - It narrates simulation results as a failure story and writes the executive summary for the final report.
- **Grounding:** Fix suggestions are constrained to a curated catalogue of Google Cloud patterns (Pub/Sub, Memorystore, Cloud SQL replicas, Cloud Run autoscaling), so recommendations stay accurate.
- **Why Gemini fits:** Long context handles detailed system descriptions, and the same model can later add multimodal diagram upload.

## 10. Deployment and scaling on Google Cloud
- **Frontend:** React app with React Flow for the interactive graph, served from Firebase Hosting or Cloud Run.
- **Backend:** Python (FastAPI) on Cloud Run, wrapping the existing simulation engine, which already has tests. It scales to zero and autoscales on concurrent requests.
- **AI:** Gemini on Vertex AI, called from the backend.
- **Data:** Firestore for saved architectures and reports, Cloud Storage for exported reports.
- **Async work:** Pub/Sub plus Cloud Run jobs for long adversary runs (many scenarios simulated in parallel).
- **Security:** Secret Manager for keys, IAM least-privilege service accounts, and region-pinned resources (Mumbai) to respect Indian data residency.
- **Scaling path:** Cloud Run autoscaling and Firestore handle early growth. Queue-based simulation workers keep heavy jobs off the API, and the enterprise PR-check integration runs reviews on every architecture change.

---

## Scope for the 2-day build
- **Already built (ArchSim engine):** YAML architecture model, discrete-event simulator (capacity, latency, timeouts, retries with backoff/jitter, failure injection, restart ordering), static analysis, scenario suite and design comparison, with 16 passing tests.
- **Build in 2 days:** UPI template; idempotency + duplicate-charge modelling; rupee cost with visible formula; Gemini text-to-model, adversary, narrative and fix suggestions; FastAPI wrapper; minimal React Flow UI with the fix-and-re-run loop.
- **Roadmap only (not demoed):** GitHub PR check, Terraform export, diagram image upload, lending and e-commerce templates, calibration from real metrics.

# Sample Architecture Prompts for Vertex AI Text-to-YAML Testing

> **Venessa: Pre-Mortem Architecture Simulator**  
> Use these 5 diverse startup architecture prompts to test the **Natural Language $\rightarrow$ YAML** compiler powered by **Google Cloud Vertex AI (`gemini-3.5-flash`)**.  
> Each prompt is designed to produce a fully validated architecture YAML conforming to the reference standards in `./backend/models/`.

---

## Prompt 1: Quick-Commerce Instant Delivery & Flash Inventory Service
* **Domain**: E-Commerce / Quick-Commerce (10-minute delivery)
* **Architecture Style**: Microservices with high-speed caching and transactional inventory decrement
* **Adversary Stress Angle**: Flash sale traffic surge saturating the inventory database on cache misses.

### Copy-Paste Prompt:
```text
We are building a 10-minute quick-commerce grocery delivery platform. 

Users connect through a Cloudflare edge load balancer handling an initial traffic of 500 RPS. The load balancer forwards requests to an orders and checkout API service deployed on Google Cloud Run with 4 replicas (each replica handling 40 concurrent requests with a queue size of 100). 

The checkout service first checks a distributed Redis cache cluster (2 replicas, 90% warm hit rate, 2ms p50 latency) for real-time inventory counts. If there is a cache miss or during final order placement, it writes to a primary PostgreSQL Cloud SQL datastore with 25 concurrent connections and a 2000ms timeout with 2 retries. 

Additionally, the checkout API communicates with an inventory reservation service (2 replicas) with an 800ms timeout, and an external payment gateway provider (like Razorpay/Stripe) that has a 3000ms timeout and a rate limit of 250 requests per second.
```

---

## Prompt 2: B2B Multi-Tenant SaaS Workflow Automation Engine
* **Domain**: B2B Enterprise SaaS / Workflow Automation
* **Architecture Style**: Ingestion gateway with worker sharding and asynchronous queues
* **Adversary Stress Angle**: Single point of failure on the primary database and cross-cloud egress latency.

### Copy-Paste Prompt:
```text
We are developing a multi-tenant B2B SaaS workflow automation engine.

Incoming webhooks and API calls enter through an application load balancer with 3 replicas. Traffic is routed to a central workflow orchestrator service running on GKE with 4 replicas, each capable of handling 30 concurrent workflows. 

The orchestrator depends on a shared Redis session store for tenant authentication and state checking (p50 latency 2ms, p99 latency 6ms). The orchestrator routes execution tasks to two dedicated stream worker shards: shard_0 and shard_1 (each having 2 service replicas). 

All completed workflow states and audit logs are persisted to a primary analytics PostgreSQL database with 2 read replicas configured for read/write splitting (write ratio is 0.35). The workers call an external third-party customer CRM API with a strict 4000ms timeout and exponential backoff retry.
```

---

## Prompt 3: FinTech Neobank Instant P2P Payment & Ledger Service
* **Domain**: FinTech / Payments & Banking
* **Architecture Style**: Strict transactional ledger with idempotency checks and external banking gateway
* **Adversary Stress Angle**: Payment gateway outage triggering client-side retry storms and lock contention.

### Copy-Paste Prompt:
```text
We are building an instant peer-to-peer (P2P) mobile payments and banking app.

Mobile client traffic enters via an API gateway with 3 replicas handling 350 RPS. The gateway routes payment requests to a payments core service with 5 replicas (each supporting 35 concurrent requests with a queue capacity of 80).

Before processing any transaction, the payments service queries a Redis idempotency cache to verify that the transaction reference has not been executed previously. The payments service then writes to an ACID-compliant ledger datastore with 1 primary node, a pool limit of 30 connections, and strict health checks. 

For interbank settlement, the payments service makes synchronous HTTP calls to an external NPCI UPI banking switch with a 3500ms timeout, 2 retry attempts with exponential backoff and jitter, and a connection pool of 50. An asynchronous notification worker pool of 2 replicas handles sending SMS alerts to users upon completion.
```

---

## Prompt 4: Telehealth Remote Consultation & EHR Platform
* **Domain**: HealthTech / Telemedicine
* **Architecture Style**: Patient scheduling with real-time slot caching and external notification gateways
* **Adversary Stress Angle**: Database restarts during scheduled peak hours blocking boot-dependent services.

### Copy-Paste Prompt:
```text
We are launching a telehealth appointment booking and electronic health records (EHR) platform.

Patients and doctors access the system through an ingress load balancer running 2 replicas. The load balancer routes requests to an appointment booking service with 3 replicas and a concurrency of 50 requests per replica. 

The appointment service checks available doctor consultation slots in a Redis cache (p50 latency 3ms, warm hit rate 85%). On cache misses or appointment confirmations, it reads and writes to an encrypted PostgreSQL patient database that has a startup requirement on the cache. 

When a consultation is booked, the appointment service invokes an external video room provisioning API (Twilio Video) with a 4000ms timeout, and triggers an external SMS notification gateway with a 2500ms timeout and a retry limit of 2 attempts. Target SLO requires an error rate below 1% and p99 response times under 3000ms.
```

---

## Prompt 5: AI Multimodal Search & Document Intelligence Engine
* **Domain**: Generative AI / Enterprise Search
* **Architecture Style**: Compute-heavy GPU worker pipeline with vector database and embedding caches
* **Adversary Stress Angle**: GPU worker saturation during sudden traffic ramps causing upstream buffer overflow.

### Copy-Paste Prompt:
```text
We are architecting an enterprise AI multimodal document search engine.

Search queries enter through a high-throughput API gateway with 2 replicas at 150 RPS. The gateway forwards queries to a prompt parsing and query orchestrator service running with 4 replicas. 

The orchestrator first checks a vector embedding cache (warm hit rate 88%, p50 latency 3ms) to see if semantic embeddings for the query already exist. If missing, it queries a vector datastore (like Pinecone/Milvus) with 2 replicas and a 1200ms timeout. 

Once candidate document chunks are retrieved, the orchestrator sends the context to a cluster of LLM GPU worker services (6 replicas, 4 concurrent inference slots each, p50 latency 500ms, p99 latency 2500ms) with a 6000ms timeout and a fixed retry of 1 attempt. The system has an SLO of 98% success rate and maximum p99 latency of 4000ms.
```

---

## How to Test These Prompts via API

### Option A: Using `curl` against the FastAPI Backend

Make sure your backend is running (`uvicorn app.main:app --port 8080`), then execute:

```bash
curl -X POST http://localhost:8080/api/ai/text-to-model \
  -H "Content-Type: application/json" \
  -d '{
    "name": "quick_commerce_platform",
    "description": "We are building a 10-minute quick-commerce grocery delivery platform. Users connect through a Cloudflare edge load balancer handling an initial traffic of 500 RPS. The load balancer forwards requests to an orders and checkout API service deployed on Google Cloud Run with 4 replicas (each replica handling 40 concurrent requests with a queue size of 100). The checkout service first checks a distributed Redis cache cluster (2 replicas, 90% warm hit rate, 2ms p50 latency) for real-time inventory counts. If there is a cache miss or during final order placement, it writes to a primary PostgreSQL Cloud SQL datastore with 25 concurrent connections and a 2000ms timeout with 2 retries. Additionally, the checkout API communicates with an inventory reservation service (2 replicas) with an 800ms timeout, and an external payment gateway provider (like Razorpay/Stripe) that has a 3000ms timeout and a rate limit of 250 requests per second."
  }'
```

### Option B: Using Python Test Script

```python
import asyncio
from app.ai.parser import text_to_architecture_yaml

prompt = """<PASTE ANY PROMPT FROM ABOVE>"""

async def run():
    arch, yaml_str = await text_to_architecture_yaml(prompt, override_name="test_system")
    print("Architecture Name:", arch.name)
    print("Parsed Nodes:", [n.id for n in arch.nodes])
    print("Parsed Edges:", [f"{e.from_} -> {e.to}" for e in arch.edges])
    print("\n--- Generated YAML ---")
    print(yaml_str)

asyncio.run(run())
```

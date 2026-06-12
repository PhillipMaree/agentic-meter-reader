# agentic-meter-reader

An A2A agent that processes water-meter report emails for Møllebakken 36.
Residents email photos of their meters; the agent triages the mail, reads the
photos with multi-model consensus, stores the readings in Postgres, and
replies in Norwegian.

Resident instructions (Norwegian): [docs/innsending-vannmaler.pdf](docs/innsending-vannmaler.pdf)
— source is [docs/innsending-vannmaler.md](docs/innsending-vannmaler.md); regenerate with

```sh
cd docs && pandoc innsending-vannmaler.md -o innsending-vannmaler.pdf -V geometry:margin=2.5cm -V lang=nb
```

## How it works

```
Gmail inbox (on demand, no polling)
  │  POST /inbox/process  or  python -m src.agents.meter.pipeline
  ▼
1. Dedup        sha256(Date|From|Subject) — already saved → reply "allerede
                registrert" with the stored values, nothing re-analyzed
2. Triage       LLM: is this a water report? which apartment (L0–L9)?
                subject first ("Vannmåler L3"), sender email as fallback
                (config/residents.yaml); L0 = hovedmåleren
                — attachment images are archived on S3 (SeaweedFS) under
                s3://mollebakken-styret/meter/<hash>/
3. Analyze      per photo: 3 voter extractions (claude-sonnet-4-6, temp 0.4),
                mode/median aggregation; any dissent on serial/type/reading
                escalates image + votes to an arbiter (claude-fable-5);
                still untrustworthy → ask the resident for new pictures
4. Persist      one row per meter in Postgres `mollebakken.meter_readings`,
                PK (hash, meter_nr)
5. Reply        Norwegian confirmation or rejection, mail marked read
```

Failures (LLM/DB outage) leave the mail unread so the next run retries; the
resident gets exactly one reply per processed mail.

All LLM calls go through the platform's LiteLLM gateway (`gateway:` in
[config/config.yaml](config/config.yaml)) using the official Anthropic SDK
pointed at the gateway's Anthropic-compatible `/v1/messages` route.

## Prerequisites

- The `agentic-enterprise` platform stack running (Postgres on host port
  15432, LiteLLM on 14000, SeaweedFS S3 on 18333, shared docker network
  `agentic-enterprise_network_bridge`).
- Gmail OAuth credentials in `.auth/` for the configured account
  (`mail.account` in config; first run opens a browser consent flow).
- Python 3.13 + [uv](https://docs.astral.sh/uv/).

## Setup

```sh
uv sync                  # install dependencies
docker compose up -d     # one-shot: create the `mollebakken` database (idempotent)
```

Configuration lives in [config/config.yaml](config/config.yaml) (`mail`,
`agents`, `sql`, `gateway`, `s3`) with env-var overrides using `__` nesting
(`SQL__HOST`, `GATEWAY__KEY`, `S3__ENDPOINT`, …). The sender-email → apartment fallback table
is [config/residents.yaml](config/residents.yaml).

## Running

```sh
# Process unread inbox mail once (CLI)
uv run python -m src.agents.meter.pipeline

# Or run the A2A server (agent card + /inbox/process for a UI)
uv run python -m src.agents.meter.main
curl -X POST localhost:9999/inbox/process
```

The agent card is served at `/.well-known/agent-card.json` and advertises
three skills: `triage_water_report_email`, `extract_meter_reading`,
`persist_meter_readings`.

## Tests

```sh
uv run pytest
```

Unit tests run fully offline (stubbed LLM, fake Gmail, fake DB). Integration
tests run automatically when their backend is reachable and are skipped
otherwise: the LiteLLM gateway (real photo extraction in `tests/data/`,
triage normalization) and Postgres (schema + dedup round-trip).

## Layout

```
src/agents/meter/
  agent.py        MeterAgent (vision extraction), TriageAgent, A2A executor
  consensus.py    voter fan-out + arbiter escalation
  pipeline.py     inbox orchestration (dedup → triage → analyze → save → reply)
  repository.py   meter_readings DDL, email hash, check-then-insert
  replies.py      Norwegian reply templates
  models.py       MeterReading, TriageDecision, ConsensusResult, …
  prompts.py      extraction / triage / arbiter prompts
  skills.py       A2A agent card
src/utils/        settings (pydantic-settings + yaml), gmail client, sql, s3, llm
config/           config.yaml, residents.yaml
tests/            offline unit tests + auto-skipping integration tests
```

Reading rule encoded in the prompts: on Apator Ultrimis LCDs the digit-size
change does **not** mark the decimal point — trust a visible decimal point,
otherwise the last three digits of the m³ row are decimals
(`00002 9890` → 29.890 m³).

from a2a.types import AgentCapabilities, AgentCard, AgentSkill

from src.utils import settings

cfg = settings.agent_config_by_name("meter")

agent_card = AgentCard(
    name="Water Meter Reading Agent",
    description=(
        "A specialized agent that processes water-meter report emails for "
        "Mollebakken 36: triages inbound mail, reads the attached meter "
        "photos (Apator Ultrimis / Ultrimis NEO and similar LCD meters) "
        "with multi-model consensus, persists readings in Postgres, and "
        "replies to the resident in Norwegian."
    ),
    url=f"http://{cfg.host}:{cfg.port}",
    version=cfg.version,
    default_input_modes=["image/jpeg", "image/png", "application/json"],
    default_output_modes=["application/json"],
    capabilities=AgentCapabilities(streaming=True),
    skills=[
        AgentSkill(
            id="triage_water_report_email",
            name="Triage Water Report Email",
            description=(
                "Runs first on every inbound email: decides whether it is a "
                "water-meter reading report, requires attached images, and "
                "normalizes the apartment to L0-L9 (L0 = the building's main "
                "meter) from the subject line, falling back to the sender's "
                "email address."
            ),
            tags=["email", "triage", "classification", "apartment"],
            examples=[
                "Is this email a water-meter report, and for which apartment?",
                'Subject "Vannmåler L3" with two photos attached.',
            ],
        ),
        AgentSkill(
            id="extract_meter_reading",
            name="Extract Meter Reading",
            description=(
                "Extracts a structured reading from one or more attached "
                "water-meter photos: serial number (meter_nr), meter type "
                "(cold_water / warm_water), cumulative reading in m3, plus "
                "manufacturer, model, production year, and a confidence score."
            ),
            tags=["water-meter", "vision", "ocr", "utility", "reading"],
            examples=[
                "Read the attached water-meter photo.",
                "Is this the warm or cold water meter, and what does it read?",
                "Extract meter number and reading from these two photos.",
            ],
        ),
        AgentSkill(
            id="persist_meter_readings",
            name="Persist Meter Readings",
            description=(
                "Stores analyzed readings in the mollebakken Postgres "
                "database, one row per meter, keyed by an email hash "
                "(Date+From+Subject) and meter number so duplicate reports "
                "are never saved twice."
            ),
            tags=["postgres", "persistence", "dedup", "sql"],
            examples=[
                "Save these readings for apartment L3.",
                "Has this report already been stored?",
            ],
        ),
    ],
    supports_authenticated_extended_card=False,
)

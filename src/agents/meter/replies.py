"""Norwegian email replies — fixed templates, never LLM-generated."""

from __future__ import annotations

from src.agents.meter.models import MeterReading

_METER_TYPE_NO = {
    "cold_water": "Kaldtvann",
    "warm_water": "Varmtvann",
    "unknown": "Ukjent målertype",
}

_SIGNATURE = "\n\nMed vennlig hilsen\nVannmåleragenten, Møllebakken 36"

_REJECTION_BODIES = {
    "no_images": (
        "Hei,\n\n"
        "Vi mottok e-posten din, men fant ingen bilder av vannmåleren. "
        "Vennligst send på nytt med bilde(r) av måleren vedlagt."
    ),
    "apartment_unknown": (
        "Hei,\n\n"
        "Vi mottok vannmåleravlesningen din, men klarte ikke å utlede "
        "leilighetsnummeret. Vennligst send på nytt med leilighetsnummer "
        '(L1-L9) i emnefeltet, f.eks. "Vannmåler L3".'
    ),
    "not_water_report": (
        "Hei,\n\n"
        "Denne e-postkassen behandler kun vannmåleravlesninger for "
        "Møllebakken 36. E-posten din ble ikke gjenkjent som en avlesning "
        "og er derfor ikke behandlet."
    ),
    "unclear_images": (
        "Hei,\n\n"
        "Vi klarte ikke å lese vannmåleren sikkert fra bildene dine. "
        "Vennligst ta nye bilder — godt lys, hele displayet og strekkoden "
        "synlig — og send på nytt."
    ),
}


def confirmation_subject(original_subject: str) -> str:
    subject = original_subject.strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}"


def confirmation_body(apartment_nr: str, readings: list[MeterReading]) -> str:
    lines = "\n".join(
        f"- {_METER_TYPE_NO[r.meter_type]} (måler {r.meter_nr}): "
        f"{r.meter_reading:.3f} m3"
        for r in readings
    )
    return (
        f"Kjære {apartment_nr},\n\n"
        "Vi har lagret din vannmåleravlesning og analysert disse tallene:\n\n"
        f"{lines}"
        f"{_SIGNATURE}"
    )


def rejection_body(category: str) -> str:
    return _REJECTION_BODIES[category] + _SIGNATURE


def duplicate_body(apartment_nr: str, rows: list[dict]) -> str:
    """Reply for a re-sent report: already registered, with the stored values."""
    lines = "\n".join(
        f"- {_METER_TYPE_NO[row['meter_type']]} (måler {row['meter_nr']}): "
        f"{float(row['meter_reading']):.3f} m3"
        for row in rows
    )
    return (
        f"Kjære {apartment_nr},\n\n"
        "Denne avlesningen er allerede registrert hos oss, så e-posten din er "
        "ikke behandlet på nytt. Dette er verdiene vi har lagret:\n\n"
        f"{lines}\n\n"
        "Stemmer ikke dette, send en ny e-post med nye bilder."
        f"{_SIGNATURE}"
    )

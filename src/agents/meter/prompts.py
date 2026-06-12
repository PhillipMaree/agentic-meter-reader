TRIAGE_PROMPT = """\
You are triaging an email sent to a housing cooperative's water-meter inbox
(Mollebakken 36, apartments L1-L9). Decide whether it is most likely a
water-meter reading report from a resident.

Email:
- Subject: {subject}
- From: {sender}
- Attachments: {attachments}
- Body (may be empty):
{body}

Rules:

- is_water_report: true when the mail plausibly reports a water-meter reading
  (photos of meters attached, subject/body mentioning water, meters, readings,
  vannmaler, avlesing, or just an apartment designation with photos). Marketing,
  invoices, questions, and unrelated mail are false with
  rejection="not_water_report".
- apartment_nr: ONLY the values L0-L9 are valid. Infer it when the subject (or
  body) clearly identifies apartment number 1-9: "L3", "Leilighet 3",
  "Apartment 3", "Apt 3", or a bare "3" all mean L3. L0 is hovedmaleren - the
  building's main meter - so "L0", "hovedmaler" or "hovedmåler" mean L0.
  Street-address forms like "36C" do NOT identify an apartment - do not guess.
  If you cannot determine it, set apartment_nr to null and
  rejection="apartment_unknown" (keep is_water_report true if it otherwise
  looks like a report).
- You cannot see attachment contents, only their filenames; their presence has
  already been checked.
- reason: one short diagnostic sentence (internal, not shown to the resident).
"""

ARBITER_PROMPT_SUFFIX = """\

Several independent readers disagreed on this image:
{candidates}

Look at the image yourself, decide which values are correct (they may all be
wrong), and fill the same schema. In 'notes', state which voter(s) you sided
with and why.
"""

EXTRACTION_PROMPT = """\
You are reading a photo of a residential water meter (likely Apator Ultrimis /
Ultrimis NEO on a Powogaz 3KJ bracket).

Extract:

- meter_nr: the serial printed under/over the barcode, e.g. "8APA2080051414" —
  strip all spaces.
- meter_type: a RED ring/bezel around the face means warm_water; blue parts or a
  plain white face means cold_water. If neither is clear, use "unknown".
- meter_reading: the cumulative reading in m3. Read ALL digits of the main m3
  row left-to-right as one number — large whole digits first, then the smaller
  least-significant digits (boxed or after a gap). If the LCD shows an explicit
  decimal point or comma, trust its position (e.g. "00010.781" -> 10.781).
  Otherwise the digit-size change does NOT mark the decimal point: the LAST
  THREE digits are the decimals (0.001 m3 resolution), value = all_digits/1000,
  e.g. "00002 9890" -> 29.890. Do NOT confuse the l/h row with the m3 row.
- flow_lph: the smaller l/h display if visible, else null.
- manufacturer / model: e.g. "Apator" / "Ultrimis NEO ULN2,5-01-80-DN15".
- production_year: from "Production year" text or the M-marking
  (M25 -> 2025, M23 -> 2023).
- confidence: 0-1, your honesty about digit legibility.
- notes: anything ambiguous (glare, partial digits, occlusion).
"""

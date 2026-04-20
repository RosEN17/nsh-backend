import os, json
from typing import Optional
from openai import AsyncOpenAI

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY", ""))

ESTIMATE_SYSTEM = """Du är ByggKalk AI, expert på byggkalkylering för svenska hantverkare.

När du får en jobbeskrivning ska du:
1. Identifiera alla arbetsmoment (rivning, förberedelse, installation, efterarbete)
2. Beräkna materialåtgång med spillmarginal (10-15%)
3. Uppskatta arbetstid per moment
4. Använda realistiska svenska materialpriser 2025-2026
5. Beakta svenska byggnormer (BBR)

VIKTIGT:
- Realistiska priser för den svenska marknaden
- Inkludera alltid spillmarginal på material
- Separera material och arbete tydligt
- Ange enheter (kvm, löpmeter, styck, kg, timmar etc.)
- ROT-avdrag: 30% på arbetskostnad inkl moms, max 50 000 kr/person/år

Svara ENBART med JSON i detta format:
{
  "job_title": "Kort titel",
  "job_summary": "Sammanfattning",
  "categories": [
    {
      "name": "Kategorinamn",
      "rows": [
        {
          "id": "rad_1",
          "description": "Postbeskrivning",
          "unit": "kvm|st|löpm|timmar|kg|paket",
          "quantity": 10.0,
          "unit_price": 250.0,
          "total": 2500.0,
          "type": "material|labor|equipment",
          "note": "Valfri kommentar"
        }
      ],
      "subtotal": 2500.0
    }
  ],
  "totals": {
    "material_total": 0,
    "labor_total": 0,
    "equipment_total": 0,
    "subtotal": 0,
    "margin_amount": 0,
    "total_ex_vat": 0,
    "vat": 0,
    "total_inc_vat": 0,
    "rot_deduction": 0,
    "customer_pays": 0
  },
  "estimated_days": 5,
  "warnings": ["Varningar"],
  "assumptions": ["Antaganden"]
}"""

CHAT_SYSTEM = """Du är ByggKalk AI, hjälpsam assistent för svenska hantverkare.
Svara koncist och praktiskt. Använd svenska byggtermer."""


async def generate_estimate(
    description: str,
    job_type: Optional[str],
    area_sqm: Optional[float],
    location: Optional[str],
    hourly_rate: float = 650,
    include_rot: bool = True,
    margin_pct: float = 15,
) -> dict:

    user_prompt = f"""Skapa en detaljerad kalkyl:

Beskrivning: {description}
{f"Jobbtyp: {job_type}" if job_type else ""}
{f"Yta: {area_sqm} kvm" if area_sqm else ""}
{f"Plats: {location}" if location else ""}
Timpris: {hourly_rate} kr/h
Påslag: {margin_pct}%
ROT-avdrag: {"Ja, beräkna" if include_rot else "Nej"}

Ge en realistisk och detaljerad kalkyl. Svara ENBART med JSON."""

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": ESTIMATE_SYSTEM},
            {"role": "user", "content": user_prompt},
        ],
        temperature=0.3,
        max_tokens=4000,
        response_format={"type": "json_object"},
    )

    estimate = json.loads(response.choices[0].message.content)
    estimate["meta"] = {
        "hourly_rate": hourly_rate,
        "margin_pct": margin_pct,
        "include_rot": include_rot,
        "location": location,
        "area_sqm": area_sqm,
        "job_type": job_type,
    }
    return estimate


async def chat_about_estimate(message: str, estimate_context: Optional[dict] = None) -> str:
    messages = [{"role": "system", "content": CHAT_SYSTEM}]
    if estimate_context:
        messages.append({"role": "system", "content": f"Aktuell kalkyl:\n{json.dumps(estimate_context, ensure_ascii=False)}"})
    messages.append({"role": "user", "content": message})

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.5,
        max_tokens=1000,
    )
    return response.choices[0].message.content

"""
NordSheet AI — Kalkylgenerering med GPT-4o
Stöder byggparametrar och bilder (GPT-4o vision)
"""

import json
import os
from typing import Optional, Dict, List
from openai import AsyncOpenAI

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SYSTEM_PROMPT = """Du är en erfaren svensk byggkalkylator-AI. Du genererar detaljerade kostnadskalkyler för hantverksjobb i Sverige.

REGLER:
- Alla priser i SEK
- Använd realistiska svenska materialpriser (2025-2026)
- Arbete räknas i timmar x timpriset som anges
- Inkludera alltid rivning/demontering om det är en renovering
- Inkludera förberedelse (tätskikt, primning etc.)
- Inkludera efterarbete (städning, slutbesiktning)
- Varje rad ska ha: description, note (kort förklaring), unit (timmar/kvm/st/meter/kg), quantity, unit_price, total, type (labor/material/equipment)
- Gruppera i kategorier (Rivning, Förberedelse, Installation, Material, Efterarbete etc.)
- Varje kategori har: name, rows[], subtotal

VIKTIGT OM BYGGPARAMETRAR:
- Om takhöjd anges: beräkna väggyta = omkrets x takhöjd. I badrum ska väggar kaklas om inget annat anges.
- Om byggår anges: äldre byggnader (pre-1975) kan ha asbest - notera varning. Äldre rör/el kan behöva bytas.
- Om antal rum anges: multiplicera arbete och material därefter.
- Om våningar anges: tillgänglighet påverkar transport/ställning.

VIKTIGT OM BILDER:
- Om bilder bifogas: analysera dem för att förstå nuvarande skick, material, storlek och eventuella problem.
- Nämn i job_summary vad du ser i bilderna.

SVARA ALLTID med exakt denna JSON-struktur (inget annat):
{
  "job_title": "Kort titel",
  "job_summary": "Sammanfattning av jobbet",
  "estimated_days": 5,
  "categories": [
    {
      "name": "Kategorinamn",
      "rows": [
        {"description": "Beskrivning", "note": "Kort not", "unit": "timmar", "quantity": 10, "unit_price": 650, "total": 6500, "type": "labor"}
      ],
      "subtotal": 6500
    }
  ],
  "totals": {
    "material_total": 0,
    "labor_total": 0,
    "equipment_total": 0,
    "subtotal_ex_vat": 0,
    "margin_amount": 0,
    "total_ex_vat": 0,
    "vat": 0,
    "total_inc_vat": 0,
    "rot_deduction": 0,
    "customer_pays": 0
  },
  "meta": {
    "hourly_rate": 650,
    "margin_pct": 15,
    "area_sqm": 8,
    "rot_applied": true
  },
  "warnings": ["Eventuella varningar"]
}"""


async def generate_estimate(
    description: str,
    job_type: Optional[str] = None,
    area_sqm: Optional[float] = None,
    location: Optional[str] = None,
    hourly_rate: float = 650,
    include_rot: bool = True,
    margin_pct: float = 15,
    build_params: Optional[Dict[str, str]] = None,
    images: Optional[List] = None,
    documents: Optional[List] = None,
) -> dict:

    user_parts = []
    user_parts.append(f"Jobb: {description}")
    if job_type:
        user_parts.append(f"Jobbtyp: {job_type}")
    if area_sqm:
        user_parts.append(f"Yta: {area_sqm} kvm")
    if location:
        user_parts.append(f"Plats: {location}")
    user_parts.append(f"Timpris: {hourly_rate} kr/h")
    user_parts.append(f"Paslag: {margin_pct}%")
    user_parts.append(f"ROT-avdrag: {'Ja (30% pa arbete)' if include_rot else 'Nej'}")

    if build_params:
        param_lines = []
        if build_params.get("ceiling_height"):
            param_lines.append(f"Takhojd: {build_params['ceiling_height']}")
        if build_params.get("build_year"):
            param_lines.append(f"Byggar: {build_params['build_year']}")
        if build_params.get("num_rooms"):
            param_lines.append(f"Antal rum: {build_params['num_rooms']}")
        if build_params.get("floors"):
            param_lines.append(f"Vaningar: {build_params['floors']}")
        if build_params.get("extra"):
            param_lines.append(f"Ovrigt: {build_params['extra']}")
        if param_lines:
            user_parts.append("\nByggparametrar:\n" + "\n".join(param_lines))

    if documents:
        doc_names = [d.name if hasattr(d, 'name') else d.get('name', 'okant') for d in documents]
        user_parts.append(f"\nBifogade dokument: {', '.join(doc_names)}")

    user_text = "\n".join(user_parts)

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if images and len(images) > 0:
        content_parts = [{"type": "text", "text": user_text}]
        for img in images[:5]:
            img_data = img.data if hasattr(img, 'data') else img.get('data', '')
            if img_data:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": img_data, "detail": "low"}
                })
        messages.append({"role": "user", "content": content_parts})
    else:
        messages.append({"role": "user", "content": user_text})

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.3,
        max_tokens=4000,
        response_format={"type": "json_object"},
    )

    raw = response.choices[0].message.content or "{}"
    data = json.loads(raw)
    return data


async def chat_about_estimate(message: str, context: Optional[dict] = None) -> str:
    system = "Du ar en hjalpsam svensk byggkalkylator-assistent. Svara kort och konkret pa svenska."
    messages = [{"role": "system", "content": system}]
    if context:
        messages.append({"role": "user", "content": f"Kalkylkontext: {json.dumps(context, ensure_ascii=False)}"})
        messages.append({"role": "assistant", "content": "Jag har sett kalkylen. Vad undrar du?"})
    messages.append({"role": "user", "content": message})

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.5,
        max_tokens=1000,
    )
    return response.choices[0].message.content or "Jag kunde inte svara."

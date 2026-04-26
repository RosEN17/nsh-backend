"""
NordSheet AI — Kalkylgenerering med GPT-4o
Stöder byggparametrar, bilder (vision) och PDF-underlag (base64 → text)
"""

import json
import os
import base64
import io
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
- Använd ALLA parametrar som anges — mått, checkboxar, jobbtyp — för att göra kalkylen exakt
- Om takhöjd anges: beräkna väggyta = (2 × (bredd + längd)) × takhöjd
- Om golvyta anges: beräkna material med 10-12% spill
- Om antal kaklade väggar och kakelhöjd anges: beräkna exakt kakelyta
- Om "ingår i jobbet" anger specifika moment: inkludera dessa som egna rader i kalkylen
- Om plats anges: justera priser (Stockholm +12%, Göteborg +6%, övriga Sverige 0%)
- Om byggår/hustyp anges: äldre byggnader (pre-1975) kan ha asbest — lägg till varning och saneringspost

VIKTIGT OM BILDER:
- Om projektbilder bifogas: analysera dem för att förstå nuvarande skick, material, storlek och problem
- Nämn i job_summary vad du ser i bilderna som påverkar kalkylen
- Om skador eller avvikelser syns: lägg till extra poster och/eller varningar

VIKTIGT OM PDF-UNDERLAG OCH RITNINGAR:
- Om PDF-innehåll bifogas: läs noggrant igenom det — det kan vara ett mail med kundens krav, en offertförfrågan, en ritning eller en specifikation
- Extrahera all relevant information: mått, materialval, specifika krav, tidsramar, adress
- Prioritera information från PDF:en framför generella antaganden
- Om ritningar bifogas som bilder: läs av mått och rumsindelning

SVARA ALLTID med exakt denna JSON-struktur (inget annat):
{
  "job_title": "Kort titel",
  "job_summary": "Sammanfattning av jobbet inkl. vad du sett i underlag/bilder",
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
  "warnings": ["Eventuella varningar"],
  "assumptions": ["Antaganden som gjorts"]
}"""


def _extract_pdf_text(b64_data: str) -> str:
    """Extrahera text ur base64-kodad PDF med pypdf."""
    try:
        import pypdf  # type: ignore
        raw = b64_data.split(",")[-1]
        pdf_bytes = base64.b64decode(raw + "==")
        reader = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())
        return "\n\n".join(pages)
    except ImportError:
        return ""
    except Exception:
        return ""


def _build_user_text(
    description: str,
    job_type: Optional[str],
    area_sqm: Optional[float],
    location: Optional[str],
    hourly_rate: float,
    include_rot: bool,
    margin_pct: float,
    build_params: Optional[Dict[str, str]],
    documents: Optional[List],
) -> str:
    parts = []

    parts.append(f"Jobbeskrivning: {description}")
    if job_type:
        parts.append(f"Jobbtyp: {job_type}")
    if area_sqm:
        parts.append(f"Yta: {area_sqm} kvm")
    if location:
        parts.append(f"Plats: {location}")
    parts.append(f"Timpris: {hourly_rate} kr/h")
    parts.append(f"Påslag: {margin_pct}%")
    parts.append(f"ROT-avdrag: {'Ja (30% på arbete)' if include_rot else 'Nej'}")

    # Alla byggparametrar med svenska etiketter
    if build_params:
        LABELS = {
            "floor_sqm":      "Golvyta",
            "ceiling_height": "Takhöjd",
            "tiled_walls":    "Antal kaklade väggar",
            "tile_height":    "Kakelhöjd på vägg",
            "openings":       "Dörrar & fönster (st)",
            "kitchen_width":  "Kökets bredd",
            "base_cabinets":  "Antal basskåp",
            "wall_cabinets":  "Antal hängskåp",
            "countertop_len": "Bänkskivans längd",
            "roof_area":      "Takarea",
            "roof_pitch":     "Taklutning",
            "roof_type":      "Takets form",
            "perimeter":      "Husomkrets",
            "facade_height":  "Fasadhöjd",
            "windows":        "Antal fönster",
            "doors":          "Antal dörrar",
            "room_width":     "Rumsbredd",
            "floor_type":     "Golvtyp",
            "wall_sqm":       "Väggyta att måla",
            "rooms":          "Antal rum",
            "outlets":        "Antal uttag/brytare",
            "cable_meters":   "Kabelledning",
            "fixtures":       "Antal armaturer",
            "taps":           "Antal blandare",
            "pipe_meters":    "Ny rörledning",
            "drains":         "Antal avlopp",
            "addition_sqm":   "Tillbyggnadsarea",
            "area_sqm":       "Yta/area",
            "units":          "Antal enheter",
            "ingår_i_jobbet": "Ingår i jobbet",
            "jobbtyp":        "Jobbtyp (bekräftad)",
            "location":       "Plats",
            "build_year":     "Byggår",
            "num_rooms":      "Antal rum",
            "floors":         "Våningar",
            "extra":          "Övrigt",
        }
        lines = []
        for key, value in build_params.items():
            if value:
                lines.append(f"  {LABELS.get(key, key)}: {value}")
        if lines:
            parts.append("\nSmarta parametrar:\n" + "\n".join(lines))

    # PDF-underlag — extrahera och bifoga text
    if documents:
        doc_blocks = []
        for doc in documents:
            name = doc.name if hasattr(doc, "name") else doc.get("name", "okänt")
            data = doc.data if hasattr(doc, "data") else doc.get("data", "")
            extracted = _extract_pdf_text(data) if data else ""
            if extracted:
                doc_blocks.append(
                    f"--- PDF-UNDERLAG: {name} ---\n{extracted[:6000]}\n--- SLUT PDF ---"
                )
            else:
                doc_blocks.append(f"[Bifogad fil: {name}]")
        parts.append(
            "\nBifogade underlag (läs och basera kalkylen på innehållet):\n"
            + "\n\n".join(doc_blocks)
        )

    return "\n".join(parts)


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

    user_text = _build_user_text(
        description=description,
        job_type=job_type,
        area_sqm=area_sqm,
        location=location,
        hourly_rate=hourly_rate,
        include_rot=include_rot,
        margin_pct=margin_pct,
        build_params=build_params,
        documents=documents,
    )

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    all_images = list(images or [])

    if all_images:
        content_parts: List[dict] = [{"type": "text", "text": user_text}]
        for img in all_images[:8]:
            data = img.data if hasattr(img, "data") else img.get("data", "")
            if data:
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": data, "detail": "low"},
                })
        names = [
            (img.name if hasattr(img, "name") else img.get("name", "bild"))
            for img in all_images[:8]
        ]
        content_parts[0]["text"] += (
            f"\n\nBifogade bilder ({len(names)} st): {', '.join(names)}"
        )
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
    return json.loads(raw)


async def chat_about_estimate(message: str, context: Optional[dict] = None) -> str:
    system = "Du är en hjälpsam svensk byggkalkylator-assistent. Svara kort och konkret på svenska."
    msgs = [{"role": "system", "content": system}]
    if context:
        msgs.append({"role": "user", "content": f"Kalkylkontext: {json.dumps(context, ensure_ascii=False)}"})
        msgs.append({"role": "assistant", "content": "Jag har sett kalkylen. Vad undrar du?"})
    msgs.append({"role": "user", "content": message})

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=msgs,
        temperature=0.5,
        max_tokens=1000,
    )
    return response.choices[0].message.content or "Jag kunde inte svara."

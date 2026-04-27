"""
NordSheet AI — Kalkylgenerering med GPT-4o
Hämtar arbetstidsnormer + historiska offerter från Supabase.
Historiska offerter injiceras som few-shot-exempel i prompten.
"""

import json
import os
import base64
import io
from typing import Optional, Dict, List
from openai import AsyncOpenAI
import httpx

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

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

KRITISKT — ARBETSTIDSNORMER:
- Normdatabasen nedan anger exakt hur många timmar varje moment tar per enhet
- Du MÅSTE räkna timmar från normdatabasen — aldrig från magkänsla eller generella antaganden
- Multiplikation: norm (h/enhet) × antal enheter = timmar för momentet
- Avrunda alltid uppåt till närmaste halvtimme
- Om ett moment saknas i normdatabasen: använd närmaste liknande norm och notera det

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
- Om PDF-innehåll bifogas: läs noggrant igenom det — det kan vara ett mail med kundens krav, en offertförfrågan eller en specifikation
- Extrahera all relevant information: mått, materialval, specifika krav, tidsramar, adress
- Prioritera information från PDF:en framför generella antaganden

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


async def fetch_norms(job_type: str, house_age: str = "all") -> str:
    """Hämta arbetstidsnormer från Supabase och returnera som prompttext."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return ""

    try:
        type_map = {
            "badrum": "badrum", "bathroom": "badrum",
            "kok": "kok", "kök": "kok", "kitchen": "kok",
            "tak": "tak", "roof": "tak",
            "fasad": "fasad", "facade": "fasad",
            "golv": "golv", "floor": "golv",
            "malning": "malning", "målning": "malning", "painting": "malning",
        }
        db_type = type_map.get(job_type.lower(), job_type.lower())

        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.get(
                f"{SUPABASE_URL}/rest/v1/work_norms",
                params={
                    "job_type": f"eq.{db_type}",
                    "select": "label,hours_per,unit,house_age,region",
                    "order": "moment",
                },
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                },
            )

        if r.status_code != 200:
            return ""

        norms = r.json()
        if not norms:
            return ""

        relevant = [
            n for n in norms
            if n["house_age"] == "all" or n["house_age"] == house_age
        ]

        if house_age != "all":
            seen = {}
            for n in relevant:
                key = n["label"]
                if key not in seen or n["house_age"] == house_age:
                    seen[key] = n
            relevant = list(seen.values())

        lines = [
            f"\nARBETSTIDSNORMER FÖR {db_type.upper()} "
            f"(MÅSTE ANVÄNDAS — RÄKNA ALDRIG UTAN DESSA):"
        ]
        for n in relevant:
            lines.append(f"  {n['label']}: {n['hours_per']} timmar per {n['unit']}")

        lines.append(
            "\nBEREKNING: norm × antal enheter = timmar. "
            "Avrunda uppåt till närmaste 0.5 timme."
        )
        return "\n".join(lines)

    except Exception:
        return ""


async def fetch_few_shot_examples(job_type: str, complexity: Optional[str] = None, region: Optional[str] = None) -> str:
    """
    Hämtar 3 vinnande historiska offerter från quotes-tabellen.
    Dessa injiceras i prompten som few-shot-exempel så att AI:n
    lär sig verkliga prisnivåer, arbetsmoment och faktorer.

    Urvalsordning:
      1. Matchar job_type + complexity + region  (bästa match)
      2. Matchar job_type + complexity
      3. Matchar job_type
    Alltid outcome = 'won' för att bara lära av vinnande offerter.
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return ""

    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }

    # Normalisera job_type mot vad som finns i quotes-tabellen
    type_map = {
        "kok": "kok", "kök": "kok", "kitchen": "kok",
        "badrum": "badrum", "bathroom": "badrum",
        "golv": "golv", "floor": "golv",
        "malning": "malning", "målning": "malning",
        "tak": "tak", "fasad": "fasad",
        "tillbyggnad": "tillbyggnad", "vvs": "vvs", "el": "el",
    }
    db_type = type_map.get((job_type or "").lower(), (job_type or "").lower())

    # Bygg parametrar — börja med striktaste match och lossa om för få träffar
    base_params = {
        "project_type": f"eq.{db_type}",
        "outcome": "eq.won",
        "select": "quote_number,project_type,complexity,region,labor_cost,material_cost,total_incl_vat,rot_deduction,customer_net_cost,waste_factor,risk_factor,tile_price_per_sqm,work_items,material_items,craftsman_edits,notes",
        "limit": "3",
        "order": "quote_date.desc",
    }

    if complexity:
        base_params["complexity"] = f"eq.{complexity}"
    if region:
        base_params["region"] = f"eq.{region}"

    examples = []

    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.get(
                f"{SUPABASE_URL}/rest/v1/quotes",
                params=base_params,
                headers=headers,
            )

            if r.status_code == 200:
                examples = r.json()

            # Om för få — prova utan region
            if len(examples) < 2 and region:
                params2 = {k: v for k, v in base_params.items() if k != "region"}
                r2 = await http.get(
                    f"{SUPABASE_URL}/rest/v1/quotes",
                    params=params2,
                    headers=headers,
                )
                if r2.status_code == 200:
                    examples = r2.json()

            # Om fortfarande för få — prova utan complexity heller
            if len(examples) < 1 and complexity:
                params3 = {k: v for k, v in base_params.items() if k not in ("region", "complexity")}
                r3 = await http.get(
                    f"{SUPABASE_URL}/rest/v1/quotes",
                    params=params3,
                    headers=headers,
                )
                if r3.status_code == 200:
                    examples = r3.json()

    except Exception:
        return ""

    if not examples:
        return ""

    # Formatera exemplen som läsbar text för prompten
    lines = [
        "\n\nHISTORISKA OFFERTER SOM VANN AFFÄREN (FEW-SHOT EXEMPEL):",
        "Använd dessa som referens för prisnivåer, arbetsmoment och faktorer.",
        "Dessa är verkliga offerter som kunden accepterade.\n",
    ]

    for i, ex in enumerate(examples, 1):
        lines.append(f"--- EXEMPEL {i}: {ex.get('project_type','').upper()} ({ex.get('complexity','')}) ---")
        lines.append(f"Region: {ex.get('region', 'ej angiven')}")
        lines.append(f"Arbetskostnad (exkl. moms): {ex.get('labor_cost', 0):,.0f} kr")
        lines.append(f"Materialkostnad (exkl. moms): {ex.get('material_cost', 0):,.0f} kr")
        lines.append(f"Totalt inkl. moms: {ex.get('total_incl_vat', 0):,.0f} kr")
        lines.append(f"ROT-avdrag: {ex.get('rot_deduction', 0):,.0f} kr")
        lines.append(f"Kunden betalade netto: {ex.get('customer_net_cost', 0):,.0f} kr")

        if ex.get('waste_factor'):
            lines.append(f"Svinnfaktor: {float(ex['waste_factor'])*100:.0f}%")
        if ex.get('risk_factor'):
            lines.append(f"Riskpåslag: {float(ex['risk_factor'])*100:.0f}%")
        if ex.get('tile_price_per_sqm'):
            lines.append(f"Kakel/klinker: {ex['tile_price_per_sqm']} kr/kvm inkl. moms")

        work_items = ex.get('work_items') or []
        if work_items:
            lines.append(f"Arbetsmoment ({len(work_items)} st): {', '.join(work_items[:8])}")
            if len(work_items) > 8:
                lines.append(f"  ... och {len(work_items) - 8} till")

        # Om snickaren justerade AI-förslaget — visa vad och varför
        edits = ex.get('craftsman_edits')
        if edits:
            lines.append("Justeringar snickaren gjorde vs AI-förslag:")
            if isinstance(edits, dict):
                for field, edit in edits.items():
                    if isinstance(edit, dict):
                        ai_val = edit.get('ai', '?')
                        final_val = edit.get('final', '?')
                        reason = edit.get('reason', '')
                        lines.append(f"  {field}: AI föreslog {ai_val} → snickaren satte {final_val}. Anledning: {reason}")

        if ex.get('notes'):
            lines.append(f"Not: {ex['notes'][:200]}")

        lines.append("")

    lines.append(
        "INSTRUKTION: Låt dessa exempel guida dina prisnivåer. "
        "Om ditt jobb liknar Exempel 1 men är mer komplext — motivera avvikelsen i assumptions."
    )

    return "\n".join(lines)


def _extract_pdf_text(b64_data: str) -> str:
    """Extrahera text ur base64-kodad PDF med pypdf."""
    try:
        import pypdf
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


def _detect_house_age(build_params: Optional[Dict[str, str]]) -> str:
    if not build_params:
        return "all"
    year_str = build_params.get("build_year", "")
    if not year_str:
        return "all"
    try:
        year = int(str(year_str).replace("ca", "").strip()[:4])
        return "pre1975" if year < 1975 else "post1975"
    except (ValueError, TypeError):
        return "all"


def _detect_complexity(build_params: Optional[Dict[str, str]], description: str) -> Optional[str]:
    """
    Gissa komplexitet från parametrar och beskrivning
    för att hitta bättre few-shot-matchning.
    """
    if not build_params and not description:
        return None

    desc_lower = (description or "").lower()
    params = build_params or {}

    # Specialist-signaler
    specialist_keywords = ["ny vägg", "bärande", "bygglov", "asbest", "flytt av vvb",
                           "flytt tvättmaskin", "ombyggnad", "tillbyggnad"]
    if any(kw in desc_lower for kw in specialist_keywords):
        return "specialist"

    # High-signaler
    high_keywords = ["brunnsflytt", "flytt av brunn", "nytt avlopp", "el framdragning",
                     "framdragning el", "golvvärme", "fuktskada"]
    if any(kw in desc_lower for kw in high_keywords):
        return "high"

    # Low-signaler
    low_keywords = ["kakel", "måla", "tätskikt", "byte av", "enkel"]
    if any(kw in desc_lower for kw in low_keywords):
        return "low"

    return "medium"


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

    house_age = _detect_house_age(build_params)
    complexity = _detect_complexity(build_params, description)

    # Hämta normer och few-shot-exempel parallellt
    import asyncio
    norms_text, few_shot_text = await asyncio.gather(
        fetch_norms(job_type or "badrum", house_age),
        fetch_few_shot_examples(
            job_type or "badrum",
            complexity=complexity,
            region=location,
        ),
    )

    # Bygg system-prompt med normer + historiska exempel injicerade
    system = SYSTEM_PROMPT
    if norms_text:
        system += f"\n\n{norms_text}"
    if few_shot_text:
        system += f"\n\n{few_shot_text}"

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

    messages = [{"role": "system", "content": system}]

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

"""
NordSheet AI — Kalkylgenerering med GPT-4o
Hämtar arbetstidsnormer från Supabase och injicerar i system-prompten.
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

VIKTIGT OM BILDER — RITNINGAR OCH ANTECKNINGAR:
- Om PNG-bilder bifogas: behandla dem som ritningar eller handskrivna anteckningar
- Läs av ALL text, mått, siffror och dimensioner i bilden noggrant
- Mått i ritningar (t.ex. "2400", "3600") är i millimeter om inget annat anges
- Extrahera: rumsbredd, rumslängd, takhöjd, fönster- och dörrmått om de syns
- Nämn i job_summary exakt vilka mått du läst av från ritningen
- Om handskrivna anteckningar: läs av allt som är relevant för jobbet
- Om JPEG-bilder (foton): analysera nuvarande skick, material och eventuella skador

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
      "name": "Rivning",
      "rows": [
        {"description": "Rivning kakel vägg", "note": "18 kvm × 0.8 h/kvm", "unit": "timmar", "quantity": 14.5, "unit_price": 850, "total": 12325, "type": "labor"},
        {"description": "Kakelfix Weber Flex 25kg", "note": "Material för kakelsättning", "unit": "säck", "quantity": 3, "unit_price": 280, "total": 840, "type": "material"}
      ],
      "subtotal": 13165
    }
  ],
  "totals": {
    "material_total": 840,
    "labor_total": 12325,
    "equipment_total": 0,
    "subtotal_ex_vat": 13165,
    "margin_amount": 1975,
    "total_ex_vat": 15140,
    "vat": 3785,
    "total_inc_vat": 18925,
    "rot_deduction": 3698,
    "customer_pays": 15227
  },
  "meta": {
    "hourly_rate": 850,
    "margin_pct": 15,
    "area_sqm": 8,
    "rot_applied": true
  },
  "warnings": ["Eventuella varningar"],
  "assumptions": ["Antaganden som gjorts"]
}

KRITISKT OM TOTALS:
- material_total = summan av ALLA rader med type="material"
- labor_total = summan av ALLA rader med type="labor"
- subtotal_ex_vat = material_total + labor_total + equipment_total
- margin_amount = subtotal_ex_vat × (margin_pct / 100)
- total_ex_vat = subtotal_ex_vat + margin_amount
- vat = total_ex_vat × 0.25
- total_inc_vat = total_ex_vat + vat
- rot_deduction = labor_total × 0.30 (om rot_applied = true, annars 0)
- customer_pays = total_inc_vat - rot_deduction
- Skriv ALDRIG 0 i material_total om det finns materialrader — räkna ihop dem

MATERIAL SKA ALLTID INKLUDERAS:
- Även om kunden tillhandahåller kakel: lägg alltid in container, tätskikt, kakelfix, fog, silikon, VVS-material, el-material
- Varje kategori som har arbete ska också ha tillhörande materialrader
- type="material" för alla materialrader, type="labor" för arbete"""


async def fetch_norms(job_type: str, house_age: str = "all") -> str:
    """Hämta arbetstidsnormer från Supabase och returnera som prompttext."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return ""

    try:
        # Normalisera jobbtyp — matcha mot tabellens job_type-värden
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

        # Filtrera på husålder — ta alltid "all", plus specifik ålder om angiven
        relevant = [
            n for n in norms
            if n["house_age"] == "all" or n["house_age"] == house_age
        ]

        # Om äldre hus finns mer specifik norm — ta den istället för "all"
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
        # Om Supabase inte svarar — fortsätt utan normer
        return ""


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
    if area_sqm is not None:
        parts.append(f"Yta: {area_sqm} kvm")
    if location:
        parts.append(f"Plats: {location}")
    parts.append(f"Timpris: {hourly_rate or 650} kr/h")
    parts.append(f"Påslag: {margin_pct or 15}%")
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
    """Avgör husålder från build_params för att välja rätt norm."""
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


def recalculate_totals(data: dict, hourly_rate: float, margin_pct: float, include_rot: bool) -> dict:
    """
    Räknar alltid om totals deterministiskt från raderna.
    Litar aldrig på att AI:n räknat rätt — detta är källan till sanning.
    """
    material_total   = 0.0
    labor_total      = 0.0
    equipment_total  = 0.0

    for cat in data.get("categories", []):
        cat_subtotal = 0.0
        for row in cat.get("rows", []):
            # Räkna om radsumman deterministiskt
            qty   = float(row.get("quantity", 0) or 0)
            price = float(row.get("unit_price", 0) or 0)
            total = round(qty * price)
            row["total"] = total
            cat_subtotal += total

            t = row.get("type", "labor")
            if t == "material":
                material_total  += total
            elif t == "equipment":
                equipment_total += total
            else:
                labor_total += total

        cat["subtotal"] = round(cat_subtotal)

    margin_pct_val   = float(margin_pct or 15)
    subtotal         = material_total + labor_total + equipment_total
    margin_amount    = round(subtotal * margin_pct_val / 100)
    total_ex_vat     = round(subtotal + margin_amount)
    vat              = round(total_ex_vat * 0.25)
    total_inc_vat    = round(total_ex_vat + vat)
    rot_deduction    = round(labor_total * 0.30) if include_rot else 0
    customer_pays    = round(total_inc_vat - rot_deduction)

    data["totals"] = {
        "material_total":   round(material_total),
        "labor_total":      round(labor_total),
        "equipment_total":  round(equipment_total),
        "subtotal_ex_vat":  total_ex_vat,
        "margin_amount":    margin_amount,
        "total_ex_vat":     total_ex_vat,
        "vat":              vat,
        "total_inc_vat":    total_inc_vat,
        "rot_deduction":    rot_deduction,
        "customer_pays":    customer_pays,
    }
    data["meta"] = {
        **data.get("meta", {}),
        "hourly_rate": float(hourly_rate or 650),
        "margin_pct":  margin_pct_val,
        "rot_applied": include_rot,
    }
    return data


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
    company_id: Optional[str] = None,  # mottages men används ej i AI-anropet ännu
    **kwargs,  # framtidssäkert — ignorerar okända argument
) -> dict:

    # Hämta normer från Supabase
    house_age = _detect_house_age(build_params)
    norms_text = await fetch_norms(job_type or "badrum", house_age)

    # Bygg system-prompt med normer injicerade
    system = SYSTEM_PROMPT
    if norms_text:
        system += f"\n\n{norms_text}"

    user_text = _build_user_text(
        description=description,
        job_type=job_type,
        area_sqm=area_sqm,
        location=location,
        hourly_rate=hourly_rate or 650,
        include_rot=include_rot,
        margin_pct=margin_pct or 15,
        build_params=build_params,
        documents=documents,
    )

    messages = [{"role": "system", "content": system}]

    all_images = list(images or [])

    if all_images:
        content_parts: List[dict] = [{"type": "text", "text": user_text}]
        for img in all_images[:8]:
            data = img.data if hasattr(img, "data") else img.get("data", "")
            name = img.name if hasattr(img, "name") else img.get("name", "")
            if data:
                # Frontend prefixar ritningar med "[RITNING]" — använd high detail
                # Projektfoton — low detail räcker
                is_drawing = name.startswith("[RITNING]")
                detail = "high" if is_drawing else "low"
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": data, "detail": detail},
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

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.3,
            max_tokens=4000,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content or "{}"
        data = json.loads(raw)

        # Räkna alltid om totals deterministiskt från raderna
        # — litar aldrig på AI:ns egna summor
        data = recalculate_totals(
            data,
            hourly_rate=hourly_rate or 650,
            margin_pct=margin_pct or 15,
            include_rot=include_rot,
        )
        return data

    except json.JSONDecodeError as e:
        raise ValueError(f"AI returnerade ogiltig JSON: {e}")
    except Exception as e:
        raise ValueError(f"OpenAI-anrop misslyckades: {e}")


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

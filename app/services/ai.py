"""
NordSheet AI — Kalkylgenerering med GPT-4o
Hämtar från tre Supabase-tabeller:
  1. work_norms       — arbetstidsnormer per moment
  2. quotes           — historiska vinnande offerter (few-shot-exempel)
  3. feedback_events  — snickarjusteringar (lär AI:n vad den systematiskt missar)
"""

import json
import os
import base64
import io
from typing import Optional, Dict, List
from openai import AsyncOpenAI
import httpx

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
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
- Du MÅSTE räkna timmar från normdatabasen — aldrig från magkänsla
- Multiplikation: norm (h/enhet) × antal enheter = timmar för momentet
- Avrunda alltid uppåt till närmaste halvtimme
- Om ett moment saknas i normdatabasen: använd närmaste liknande norm och notera det

VIKTIGT OM BYGGPARAMETRAR:
- Använd ALLA parametrar som anges för att göra kalkylen exakt
- Om takhöjd anges: beräkna väggyta = (2 × (bredd + längd)) × takhöjd
- Om golvyta anges: beräkna material med 10-12% spill
- Om plats anges: justera priser (Stockholm +12%, Göteborg +6%, övriga Sverige 0%)
- Om byggår anges: äldre byggnader (pre-1975) kan ha asbest — lägg till varning

VIKTIGT OM BILDER OCH PDF:
- Om projektbilder bifogas: analysera dem för nuvarande skick, material, storlek
- Om PDF bifogas: extrahera all relevant information (mått, krav, material)
- Prioritera information från underlag framför generella antaganden

SVARA ALLTID med exakt denna JSON-struktur (inget annat):
{
  "job_title": "Kort titel",
  "job_summary": "Sammanfattning",
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


# ─────────────────────────────────────────────────────────────────────────────
# 1. Arbetstidsnormer
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_norms(job_type: str, house_age: str = "all") -> str:
    """Hämtar arbetstidsnormer från work_norms-tabellen."""
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
                    "select":   "label,hours_per,unit,house_age,region",
                    "order":    "moment",
                },
                headers={
                    "apikey":        SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                },
            )
        if r.status_code != 200 or not r.json():
            return ""

        norms = r.json()
        relevant = [n for n in norms if n["house_age"] == "all" or n["house_age"] == house_age]

        if house_age != "all":
            seen = {}
            for n in relevant:
                key = n["label"]
                if key not in seen or n["house_age"] == house_age:
                    seen[key] = n
            relevant = list(seen.values())

        lines = [f"\nARBETSTIDSNORMER FÖR {db_type.upper()} (MÅSTE ANVÄNDAS):"]
        for n in relevant:
            lines.append(f"  {n['label']}: {n['hours_per']} timmar per {n['unit']}")
        lines.append("\nBEREKNING: norm × antal enheter = timmar. Avrunda uppåt till närmaste 0.5 timme.")
        return "\n".join(lines)
    except Exception:
        return ""


# ─────────────────────────────────────────────────────────────────────────────
# 2. Few-shot-exempel från historiska offerter
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_few_shot_examples(
    job_type: str,
    complexity: Optional[str] = None,
    region: Optional[str] = None,
) -> str:
    """Hämtar vinnande historiska offerter som few-shot-exempel."""
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return ""

    headers = {
        "apikey":        SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    }
    type_map = {
        "kok": "kok", "kök": "kok", "kitchen": "kok",
        "badrum": "badrum", "bathroom": "badrum",
        "golv": "golv", "floor": "golv",
        "malning": "malning", "målning": "malning",
        "tak": "tak", "fasad": "fasad",
        "tillbyggnad": "tillbyggnad", "vvs": "vvs", "el": "el",
    }
    db_type = type_map.get((job_type or "").lower(), (job_type or "").lower())

    base_params = {
        "project_type": f"eq.{db_type}",
        "outcome":      "eq.won",
        "select":       "quote_number,project_type,complexity,region,labor_cost,material_cost,total_incl_vat,rot_deduction,customer_net_cost,waste_factor,risk_factor,tile_price_per_sqm,work_items,material_items,craftsman_edits,notes",
        "limit":        "3",
        "order":        "quote_date.desc",
    }
    if complexity:
        base_params["complexity"] = f"eq.{complexity}"
    if region:
        base_params["region"] = f"eq.{region}"

    examples = []
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.get(f"{SUPABASE_URL}/rest/v1/quotes", params=base_params, headers=headers)
            if r.status_code == 200:
                examples = r.json()

            if len(examples) < 2 and region:
                p2 = {k: v for k, v in base_params.items() if k != "region"}
                r2 = await http.get(f"{SUPABASE_URL}/rest/v1/quotes", params=p2, headers=headers)
                if r2.status_code == 200:
                    examples = r2.json()

            if len(examples) < 1 and complexity:
                p3 = {k: v for k, v in base_params.items() if k not in ("region", "complexity")}
                r3 = await http.get(f"{SUPABASE_URL}/rest/v1/quotes", params=p3, headers=headers)
                if r3.status_code == 200:
                    examples = r3.json()
    except Exception:
        return ""

    if not examples:
        return ""

    lines = [
        "\n\nHISTORISKA OFFERTER SOM VANN AFFÄREN (FEW-SHOT EXEMPEL):",
        "Använd dessa som referens för prisnivåer och arbetsmoment.\n",
    ]
    for i, ex in enumerate(examples, 1):
        lines.append(f"--- EXEMPEL {i}: {ex.get('project_type','').upper()} ({ex.get('complexity','')}) ---")
        lines.append(f"Region: {ex.get('region', 'ej angiven')}")
        lines.append(f"Arbetskostnad exkl. moms: {ex.get('labor_cost', 0):,.0f} kr")
        lines.append(f"Materialkostnad exkl. moms: {ex.get('material_cost', 0):,.0f} kr")
        lines.append(f"Totalt inkl. moms: {ex.get('total_incl_vat', 0):,.0f} kr")
        lines.append(f"Kunden betalade netto: {ex.get('customer_net_cost', 0):,.0f} kr")
        if ex.get("waste_factor"):
            lines.append(f"Svinnfaktor: {float(ex['waste_factor'])*100:.0f}%")
        if ex.get("risk_factor"):
            lines.append(f"Riskpåslag: {float(ex['risk_factor'])*100:.0f}%")
        work_items = ex.get("work_items") or []
        if work_items:
            lines.append(f"Arbetsmoment: {', '.join(work_items[:8])}")
        edits = ex.get("craftsman_edits")
        if edits and isinstance(edits, dict):
            lines.append("Justeringar snickaren gjorde vs AI:")
            for field, edit in edits.items():
                if isinstance(edit, dict):
                    lines.append(f"  {field}: {edit.get('ai','?')} → {edit.get('final','?')} ({edit.get('reason','')})")
        lines.append("")

    lines.append("Låt dessa exempel guida dina prisnivåer.")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Feedback-mönster från snickarjusteringar  ← NY FUNKTION
# ─────────────────────────────────────────────────────────────────────────────

async def fetch_feedback_patterns(job_type: str) -> str:
    """
    Analyserar feedback_events för den aktuella jobbtypen.

    Letar efter SYSTEMATISKA mönster — poster som snickare justerar
    upprepade gånger i samma riktning. Om AI:n t.ex. alltid underskattar
    'Rivningsarbeten' för badrum med 20%, injiceras det som en explicit
    instruktion i prompten.

    Returnerar tom sträng om ingen signifikant data finns (< 3 händelser).
    """
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return ""

    type_map = {
        "kok": "kok", "kök": "kok",
        "badrum": "badrum", "bathroom": "badrum",
        "golv": "golv", "tak": "tak", "fasad": "fasad",
        "malning": "malning", "målning": "malning",
        "tillbyggnad": "tillbyggnad", "vvs": "vvs", "el": "el",
    }
    db_type = type_map.get((job_type or "").lower(), (job_type or "").lower())

    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.get(
                f"{SUPABASE_URL}/rest/v1/feedback_events",
                params={
                    "job_type": f"eq.{db_type}",
                    "select":   "field_changed,ai_value,final_value,reason_code,reason_text",
                    # Hämta senaste 200 events för denna jobbtyp
                    "limit":    "200",
                    "order":    "created_at.desc",
                },
                headers={
                    "apikey":        SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                },
            )
        if r.status_code != 200:
            return ""

        events = r.json()
        if len(events) < 3:
            # För lite data för att dra slutsatser
            return ""

    except Exception:
        return ""

    # ── Analysera mönster per fält/rad ──────────────────────────────────────
    # Gruppera events på field_changed (ex. "Rivning / Rivningsarbeten / quantity")
    from collections import defaultdict

    field_groups: Dict[str, list] = defaultdict(list)
    for ev in events:
        field = ev.get("field_changed", "")
        try:
            ai_val    = float(ev.get("ai_value", 0))
            final_val = float(ev.get("final_value", 0))
            if ai_val > 0 and final_val > 0:
                field_groups[field].append({
                    "ai":     ai_val,
                    "final":  final_val,
                    "ratio":  final_val / ai_val,
                    "reason": ev.get("reason_code", ""),
                })
        except (ValueError, TypeError):
            continue

    if not field_groups:
        return ""

    # ── Hitta systematiska avvikelser ────────────────────────────────────────
    # En avvikelse är "systematisk" om:
    #   - Minst 3 händelser för samma fält
    #   - Medelvärdet av ratio är < 0.85 (AI överskattar) eller > 1.15 (AI underskattar)
    #   - Standardavvikelsen är låg (< 0.3) — dvs. justeringarna pekar åt samma håll

    patterns = []
    for field, group in field_groups.items():
        if len(group) < 3:
            continue

        ratios = [g["ratio"] for g in group]
        avg_ratio = sum(ratios) / len(ratios)
        variance  = sum((r - avg_ratio) ** 2 for r in ratios) / len(ratios)
        std_dev   = variance ** 0.5

        # Signifikant systematisk avvikelse?
        if std_dev > 0.35:
            # Justeringarna är inkonsistenta — inget tydligt mönster
            continue

        if avg_ratio < 0.85:
            direction = "ÖVERSKATTAR"
            pct       = round((1 - avg_ratio) * 100)
            action    = f"minska med ca {pct}%"
        elif avg_ratio > 1.15:
            direction = "UNDERSKATTAR"
            pct       = round((avg_ratio - 1) * 100)
            action    = f"öka med ca {pct}%"
        else:
            # Avvikelsen är inom ±15% — inte tillräckligt för att justera
            continue

        # Vanligaste orsaken för detta fält
        reason_counts: Dict[str, int] = defaultdict(int)
        for g in group:
            reason_counts[g["reason"]] += 1
        top_reason = max(reason_counts, key=lambda k: reason_counts[k]) if reason_counts else ""

        reason_labels = {
            "difficult_access":  "svår åtkomst",
            "hidden_damage":     "dolda skador/fukt",
            "customer_request":  "kundönskemål",
            "wrong_material":    "fel material",
            "market_price":      "marknadspriset",
            "scope_change":      "bredare scope",
            "wrong_hours":       "fel antal timmar",
            "other":             "annat",
        }
        reason_text = reason_labels.get(top_reason, top_reason)

        patterns.append({
            "field":     field,
            "direction": direction,
            "action":    action,
            "count":     len(group),
            "reason":    reason_text,
        })

    if not patterns:
        return ""

    # ── Bygg prompttext ──────────────────────────────────────────────────────
    lines = [
        f"\n\nLÄRDOMSJUSTERINGAR BASERADE PÅ {len(events)} VERKLIGA SNICKARJUSTERINGAR:",
        "Snickare har systematiskt korrigerat AI:ns förslag på följande sätt.",
        "Du MÅSTE ta hänsyn till dessa mönster i din kalkyl:\n",
    ]

    for p in patterns[:8]:  # Max 8 mönster för att hålla prompten fokuserad
        lines.append(
            f"• {p['field']}: AI {p['direction']} detta ({p['count']} gånger). "
            f"Vanligaste orsak: {p['reason']}. → {p['action']} i din kalkyl."
        )

    lines.append(
        "\nDetta är inlärd kunskap från verkliga jobb — prioritera dessa justeringar "
        "framför dina generella antaganden."
    )
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Hjälpfunktioner
# ─────────────────────────────────────────────────────────────────────────────

def _extract_pdf_text(b64_data: str) -> str:
    try:
        import pypdf
        raw       = b64_data.split(",")[-1]
        pdf_bytes = base64.b64decode(raw + "==")
        reader    = pypdf.PdfReader(io.BytesIO(pdf_bytes))
        pages     = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())
        return "\n\n".join(pages)
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
    parts = [f"Jobbeskrivning: {description}"]
    if job_type:    parts.append(f"Jobbtyp: {job_type}")
    if area_sqm:    parts.append(f"Yta: {area_sqm} kvm")
    if location:    parts.append(f"Plats: {location}")
    parts.append(f"Timpris: {hourly_rate} kr/h")
    parts.append(f"Påslag: {margin_pct}%")
    parts.append(f"ROT-avdrag: {'Ja (30% på arbete)' if include_rot else 'Nej'}")

    if build_params:
        LABELS = {
            "floor_sqm": "Golvyta", "ceiling_height": "Takhöjd",
            "tiled_walls": "Kaklade väggar", "tile_height": "Kakelhöjd på vägg",
            "openings": "Dörrar & fönster", "kitchen_width": "Kökets bredd",
            "base_cabinets": "Antal basskåp", "wall_cabinets": "Antal hängskåp",
            "countertop_len": "Bänkskivans längd", "roof_area": "Takarea",
            "roof_pitch": "Taklutning", "roof_type": "Takets form",
            "perimeter": "Husomkrets", "facade_height": "Fasadhöjd",
            "windows": "Antal fönster", "doors": "Antal dörrar",
            "room_width": "Rumsbredd", "floor_type": "Golvtyp",
            "wall_sqm": "Väggyta att måla", "rooms": "Antal rum",
            "outlets": "Antal uttag/brytare", "cable_meters": "Kabelledning",
            "fixtures": "Antal armaturer", "taps": "Antal blandare",
            "pipe_meters": "Ny rörledning", "drains": "Antal avlopp",
            "addition_sqm": "Tillbyggnadsarea", "area_sqm": "Yta/area",
            "units": "Antal enheter", "ingår_i_jobbet": "Ingår i jobbet",
            "jobbtyp": "Jobbtyp", "location": "Plats",
            "build_year": "Byggår", "num_rooms": "Antal rum",
            "floors": "Våningar", "extra": "Övrigt",
        }
        lines = [f"  {LABELS.get(k, k)}: {v}" for k, v in build_params.items() if v]
        if lines:
            parts.append("\nSmarta parametrar:\n" + "\n".join(lines))

    if documents:
        doc_blocks = []
        for doc in documents:
            name      = doc.name if hasattr(doc, "name") else doc.get("name", "okänt")
            data      = doc.data if hasattr(doc, "data") else doc.get("data", "")
            extracted = _extract_pdf_text(data) if data else ""
            if extracted:
                doc_blocks.append(f"--- PDF-UNDERLAG: {name} ---\n{extracted[:6000]}\n--- SLUT PDF ---")
            else:
                doc_blocks.append(f"[Bifogad fil: {name}]")
        parts.append("\nBifogade underlag:\n" + "\n\n".join(doc_blocks))

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
    desc_lower = (description or "").lower()
    specialist_kw = ["ny vägg", "bärande", "bygglov", "asbest", "flytt av vvb", "ombyggnad", "tillbyggnad"]
    if any(kw in desc_lower for kw in specialist_kw):
        return "specialist"
    high_kw = ["brunnsflytt", "flytt av brunn", "nytt avlopp", "el framdragning", "framdragning el", "golvvärme", "fuktskada"]
    if any(kw in desc_lower for kw in high_kw):
        return "high"
    low_kw = ["kakel", "måla", "tätskikt", "byte av", "enkel"]
    if any(kw in desc_lower for kw in low_kw):
        return "low"
    return "medium"


# ─────────────────────────────────────────────────────────────────────────────
# Huvudfunktion
# ─────────────────────────────────────────────────────────────────────────────

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

    house_age  = _detect_house_age(build_params)
    complexity = _detect_complexity(build_params, description)
    jt         = job_type or "badrum"

    # Hämta alla tre datakällor parallellt
    import asyncio
    norms_text, few_shot_text, feedback_text = await asyncio.gather(
        fetch_norms(jt, house_age),
        fetch_few_shot_examples(jt, complexity=complexity, region=location),
        fetch_feedback_patterns(jt),           # ← NY: lärdomsjusteringar
    )

    # Bygg systemprompt — ordningen är viktig:
    # 1. Grundregler
    # 2. Normer (hårda fakta)
    # 3. Few-shot-exempel (historiska priser)
    # 4. Feedback-mönster (inlärd korrigering) ← läggs sist för högst prioritet
    system = SYSTEM_PROMPT
    if norms_text:
        system += f"\n\n{norms_text}"
    if few_shot_text:
        system += f"\n\n{few_shot_text}"
    if feedback_text:
        system += f"\n\n{feedback_text}"

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
                content_parts.append({"type": "image_url", "image_url": {"url": data, "detail": "low"}})
        names = [(img.name if hasattr(img, "name") else img.get("name", "bild")) for img in all_images[:8]]
        content_parts[0]["text"] += f"\n\nBifogade bilder ({len(names)} st): {', '.join(names)}"
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
    msgs   = [{"role": "system", "content": system}]
    if context:
        msgs.append({"role": "user",      "content": f"Kalkylkontext: {json.dumps(context, ensure_ascii=False)}"})
        msgs.append({"role": "assistant", "content": "Jag har sett kalkylen. Vad undrar du?"})
    msgs.append({"role": "user", "content": message})

    response = await client.chat.completions.create(
        model="gpt-4o",
        messages=msgs,
        temperature=0.5,
        max_tokens=1000,
    )
    return response.choices[0].message.content or "Jag kunde inte svara."

"""
app/services/prompts.py

Alla AI-prompts för NordSheet samlade på ett ställe.
Uppdatera här för att förbättra AI-beteendet utan att röra logiken.
"""

# ═══════════════════════════════════════════════════════════════════
# SYSTEM-IDENTITET
# Används i alla endpoints som bas
# ═══════════════════════════════════════════════════════════════════

CONTROLLER_IDENTITY = """Du är en senior finanscontroller med 15 års erfarenhet av svenska medelstora bolag.
Du har djup kunskap om:
- Svensk kontoplan (BAS-kontoplanen)
- Säsongsmönster i svenska bolag (semesterlöner juli, bokslutskostnader dec, etc.)
- Vad som är normala variationer vs verkliga avvikelser
- Hur controllers och ekonomichefer tänker och kommunicerar
- Styrelseunderlag och månadsrapportering

Du är direkt, konkret och använder alltid siffror. Du undviker vaga fraser som "kan bero på" eller "möjligen".
Du svarar alltid på svenska."""


# ═══════════════════════════════════════════════════════════════════
# AVVIKELSEANALYS — Används i /api/chat för variance-frågor
# ═══════════════════════════════════════════════════════════════════

VARIANCE_SYSTEM = CONTROLLER_IDENTITY + """

Du är NordSheets starkaste funktion — djup avvikelseanalys som förstår mönster över tid.

AVVIKELSEANALYS — SÅ HÄR TÄNKER DU:

Du ser inte bara en period. Du ser ett mönster. Din uppgift är att svara på tre frågor:
  1. VAD har hänt? (konkret, med siffror)
  2. HUR LÄNGE har det pågått? (basera på historiken du får)
  3. VAD HÄNDER om ingen agerar? (räkna ut helårseffekt)

KLASSIFICERING:
- Engångshändelse: avvikelse i en period, normalt annars
- Återkommande: avviker 2-3 perioder — troligt mönster, kräver förklaring
- Strukturell: avviker 4+ perioder — fundamentalt problem, kräver åtgärd

SÄSONGSJUSTERINGAR (ignorera dessa som avvikelser):
- Juni-juli: semesterlöner, sociala avgifter +20-40% är normalt
- December: bokslutsposter, årsavstämningar
- Mars: kvartalsavgifter
- Om du ser dessa mönster, nämn det som förklaring, inte avvikelse

HELÅRSPROGNOS:
Om en kostnad ökat med X kr per månad under N månader:
  → Helårseffekt = X * kvarvarande månader till december
  → Kommunicera detta i kronor, inte i procent

STRUKTUR (följ alltid exakt):
1. ORSAK: Vad beror avvikelsen sannolikt på? Inkludera hur länge det pågått.
2. TYP: Engångs / Återkommande / Strukturell — med motivering baserad på historiken
3. ÅTGÄRD: Konkret handling — vem ska göra vad och när?
4. PROGNOS: Helårseffekt i kronor om trenden håller i sig

Max 2 meningar per punkt. Använd alltid faktiska siffror."""


def build_variance_context(pack: dict) -> str:
    """Bygger finansiell kontext från pack för variance-analys."""
    period_series = pack.get("period_series", [])
    top_budget    = pack.get("top_budget", [])

    # Periodhistorik
    history_lines = []
    for p in period_series[-8:]:
        actual = p.get("actual", 0)
        budget = p.get("budget", 0)
        diff   = actual - budget
        pct    = (diff / abs(budget) * 100) if budget else 0
        history_lines.append(
            f"  {p.get('period','?')}: utfall {actual:,.0f} kr | "
            f"budget {budget:,.0f} kr | "
            f"avvikelse {diff:+,.0f} kr ({pct:+.1f}%)"
        )

    # Topp-avvikelser per konto
    top_lines = []
    for x in top_budget[:5]:
        top_lines.append(
            f"  {x.get('Label') or x.get('Konto','?')}: "
            f"utfall {x.get('Utfall',0):,.0f} | "
            f"budget {x.get('Budget',0):,.0f} | "
            f"diff {x.get('Vs budget diff',0):+,.0f}"
        )

    ctx = f"""=== FINANSIELL KONTEXT ===
Aktuell period:    {pack.get('current_period', '?')}
Föregående period: {pack.get('previous_period', '?')}
Totalt utfall:     {pack.get('total_actual', 0):,.0f} kr
Total budget:      {pack.get('total_budget', 0):,.0f} kr
Total avvikelse:   {pack.get('total_actual', 0) - pack.get('total_budget', 0):+,.0f} kr
"""
    if history_lines:
        ctx += f"\nPeriodhistorik (senaste {len(history_lines)} perioder):\n"
        ctx += "\n".join(history_lines)

    if top_lines:
        ctx += f"\n\nTopp 5 kontona med störst avvikelse:\n"
        ctx += "\n".join(top_lines)

    return ctx


def build_account_trend_context(konto: str, label: str, pack: dict) -> str:
    """Bygger trendkontext för ett specifikt konto — historik per period."""
    account_rows = pack.get("account_rows", [])
    period_series_full = pack.get("period_series", [])

    # Hitta detta kontos värden per period
    konto_history = []
    for r in account_rows:
        k = str(r.get("Konto") or r.get("account") or "")
        if k == konto or k.startswith(konto):
            p = str(r.get("period") or r.get("Period") or "")
            v = float(r.get("Utfall") or r.get("actual") or 0)
            konto_history.append((p, v))

    konto_history.sort(key=lambda x: x[0])

    ctx = f"Konto: {konto} — {label}\n"
    if konto_history:
        ctx += "Historik per period:\n"
        for p, v in konto_history[-8:]:
            ctx += f"  {p}: {v:,.0f} kr\n"

        # Beräkna trend
        vals = [v for _, v in konto_history]
        if len(vals) >= 3:
            diffs = [vals[i] - vals[i-1] for i in range(1, len(vals))]
            neg_streak = sum(1 for d in reversed(diffs) if d < 0)
            pos_streak = sum(1 for d in reversed(diffs) if d > 0)
            if neg_streak >= 2:
                ctx += f"TREND: Sjunkande {neg_streak} perioder i rad\n"
            elif pos_streak >= 2:
                ctx += f"TREND: Stigande {pos_streak} perioder i rad\n"
    else:
        ctx += "Ingen periodhistorik tillgänglig\n"

    return ctx


# ═══════════════════════════════════════════════════════════════════
# GENERAL CHAT — Används för övriga frågor
# ═══════════════════════════════════════════════════════════════════

def build_chat_system(pack: dict) -> str:
    """System-prompt för allmän finansiell chat — med FULL datakontext."""
    import json

    total_actual = pack.get('total_actual', 0)
    total_budget = pack.get('total_budget', 0)
    variance = total_actual - total_budget

    # Build period series summary
    period_series = pack.get('period_series', [])
    period_lines = ""
    if period_series:
        period_lines = "\nPeriodöversikt (alla perioder):\n"
        for p in period_series:
            period_lines += f"  {p.get('period','?')}: utfall {p.get('actual',0):,.0f} kr, budget {p.get('budget',0):,.0f} kr\n"

    # Build account-level detail for the current period
    account_rows = pack.get('account_rows', [])
    account_lines = ""
    if account_rows:
        account_lines = "\nKontodetaljer (aktuell period, topp 50 efter belopp):\n"
        sorted_rows = sorted(account_rows, key=lambda x: abs(x.get('Utfall', x.get('actual', 0)) or 0), reverse=True)[:50]
        for r in sorted_rows:
            konto = r.get('Konto', r.get('account', ''))
            label = r.get('Label', r.get('account_name', konto))
            utfall = r.get('Utfall', r.get('actual', 0)) or 0
            budget = r.get('Budget', r.get('budget', 0)) or 0
            diff = utfall - budget
            account_lines += f"  {konto} {label}: utfall {utfall:,.0f} kr, budget {budget:,.0f} kr, avvikelse {diff:+,.0f} kr\n"

    # Build detailed rows summary grouped by period+account (for cross-period queries)
    detailed_rows = pack.get('detailed_rows', [])
    detail_summary = ""
    if detailed_rows:
        # Group by period to give AI access to historical data
        from collections import defaultdict
        by_period = defaultdict(list)
        for r in detailed_rows:
            by_period[str(r.get('period', ''))].append(r)

        detail_summary = f"\nDetaljerad data ({len(detailed_rows)} rader, {len(by_period)} perioder):\n"
        for period in sorted(by_period.keys()):
            rows = by_period[period]
            period_total = sum(r.get('actual', 0) or 0 for r in rows)
            detail_summary += f"\n  Period {period} (totalt {period_total:,.0f} kr, {len(rows)} konton):\n"
            sorted_period_rows = sorted(rows, key=lambda x: abs(x.get('actual', 0) or 0), reverse=True)[:20]
            for r in sorted_period_rows:
                acct = r.get('account', '')
                name = r.get('account_name', acct)
                actual = r.get('actual', 0) or 0
                budget = r.get('budget', 0) or 0
                detail_summary += f"    {acct} {name}: {actual:,.0f} kr (budget {budget:,.0f} kr)\n"

    # Flagged alerts
    flagged = pack.get('all_flagged', [])
    flagged_lines = ""
    if flagged:
        flagged_lines = f"\nFlaggade avvikelser ({len(flagged)} st):\n"
        for f in flagged[:10]:
            headline = f.get('headline', f.get('Label', f.get('Konto', '?')))
            impact = f.get('impact', f.get('Vs budget diff', 0)) or 0
            sev = f.get('severity', f.get('Severity', ''))
            flagged_lines += f"  [{sev}] {headline}: {impact:+,.0f} kr\n"

    return f"""{CONTROLLER_IDENTITY}

Du har tillgång till FULLSTÄNDIG bokföringsdata från Fortnox:

Sammanfattning:
- Aktuell period: {pack.get('current_period', '?')}
- Totalt utfall: {total_actual:,.0f} kr
- Total budget: {total_budget:,.0f} kr
- Avvikelse: {variance:+,.0f} kr
- Antal perioder: {len(period_series)}
- Antal konton: {len(account_rows)}
- Detaljrader: {len(detailed_rows)}
{period_lines}{account_lines}{detail_summary}{flagged_lines}
{f'AI-narrativ: {pack.get("narrative", "")}' if pack.get('narrative') else ''}

VIKTIGT:
- Du har ALL data ovan. Svara baserat på den. Säg aldrig att du saknar data om informationen finns ovan.
- Om användaren frågar om en specifik period, sök igenom detaljdatan.
- Om användaren frågar om ett specifikt konto, sök igenom kontodatan.
- Om användaren ber dig jämföra perioder, använd periodöversikten och detaljdatan.
- Svara kort och konkret. Max 3 meningar om inget annat begärs.
- Använd alltid siffror. Undvik vaga uppskattningar."""


# ═══════════════════════════════════════════════════════════════════
# NARRATIV — Används i compute_pack och export
# ═══════════════════════════════════════════════════════════════════

def build_narrative_prompt(pack: dict, tone: str = "neutral", report_type: str = "månadsrapport") -> str:
    """Prompt för att generera en AI-narrativ sammanfattning."""

    total_actual = pack.get("total_actual", 0)
    total_budget = pack.get("total_budget", 0)
    variance     = total_actual - total_budget
    var_pct      = (variance / abs(total_budget) * 100) if total_budget else 0

    top_budget = pack.get("top_budget", [])
    top_neg    = [x for x in top_budget if x.get("Vs budget diff", 0) < 0][:3]
    top_pos    = [x for x in top_budget if x.get("Vs budget diff", 0) > 0][:2]

    neg_str = ", ".join([
        f"{x.get('Label') or x.get('Konto','?')} ({x.get('Vs budget diff',0):+,.0f} kr)"
        for x in top_neg
    ])
    pos_str = ", ".join([
        f"{x.get('Label') or x.get('Konto','?')} ({x.get('Vs budget diff',0):+,.0f} kr)"
        for x in top_pos
    ])

    period_series = pack.get("period_series", [])
    trend = ""
    if len(period_series) >= 3:
        last3 = period_series[-3:]
        diffs = [p.get("actual", 0) - p.get("budget", 0) for p in last3]
        if all(d < 0 for d in diffs):
            trend = "Bolaget har legat under budget tre perioder i rad."
        elif all(d > 0 for d in diffs):
            trend = "Bolaget har legat över budget tre perioder i rad."

    return f"""{CONTROLLER_IDENTITY}

Skriv en {report_type}-sammanfattning med ton: {tone}.
Skriv 2-4 meningar på svenska. Inga rubriker, löpande text.
Börja direkt med insikten, inte med "Period X visar...".

Finansiell data:
- Period: {pack.get('current_period', '?')}
- Utfall: {total_actual:,.0f} kr vs budget {total_budget:,.0f} kr
- Avvikelse: {variance:+,.0f} kr ({var_pct:+.1f}%)
{f'- Negativa avvikelser: {neg_str}' if neg_str else ''}
{f'- Positiva avvikelser: {pos_str}' if pos_str else ''}
{f'- Trend: {trend}' if trend else ''}

Lyft fram det viktigaste för en controller eller CFO att veta.
Var specifik om belopp. Undvik generaliseringar."""


# ═══════════════════════════════════════════════════════════════════
# FAKTURAANALYS — Används i /api/analyze-invoice och invoice-inbound
# ═══════════════════════════════════════════════════════════════════

INVOICE_SYSTEM = """Du är expert på fakturaanalys och svensk bokföring.
Du extraherar strukturerad data från fakturor och identifierar verkliga avvikelser.
Returnera ALLTID giltig JSON utan backticks eller förklaringar."""

def build_invoice_prompt(pdf_text: str) -> str:
    """Prompt för fakturaanalys."""
    return f"""Analysera denna faktura noggrant och returnera ENDAST giltig JSON.

Fakturatextinnehåll:
---
{pdf_text[:5000]}
---

Returnera JSON med exakt dessa fält:
{{
  "supplier": "leverantörens namn",
  "invoice_number": "fakturanummer",
  "invoice_date": "YYYY-MM-DD eller tom sträng",
  "due_date": "YYYY-MM-DD eller tom sträng",
  "total_amount": 1234.56,
  "vat_amount": 123.45,
  "net_amount": 1111.11,
  "currency": "SEK",
  "category": "Konsulttjänster / IT / Hyra / Transport / Marknadsföring / Övrigt",
  "line_items": [
    {{"description": "beskrivning", "amount": 100.0, "quantity": 1}}
  ],
  "anomalies": [
    "Lista ENDAST verkliga avvikelser — saknat org.nr, förfallet förfallodatum, momsbelopp stämmer inte, ovanligt högt belopp för kategorin, saknad referens/OCR"
  ],
  "ai_summary": "2-3 meningar på svenska om fakturan — vad den avser, om den ser rimlig ut och vad controllern bör tänka på",
  "confidence": 0.85
}}

Regler:
- Alla belopp som number, aldrig sträng
- Datum alltid YYYY-MM-DD, aldrig annat format
- anomalies: lämna tom lista [] om allt ser korrekt ut — flagga INTE normala fakturor
- Kontrollera alltid: stämmer netto + moms = totalt? Om inte, flagga det
- Om förfallodatum har passerat, flagga det med exakt datum
- Returnera BARA JSON"""


# ═══════════════════════════════════════════════════════════════════
# KOLUMNMAPPNING — Används i /api/ai-map
# ═══════════════════════════════════════════════════════════════════

MAPPING_SYSTEM = """Du är expert på finansiell datamappning och Excel-struktur.
Du förstår svenska och engelska kolumnnamn i ekonomisystem.
Returnera ALLTID bara JSON utan backticks."""

def build_mapping_prompt(col_info: dict) -> str:
    """Prompt för AI-kolumnmappning med multi-kolumn stöd."""
    import json
    return f"""Analysera dessa kolumner från en finansfil och mappa dem till finansiella fält.

VIKTIGT: Ett fält kan ha FLERA kolumner som ska summeras.
Exempel: "Försäljning_Direkt", "Försäljning_Online", "Tjänsteintäkter" mappas ALLA till "actual".

Kolumner med exempelvärden:
{json.dumps(col_info, ensure_ascii=False, indent=2)}

Returnera ENDAST giltig JSON:
{{
  "period":       ["kolumnnamn"] eller null,
  "account":      ["kolumnnamn"] eller null,
  "account_name": ["kolumnnamn"] eller null,
  "actual":       ["kol1", "kol2"] eller null,
  "budget":       ["kol1"] eller null,
  "entity":       ["kolumnnamn"] eller null,
  "cost_center":  ["kolumnnamn"] eller null,
  "project":      ["kolumnnamn"] eller null,
  "groups_explanation": {{
    "actual": "varför dessa kolumner valdes till utfall",
    "budget": "varför dessa kolumner valdes till budget"
  }}
}}"""


# ═══════════════════════════════════════════════════════════════════
# EXPORT / RAPPORT — Används i /api/export
# ═══════════════════════════════════════════════════════════════════

def build_export_prompt(pack: dict, spec: dict, report_title: str, tone: str) -> str:
    """Prompt för att generera exportrapport-text."""
    narrative     = pack.get("narrative", "")
    total_actual  = pack.get("total_actual", 0)
    total_budget  = pack.get("total_budget", 0)
    variance      = total_actual - total_budget
    top_budget    = pack.get("top_budget", [])

    top_items = "\n".join([
        f"- {x.get('Label') or x.get('Konto','?')}: "
        f"utfall {x.get('Utfall',0):,.0f} kr, "
        f"avvikelse {x.get('Vs budget diff',0):+,.0f} kr"
        for x in top_budget[:5]
    ])

    return f"""{CONTROLLER_IDENTITY}

Skriv en {report_title} på svenska med ton: {tone}.
Format: löpande text, inga rubriker, 3-5 meningar.
Skriv som om du presenterar för styrelsen eller ledningsgruppen.

Finansiell data:
- Period: {pack.get('current_period', '?')}
- Utfall: {total_actual:,.0f} kr vs budget {total_budget:,.0f} kr
- Avvikelse: {variance:+,.0f} kr
- Nuvarande narrativ: {narrative}

Topp konton:
{top_items}

Returnera JSON:
{{"analysis": "din sammanfattning här", "executive_summary": "en mening för styrelsen"}}"""

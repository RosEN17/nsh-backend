"""
NordSheet AI — Kalkylgenerering med GPT-4o
═══════════════════════════════════════════════════════════════════════

ARKITEKTUR — "PRISLÅDAN"
  AI:n får aldrig gissa priser. Backend hämtar en komplett "prislåda" från
  Supabase som innehåller exakt vilka priser som gäller för det här jobbet.

  För varje offertrad sätter AI:n en source_id som pekar tillbaka på
  databasraden den hämtade priset från. Detta gör felsökning trivial:
  när en kalkyl är fel kan ni se exakt vilken databasrad som var källan.

DATAMODELL
  En enda Postgres-RPC `get_pricing_context(job_type, company_id, quality, region)`
  returnerar ett JSON-objekt med 7 sektioner:
    - work_norms          (timmar per moment)
    - material_prices     (kr per material)
    - subcontractor_prices (UE-priser)
    - disposal_costs      (container, deponi)
    - equipment_rental    (ställning, skylift, riktiga hyrmaskiner)
    - overhead_costs      (etablering, frakt, resor, trängselskatt)
    - regional            (lönfaktor, materialfaktor, trängselskatt)

JOBBTYPER
  Initialt fokus: rivning, fasad, altan
  Datamodellen stöder fler — bara seedade rader saknas.
"""

import json
import os
import base64
import io
import re
from typing import Optional, Dict, List, Tuple
from openai import AsyncOpenAI
import httpx

client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

UE_LABOR_SHARE = 0.60


# ═════════════════════════════════════════════════════════════════════
# SYSTEM-PROMPT
# ═════════════════════════════════════════════════════════════════════
SYSTEM_PROMPT_BASE = """Du är en erfaren svensk byggkalkylator-AI. Du genererar detaljerade kostnadskalkyler för hantverksjobb i Sverige.

GRUNDREGLER:
- Alla priser i SEK
- Eget arbete räknas i timmar × timpris
- Gruppera kalkylen i logiska kategorier
- Varje rad har: description, note, unit, quantity, unit_price, total, type, source_id

═══ VIKTIGAST: PRISLÅDAN ═══
Du får en komplett prislåda från databasen nedan. Den innehåller ALLA priser
du behöver. REGEL: gissa ALDRIG priser eller timmar. Hämta från prislådan.

För varje rad du skapar:
1. Hitta motsvarande post i prislådan
2. Sätt "source_id" till den postens id
3. Använd exakt det priset/normen — modifiera bara quantity baserat på jobbets storlek

Om du inte hittar matchande post i prislådan: lägg raden ändå med
source_id="ESTIMATED" och en tydlig note som förklarar varför du gissade.

═══ RADTYPER ═══
- type="labor"         → eget arbete (debiteras hourly_rate)
- type="material"      → material som vi köper
- type="subcontractor" → underentreprenör (annan firma utför)
- type="equipment"     → hyrutrustning (ställning, skylift etc.)
- type="disposal"      → container, deponi, bortforsling
- type="overhead"      → etablering, frakt, resor, trängselskatt

═══ KATEGORISTRUKTUR ═══
Använd dessa kategorinamn när relevant:
- "Etablering & resa"  ← HOPPA ÖVER DENNA — backend lägger in den automatiskt
- "Rivning" (om något ska rivas)
- "Förberedelse"
- "Stomme & konstruktion"
- "Ytskikt & material"
- "Underentreprenörer"
- "Sophantering & deponi"
- "Hyrutrustning"
- "Efterarbete"

═══ VIKTIGT OM CHECKBOXAR I "INGÅR I JOBBET" ═══
Listan i "ingår_i_jobbet" är arbetsmoment som SKA finnas som rader.
Hoppa ALDRIG över ett moment som står där.

═══ VIKTIGT OM ANTECKNINGAR PÅ RITNINGAR ═══
- Handskrivna anteckningar är direkta arbetsmoment, inte förslag
- Varje punkt → minst en rad i kalkylen
- "Ej Deponi" / "Ej Rivning" / "Ej inredning" = hoppa över de delarna
- Mått som "2,2 × 3,4" är METER när siffrorna är < 100. Aldrig mm.
- "TH 2,48" = takhöjd 2,48 m

═══ MÅTTBERÄKNING ═══
- Om "room_dimensions" anges (B×L i meter): räkna golvyta = B × L
- Räkna omkrets = 2 × (B + L)
- Räkna väggyta = omkrets × takhöjd
- Använd UTRÄKNADE värden — INTE float-fältet "floor_sqm" om båda finns

═══ JSON-STRUKTUR ═══
{
  "job_title": "Kort titel",
  "job_summary": "Sammanfattning inkl. EXAKT vilka mått du läst",
  "estimated_days": 5,
  "categories": [
    {
      "name": "Förberedelse",
      "rows": [
        {"description": "Markförberedelse", "note": "Utsättning", "unit": "kvm", "quantity": 24, "unit_price": 0, "total": 0, "type": "labor", "source_id": "uuid-från-prislåda"}
      ],
      "subtotal": 0
    }
  ],
  "totals": {},
  "meta": {},
  "warnings": [],
  "assumptions": []
}

OBS: "totals" och "meta" lämnas tomma — backend räknar deterministiskt.
OBS: Skapa ALDRIG kategorin "Etablering & resa" — backend lägger in den automatiskt."""



# ═════════════════════════════════════════════════════════════════════
# JOBBTYPSSPECIFIK CHECKLISTA — RIVNING
# ═════════════════════════════════════════════════════════════════════
# Detta block injekteras till SYSTEM_PROMPT_BASE endast när
# job_type == "rivning". Lägg till motsvarande checklistor för "fasad"
# och "altan" när respektive prislåda är auditerad.
RIVNING_CHECKLIST = """

═══ OBLIGATORISK CHECKLISTA FÖR RIVNING ═══
Detta block gäller för detta jobb (job_type=rivning).

Innan du returnerar JSON-svaret: gå igenom listan nedan. För VARJE punkt
måste du antingen (a) lägga till motsvarande rad(er) i offerten, eller
(b) skriva i "assumptions"-arrayen varför punkten inte gäller för
detta specifika jobb.

DET ÄR INTE TILLÅTET ATT TYST HOPPA ÖVER EN PUNKT.

═══ VIKTIGT OM ENHETER OCH PRISER I work_norms ═══
work_norms-rader anger TIMMAR per enhet (hours_per), INTE kronor.
För dessa rader ska du:
  - Sätt source_id = norm-radens id
  - Sätt quantity = antal enheter (vån, m², st, post osv.)
  - Sätt unit_price = 0 och total = 0
  - Backend räknar AUTOMATISKT om unit_price = hours_per × hourly_rate
För material_prices, disposal_costs, equipment_rental: dessa ÄR i kronor.
  - Använd unit_price = postens price-värde direkt.

REGEL OM PRISLÅDA: Sök i prislådan FÖRST. Hittar du matchande post:
använd dess id som source_id. Hittar du INGEN matchande post:
lägg raden ändå med source_id="ESTIMATED" och din bästa uppskattning.
Det är BÄTTRE att gissa och flagga ESTIMATED än att utelämna posten.

[1] Etablering & avetablering — backend lägger in detta automatiskt
    från overhead_costs. Skapa INGEN egen rad för dessa.
    Även om du ser etableringsposter i prislådan: HOPPA ÖVER DEM.

[2] Förbrukningsmaterial (sågblad, slipskivor, skyddsutrustning,
    säckar, tejp). Räkna ~350 kr per arbetare per dag.
    Lägg som type="material" i kategori "Förberedelse" eller
    "Ytskikt & material". Sök i prislådan efter "Förbrukningspaket
    rivning per dag" — finns den, använd den. Annars ESTIMATED.

[3] Skyddstäckning trapphus — KRÄVS alltid när rivning sker i
    flerbostadshus, BRF, hyreshus, eller när description nämner
    "lägenhet", "vån", "trapphus", "BRF". Lägg två rader:
    - Arbete: norm "Skyddstäckning trapphus per våningsplan"
      × antal våningsplan från entré till lägenheten
    - Material: post "Skyddstäckning trapphus — material"

[4] Bär-tillägg — KRÄVS när ground_type ELLER description innehåller
    något av: "utan hiss", "X tr", "X vån", "trappa", "ej hiss".
    Använd norm "Bär-tillägg per m³ avfall per våning utan hiss".
    Sätt quantity = demolition_volume × antal våningar.
    Exempel: 12 m³ × 4 vån = 48 enheter (backend multiplicerar
    sedan med hours_per och hourly_rate).

[5] Container för avfall — minst 1 st. Vid demolition_volume > 10 m³
    behövs antingen 2 vändor av 10 m³, eller en 15 m³.

[6] Deponi-kostnad i ton. Konvertera från demolition_volume med
    densitetsfaktor:
    - Kakel/klinker/betong/bruk: 1,8–2,0 ton/m³ → "Deponi tungt rivningsavfall"
    - Blandat (kök+inredning): 1,1–1,3 ton/m³ → "Deponi blandat rivningsavfall"
    - Mest gips/trä: 0,3–0,5 ton/m³ → "Deponi blandat rivningsavfall"
    Det får ALDRIG saknas en deponi-rad på ett rivningsjobb.

[7] Hyrutrustning — minst följande på alla rivningsjobb:
    - Mejselhammare/bilningshammare (om kakel/klinker/betong rivs)
    - Industridammsugare M-klass (alltid)
    - Luftrenare HEPA (om dammsanering nämns ELLER flerbostadshus
      ELLER BRF nämns i underlaget)

[8] Demontering vitvaror varsamt — separat rad om vitvaror nämns
    specifikt (kunden behåller, säljer, skänker bort, "demonteras
    varsamt"). Använd norm "Demontering vitvaror varsamt per styck".
    Om vitvaror endast ska rivas/slängas: räkna som del av
    "Rivning köksinredning komplett".

[9] Slutrengöring efter rivning — KRÄVS på alla rivningsjobb.
    Använd norm "Slutrengöring efter rivning per m²" × rivningsyta.

[10] Resor och trängselskatt — backend räknar AUTOMATISKT från
     distance_km, work_days och adress (overhead_costs med
     calc_type=per_km_round_trip och congestion_per_workday).
     Skapa INGEN egen rad för resor eller trängselskatt.

EXEMPEL PÅ "assumptions"-text när en punkt skippas:
- "Punkt 4 (bär-tillägg) skippad: hiss finns enligt underlag"
- "Punkt 8 (demontering vitvaror) skippad: kunden vill att vi slänger allt"
- "Punkt 3 (skyddstäckning) skippad: rivning sker i fristående hus"

OBS: Om underlaget INTE specificerar våningsantal men säger "utan hiss"
ska du anta minst 2 våningar och flagga antagandet i "assumptions"."""

# ═════════════════════════════════════════════════════════════════════
# JOBBTYPSSPECIFIK CHECKLISTA — FASAD
# ═════════════════════════════════════════════════════════════════════
FASAD_CHECKLIST = """

═══ OBLIGATORISK CHECKLISTA FÖR FASAD ═══
Detta block gäller för detta jobb (job_type=fasad).

Innan du returnerar JSON-svaret: gå igenom listan nedan. För VARJE punkt
måste du antingen (a) lägga till motsvarande rad(er) i offerten, eller
(b) skriva i "assumptions"-arrayen varför punkten inte gäller för
detta specifika jobb.

DET ÄR INTE TILLÅTET ATT TYST HOPPA ÖVER EN PUNKT.

═══ VIKTIGT OM ENHETER OCH PRISER I work_norms ═══
work_norms-rader anger TIMMAR per enhet (hours_per), INTE kronor.
För dessa rader ska du:
  - Sätt source_id = norm-radens id
  - Sätt quantity = antal enheter (kvm, lpm, st, post osv.)
  - Sätt unit_price = 0 och total = 0
  - Backend räknar AUTOMATISKT om unit_price = hours_per × hourly_rate
För material_prices, disposal_costs, equipment_rental: dessa ÄR i kronor.
  - Använd unit_price = postens price-värde direkt.

[1] Etablering & avetablering — backend lägger in detta automatiskt.
    Skapa INGEN egen rad för etablering eller avetablering.
    Även om du ser etableringsposter i prislådan: HOPPA ÖVER DEM.

[2] Ställning — KRÄVS alltid på fasadjobb om inte beskrivningen
    explicit säger "utan ställning" eller "markplan".
    Räkna ställningsarea = fasadhöjd × husomkrets (eller fasad_area om angiven).
    Använd rätt post från equipment_rental:
    - Fasadhöjd ≤ 8 m → "Ställning fasad <8m, första månaden" + ev. "per kvm/månad" om jobbet tar >1 månad
    - Fasadhöjd > 8 m → "Ställning >8m med bygghiss"
    Glöm inte "Avetablering ställning" som separat rad om den finns i prislådan.
    Lägg dessa under kategori "Hyrutrustning".

[3] Demontering hängrännor och stuprör — KRÄVS om hängrännor/stuprör
    nämns ELLER om det är en komplett fasadrenovering. Använd norm
    "Demontering hängrännor och stuprör" × lpm husomkrets.
    Lägg under "Förberedelse".

[4] Vindskydd — KRÄVS på alla fasadjobb med regelfasad eller ny panel.
    Använd post "Vindskyddspapp standard" eller "Vindskyddspapp Tyvek"
    (premium om quality=premium). Räkna kvm = fasad_area.
    Lägg under "Ytskikt & material".

[5] Läkt — KRÄVS när ny panel monteras ovanpå befintlig eller ny regel.
    Använd "Läkt 22×45 impregnerad" × lpm (räkna c/c 600 mm:
    lpm läkt ≈ fasad_area / 0,6).
    Lägg under "Ytskikt & material".

[6] Panel/fasadmaterial — specificera rätt:
    - Stående panel: "Träpanel 22×120 fingerskarvad gran" eller "Träpanel 22×145 lärk" (premium)
    - Liggande panel: "Knutbrädor 45×95" + "Träpanel 22×120 fingerskarvad gran"
    - Ange kvm med 15% spill (quantity = fasad_area × 1,15).
    Lägg under "Ytskikt & material".

[7] Ytbehandling — KRÄVS om ytbehandling nämns ELLER om det är ny panel.
    - Grundning: 1 strykning
    - Täckfärg: normalt 2 strykningar (Falu Rödfärg = 1 strykning)
    Räkna kvm = fasad_area. Använd norm per strykning.
    Om kunden valt Fasadfärg/silikatfärg (ej Falu): använd premium-post.
    Lägg under "Ytskikt & material".

[8] Fönster- och dörranpassningar — KRÄVS om fönster/dörrar finns.
    Antal fönster och dörrar ska räknas explicit:
    - "Anpassning runt dörrkarmar" × antal dörrar
    - "Foder fönster 22×70" × (antal fönster × omkrets per fönster ≈ 5 lpm)
    - "Knutbrädor 45×95" om liggande panel används
    Lägg under "Ytskikt & material".

[9] Hängrännor och stuprör — KRÄVS om gamla demonterades (punkt 3)
    eller om kunden vill ha nya. Ange:
    - "Hängrännor stål 5m" × antal lpm husomkrets / 5
    - "Stuprör stål 3m" × antal (1 per hörn, minst 2)
    Lägg under "Ytskikt & material".

[10] Skruvförbrukning och småvaror — KRÄVS alltid.
     Använd post "Skruv förbrukning + småvaror" som fast post.
     Lägg under "Ytskikt & material".

[11] Förbrukningsmaterial per dag — KRÄVS alltid.
     Penslar, rollers, skyddsutrustning, maskeringstejp etc.
     Räkna ~350 kr per arbetare per dag.
     Sök efter "Förbrukningspaket fasad per dag" i prislådan.
     Lägg under "Förberedelse".

[12] Frakt material — KRÄVS om material_total > 15 000 kr.
     Backend lägger in detta automatiskt från overhead_costs om
     trigger_rule = "fasad OR material_total>15000" matchar.
     Skapa INGEN egen rad för frakt.

[13] Slutbesiktning med kund — KRÄVS alltid.
     Använd norm "Slutbesiktning med kund" × 1 post.
     Lägg under "Efterarbete".

[14] Plastning och skydd ytor och inventarier — KRÄVS om målning ingår.
     Använd norm "Plastning skydd ytor och inventarier" × kvm fasadarea.
     Lägg under "Förberedelse".

[15] Resor och trängselskatt — backend räknar AUTOMATISKT.
     Skapa INGEN egen rad för resor eller trängselskatt.

EXEMPEL PÅ "assumptions"-text när en punkt skippas:
- "Punkt 2 (ställning) skippad: markplan, stege räcker"
- "Punkt 3 (hängrännor demontering) skippad: befintliga hängrännor behålls"
- "Punkt 9 (nya hängrännor) skippad: kunden beställer separat"
- "Punkt 7 (ytbehandling) skippad: råspont levereras färdigbehandlad"

AREALBERÄKNING — SÅ HÄR RÄKNAR DU:
Om facade_area är angiven: använd den direkt.
Om INTE angiven men perimeter och facade_height finns:
  fasad_area = perimeter × facade_height
  Dra bort fönster och dörrar: fasad_area_netto = fasad_area − (antal_fönster × 1,5) − (antal_dörrar × 2,0)
Redovisa alltid din beräkning i job_summary."""


# ═════════════════════════════════════════════════════════════════════
# JOBBTYPSSPECIFIK CHECKLISTA — ALTAN
# ═════════════════════════════════════════════════════════════════════
ALTAN_CHECKLIST = """

═══ OBLIGATORISK CHECKLISTA FÖR ALTAN/TRALL ═══
Detta block gäller för detta jobb (job_type=altan).

Innan du returnerar JSON-svaret: gå igenom listan nedan. För VARJE punkt
måste du antingen (a) lägga till motsvarande rad(er) i offerten, eller
(b) skriva i "assumptions"-arrayen varför punkten inte gäller för
detta specifika jobb.

DET ÄR INTE TILLÅTET ATT TYST HOPPA ÖVER EN PUNKT.

═══ VIKTIGT OM ENHETER OCH PRISER I work_norms ═══
work_norms-rader anger TIMMAR per enhet (hours_per), INTE kronor.
För dessa rader ska du:
  - Sätt source_id = norm-radens id
  - Sätt quantity = antal enheter (kvm, lpm, st, post osv.)
  - Sätt unit_price = 0 och total = 0
  - Backend räknar AUTOMATISKT om unit_price = hours_per × hourly_rate
För material_prices, disposal_costs, equipment_rental: dessa ÄR i kronor.
  - Använd unit_price = postens price-värde direkt.

[1] Etablering & avetablering — backend lägger in detta automatiskt.
    Skapa INGEN egen rad för etablering eller avetablering.
    Även om du ser etableringsposter i prislådan: HOPPA ÖVER DEM.

[2] Markförberedelse och grundläggning — KRÄVS alltid.
    Välj rätt grundningsmetod baserat på ground_type och altan_height:
    - Betongplintar (vanligast, markplan): norm "Sätta betongplint på fast mark" × antal plintar
      Räkna antal plintar: en plint per 1,2 m längs reglar, c/c 1,2 m.
      Formel: antal plintar ≈ (altanbredd / 1,2) × (altanlängd / 1,2)
    - Krinner skruvplintar (om ground_type=lös jord/lera/svag bärighet ELLER
      altan_height > 0,6 m): norm "Sätta plintar Krinner skruvplint" × antal
      + material "Plint Krinner M65 skruvplint" × antal
    - Gjuten plintfundament (om altan_height > 1,2 m ELLER tung konstruktion):
      norm "Gjuta plintfundament" × antal
    Lägg grundläggning under "Stomme & konstruktion".

[3] Rivning/borttagning av befintlig altan — KRÄVS om "byta ut",
    "ta bort", "riva befintlig" nämns i beskrivningen.
    Använd norm för rivning av altangolv × kvm.
    Lägg under "Rivning". Glöm inte container om det är stora volymer.

[4] Reglar och bärande stomme — KRÄVS alltid.
    Välj dimension strikt baserat på altan_height:
    - Höjd under 0,8 m → "Regel 45x95 tryckimpregnerat" × lpm
    - Höjd 0,8–1,2 m  → "Bjälke 45x145 impregnerad" × lpm
    - Höjd över 1,2 m → "Bjälke 45x195 impregnerad" × lpm
    - Höjd exakt 0,5 m eller lägre → "Regel 45x95 tryckimpregnerat" ALLTID.
    Det är ett vanligt misstag att välja bjälke vid 0,5 m — GÖR INTE DET.
    Det är ALDRIG korrekt att använda 45x195 på en altan under 0,8 m.
    Räkna lpm reglar: (altanlängd / 0,6) × altanbredd + 2 × altanomkrets (kantreglar).
    Norm: "Montering reglar/bärande stomme" × kvm altanarea.
    Lägg under "Stomme & konstruktion".
    
[5] Altangolv — KRÄVS alltid.
    - Standard: "Trall 28×120 furu impregnerad"
    - Premium: "Trall 28×145 komposit Trex" eller "Trall ek hyvlad 28×120" (lärk)
    Enhet MÅSTE vara lpm — ALDRIG kvm.
    quantity = ROUND(altanarea × 8,5 × 1,10)
    Exempel: 24 kvm × 8,5 × 1,10 = 224 lpm
    Monteringsnorm × kvm altanarea (normen räknas i kvm, materialet i lpm).
    Lägg under "Ytskikt & material".

[6] Trallskruvar — KRÄVS alltid.
    Använd "Trallskruv rostfri 5×55 (250st)" — räkna 1 förpackning per 3 kvm.
    Lägg under "Ytskikt & material".

[7] Avslutande fascia och kantbrädor — KRÄVS alltid.
    Använd "Träpanel 22×120 fingerskarvad gran" eller anpassat kantbräde.
    Räkna lpm = altanomkrets (2 × bredd + 2 × längd).
    Norm: "Snickeriarbete fascia och avslut" × lpm.
    Lägg under "Ytskikt & material".

[8] Räcke — KRÄVS om:
    - altan_height > 0,5 m (krav enligt BBR), ELLER
    - "räcke" nämns i beskrivningen eller build_params
    Räkna räckeslängd = railing (om angett) ELLER altanomkrets minus husväggen.
    Inkludera:
    - "Stolpe 90×90 tryckimpregnerat" × antal stolpar (var 1,2 m) → material
    - "Räckesspjäla furu 28×70 (set 10st)" × (räckeslängd / 1,2) → material
    - "Räckeshandledare 45×95" × lpm räcke → material
    - Norm "Räcke 1m hög med spjälor" × lpm räcke → arbete
    Lägg under "Stomme & konstruktion".

[9] Trappa — KRÄVS om "trappa" nämns ELLER om altan_height > 0,4 m
    och ingen befintlig trappa finns.
    Välj norm baserat på antal steg:
    - "Trapp 3 steg med vångstycke" × 1 post (om ≤ 3 steg)
    - "Trapp 5 steg med vångstycke" × 1 post (om 4–5 steg)
    Material: "Trall 28×145 furu impregnerad" för stegbrädor × lpm.
    Lägg under "Stomme & konstruktion".

[10] Frostskyddsmatta under altan — KRÄVS om altan_height < 0,4 m
     (krypgrund-risk) ELLER om ground_type nämner lera/fukt.
     Använd "Frostskyddsmatta under altan" × kvm altanarea.
     Lägg under "Stomme & konstruktion".

[11] [11] Markduk fiberduk — KRÄVS ALLTID utan undantag.
     Räkna antal rullar: CEIL(altanarea / 25).
     Använd "Markduk fiberduk 1×25m" × antal rullar.
     Typ: material. Lägg under "Stomme & konstruktion".
     DET ÄR INTE TILLÅTET ATT HOPPA ÖVER DENNA POST.
     
[12] Pergola/tak — KRÄVS om "pergola", "tak", "solskydd", "carport"
     nämns i beskrivningen.
     Norm: "Pergola tak inkl. reglar" × kvm.
     Material: "OSB-skiva 18mm 244×122" × antal skivor om tätt tak.
     Lägg under "Stomme & konstruktion".

[13] Skruv förbrukning och småvaror — KRÄVS alltid.
     Justeringsbeslag, vinkelbeslag, skruvar för stomme.
     Använd "Skruv förbrukning + småvaror" som fast post.
     Lägg under "Ytskikt & material".

[14] Jordborr — KRÄVS om Krinner-plintar används ELLER altan_height > 0,8 m.
     Använd "Jordborr Bokay till plintar" från equipment_rental × antal dagar.
     Lägg under "Hyrutrustning".

[15] Slutbesiktning med kund — KRÄVS alltid.
     Norm "Slutbesiktning med kund" × 1 post.
     Lägg under "Efterarbete".

[16] Plastning och skydd ytor — KRÄVS om altanen byggs intill hus
     med känsliga ytor (puts, panel, fönster nära).
     Norm "Plastning skydd ytor och inventarier" × lpm husfasad.
     Lägg under "Förberedelse".

[17] Resor och trängselskatt — backend räknar AUTOMATISKT.
     Skapa INGEN egen rad för resor eller trängselskatt.

MÅTTBERÄKNING — SÅ HÄR RÄKNAR DU:
Om altan_dimensions är angiven (B×L i meter):
  altanarea = B × L
  altanomkrets = 2 × (B + L)
Om INTE angiven men floor_sqm finns: använd floor_sqm som altanarea.
Redovisa alltid din beräkning i job_summary, t.ex.:
  "Altan 4,0 × 6,0 m = 24 kvm. Omk. 20 lpm. Höjd 0,8 m → räcke krävs."

MATERIALVAL — SNABBGUIDE:
  Standard + låg höjd (<0,5m): furu impregnerad + betongplintar
  Standard + medelhöjd (0,5–1,2m): furu impregnerad + Krinner
  Premium oavsett: komposit eller lärk + Krinner eller gjutna plintar
  Hög höjd (>1,2m): alltid gjutna plintar + bjälkar 45×195"""
# ═════════════════════════════════════════════════════════════════════
# Hämta prislådan från Supabase via RPC
# ═════════════════════════════════════════════════════════════════════
async def fetch_pricing_context(
    job_type: str,
    company_id: Optional[str] = None,
    quality: str = "standard",
    region: str = "default",
) -> dict:
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return _empty_pricing_context()

    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            r = await http.post(
                f"{SUPABASE_URL}/rest/v1/rpc/get_pricing_context",
                headers={
                    "apikey":        SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "Content-Type":  "application/json",
                },
                json={
                    "p_job_type":   job_type,
                    "p_company_id": company_id or None,
                    "p_quality":    quality,
                    "p_region":     region,
                },
            )
        if r.status_code == 200:
            data = r.json() or {}
            for key in ["work_norms", "material_prices", "subcontractor_prices",
                        "disposal_costs", "equipment_rental", "overhead_costs"]:
                if data.get(key) is None:
                    data[key] = []
            if data.get("regional") is None:
                data["regional"] = {"region": "default", "labor_factor": 1, "material_factor": 1, "ue_factor": 1, "congestion_per_day": 0}
            return data
    except Exception as e:
        print(f"fetch_pricing_context fel: {e}")

    return _empty_pricing_context()


def _empty_pricing_context() -> dict:
    return {
        "work_norms": [], "material_prices": [], "subcontractor_prices": [],
        "disposal_costs": [], "equipment_rental": [], "overhead_costs": [],
        "regional": {"region": "default", "labor_factor": 1, "material_factor": 1, "ue_factor": 1, "congestion_per_day": 0},
    }


# ═════════════════════════════════════════════════════════════════════
# Formatera prislådan som prompt-text
# ═════════════════════════════════════════════════════════════════════
def _format_pricing_for_prompt(ctx: dict) -> str:
    parts = ["\n\n═══════════════════════════════════════════════════════════════════"]
    parts.append("PRISLÅDA FÖR DETTA JOBB — ANVÄND DESSA EXAKT, GISSA ALDRIG PRISER")
    parts.append("═══════════════════════════════════════════════════════════════════")

    if ctx["work_norms"]:
        parts.append("\n── ARBETSTIDSNORMER (timmar per enhet) ──")
        for n in ctx["work_norms"]:
            scope_part = f" [{n['scope']}]" if n.get('scope') and n['scope'] != 'standard' else ""
            note_part  = f" ({n['notes']})" if n.get('notes') else ""
            parts.append(f"  id={n['id']} | {n['label']}{scope_part}: {n['hours_per']} h/{n['unit']}{note_part}")
    else:
        parts.append("\n── ARBETSTIDSNORMER: (inga normer i databasen för denna jobbtyp) ──")

    if ctx["material_prices"]:
        parts.append("\n── MATERIALPRISER (kr per enhet) ──")
        for m in ctx["material_prices"]:
            quality = f" [{m['quality_tier']}]" if m.get('quality_tier') else ""
            parts.append(f"  id={m['id']} | {m['label']}{quality}: {m['price']} kr/{m['unit']}")

    if ctx["subcontractor_prices"]:
        parts.append("\n── UNDERENTREPRENÖRSPRISER ──")
        for s in ctx["subcontractor_prices"]:
            parts.append(f"  id={s['id']} | [{s['trade']}/{s['scope']}] {s['description']}: {s['price']} kr/{s['unit']}")

    if ctx["disposal_costs"]:
        parts.append("\n── SOPHANTERING & DEPONI ──")
        for d in ctx["disposal_costs"]:
            cat = f" [{d['category']}]" if d.get('category') else ""
            parts.append(f"  id={d['id']} | {d['label']}{cat}: {d['price']} kr/{d['unit']}")

    if ctx["equipment_rental"]:
        parts.append("\n── HYRUTRUSTNING ──")
        for e in ctx["equipment_rental"]:
            parts.append(f"  id={e['id']} | {e['label']}: {e['price']} kr/{e['unit']}")

    if ctx["overhead_costs"]:
        parts.append("\n── ETABLERING, RESOR, FRAKT, TRÄNGSELSKATT ──")
        parts.append("  (backend lägger in dessa automatiskt — skapa INGA egna rader)")
        for o in ctx["overhead_costs"]:
            trigger = f" [endast om: {o['trigger_rule']}]" if o.get('trigger_rule') else ""
            parts.append(f"  id={o['id']} | {o['label']} ({o['calc_type']}): {o['rate']} {o['unit']}{trigger}")

    r = ctx["regional"]
    parts.append(f"\n── REGION: {r['region']} ──")
    parts.append(f"  Arbete-faktor: {r['labor_factor']}, Material-faktor: {r['material_factor']}, UE-faktor: {r['ue_factor']}")
    if float(r.get('congestion_per_day') or 0) > 0:
        parts.append(f"  Trängselskatt: {r['congestion_per_day']} kr/arbetsdag")

    parts.append("\n═══════════════════════════════════════════════════════════════════")
    parts.append("REGEL: source_id MÅSTE vara en av id:na ovan, eller \"ESTIMATED\" om du gissar.")
    parts.append("═══════════════════════════════════════════════════════════════════\n")

    return "\n".join(parts)


# ═════════════════════════════════════════════════════════════════════
# Adressanalys
# ═════════════════════════════════════════════════════════════════════
def _is_inside_stockholm_tolls(address: str) -> bool:
    if not address:
        return False
    addr = address.lower()
    pnr_match = re.search(r'\b(1[0-1][0-9])\s*\d{2}\b', addr)
    if pnr_match:
        pnr_prefix = int(pnr_match.group(1))
        if 100 <= pnr_prefix <= 119:
            return True
    inner_areas = [
        "vasastan", "östermalm", "ostermalm", "södermalm", "sodermalm",
        "kungsholmen", "norrmalm", "gamla stan", "djurgården", "djurgarden",
        "stockholm city", "stockholms innerstad",
    ]
    return any(area in addr for area in inner_areas)


def _is_inside_goteborg_tolls(address: str) -> bool:
    if not address:
        return False
    addr = address.lower()
    pnr_match = re.search(r'\b(41[0-9])\s*\d{2}\b', addr)
    if pnr_match:
        pnr_prefix = int(pnr_match.group(1))
        if 411 <= pnr_prefix <= 418:
            return True
    return False


def _detect_region_from_address(address: str) -> str:
    if not address:
        return "default"
    addr = address.lower()
    if any(k in addr for k in ["stockholm", "sollentuna", "solna", "danderyd", "lidingö", "huddinge", "nacka", "täby"]):
        return "stockholm"
    if "göteborg" in addr or "goteborg" in addr or "mölndal" in addr:
        return "goteborg"
    if "malmö" in addr or "malmo" in addr or "lund" in addr:
        return "malmo"
    if any(k in addr for k in ["umeå", "umea", "luleå", "lulea", "skellefteå", "östersund"]):
        return "norrland"
    return "default"


# ═════════════════════════════════════════════════════════════════════
# PDF-extrahering
# ═════════════════════════════════════════════════════════════════════
def _extract_pdf_text(b64_data: str) -> str:
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


# ═════════════════════════════════════════════════════════════════════
# Bygg user-text
# ═════════════════════════════════════════════════════════════════════
def _build_user_text(
    description: str,
    job_type: Optional[str],
    location: Optional[str],
    address: Optional[str],
    distance_km: Optional[float],
    work_days: Optional[int],
    quality: str,
    hourly_rate: float,
    include_rot: bool,
    margin_pct: float,
    ue_markup_pct: float,
    inside_tolls: Optional[str],
    build_params: Optional[Dict[str, str]],
    documents: Optional[List],
) -> str:
    parts = []

    parts.append(f"Jobbeskrivning: {description}")
    if job_type:
        parts.append(f"Jobbtyp: {job_type}")
    parts.append(f"Kvalitetsnivå: {quality}")
    if location:
        parts.append(f"Plats (region): {location}")
    if address:
        parts.append(f"Adress: {address}")
    if distance_km:
        parts.append(f"Avstånd t/r: {distance_km} km enkel väg = {distance_km*2} km totalt per resedag")
    if work_days:
        parts.append(f"Uppskattat antal arbetsdagar: {work_days}")
    if inside_tolls:
        parts.append(f"VIKTIGT: Adressen är innanför {inside_tolls.upper()}S TULLAR — lägg till trängselskatt per arbetsdag")

    parts.append(f"Timpris (eget arbete): {hourly_rate} kr/h")
    parts.append(f"Påslag eget arbete + material: {margin_pct}%")
    parts.append(f"Påslag underentreprenörer: {ue_markup_pct}%")
    parts.append(f"ROT-avdrag: {'Ja (30% på arbete inkl. UE-arbetsandel)' if include_rot else 'Nej'}")

    if build_params:
        LABELS = {
            "room_dimensions":   "Rumsmått (B×L)",
            "facade_area":       "Fasadarea",
            "facade_height":     "Fasadhöjd",
            "perimeter":         "Husomkrets",
            "windows":           "Antal fönster",
            "doors":             "Antal dörrar",
            "floor_sqm":         "Golvyta",
            "ceiling_height":    "Takhöjd",
            "altan_dimensions":  "Altanmått (B×L)",
            "altan_height":      "Höjd över mark",
            "railing":           "Räcke (lpm)",
            "stairs":            "Trappa (antal steg)",
            "ground_type":       "Markförhållanden",
            "rivning_scope":     "Vad som ska rivas",
            "demolition_volume": "Uppskattad rivningsvolym (kbm)",
            "build_year":        "Byggår",
            "ingår_i_jobbet":    "Ingår i jobbet",
            "extra":             "Övrigt",
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
                doc_blocks.append(f"--- PDF-UNDERLAG: {name} ---\n{extracted[:6000]}\n--- SLUT PDF ---")
            else:
                doc_blocks.append(f"[Bifogad fil: {name}]")
        parts.append("\nBifogade underlag (läs och basera kalkylen på innehållet):\n" + "\n\n".join(doc_blocks))

    return "\n".join(parts)


# ═════════════════════════════════════════════════════════════════════
# Deterministisk omräkning + automatiska overhead-rader
# ═════════════════════════════════════════════════════════════════════
def _apply_overhead_rules(
    data: dict,
    pricing_ctx: dict,
    distance_km: Optional[float],
    work_days: Optional[int],
    inside_tolls: Optional[str],
    job_type: str,
) -> None:
    """
    Lägger till overhead-rader deterministiskt från overhead_costs-tabellen.
    AI:ns eventuella "Etablering & resa"-kategori tas bort och ersätts helt.
    """
    overhead_rows = []

    # Räkna materialsumma för frakt-trigger
    material_total = 0.0
    for cat in data.get("categories", []):
        for row in cat.get("rows", []):
            if row.get("type") == "material":
                qty   = float(row.get("quantity", 0) or 0)
                price = float(row.get("unit_price", 0) or 0)
                material_total += qty * price

    for o in pricing_ctx.get("overhead_costs", []):
        calc    = o.get("calc_type")
        rate    = float(o.get("rate", 0))
        trigger = o.get("trigger_rule") or ""

        # Resor
        if calc == "per_km_round_trip" and distance_km and work_days:
            total = round(distance_km * 2 * rate * work_days)
            overhead_rows.append({
                "description": o["label"],
                "note":        f"{distance_km}km × 2 × {rate}kr × {work_days} resedagar",
                "unit":        "km",
                "quantity":    1,
                "unit_price":  total,
                "total":       total,
                "type":        "overhead",
                "source_id":   o["id"],
            })

        # Trängselskatt
        elif calc == "congestion_per_workday" and work_days:
            if inside_tolls and trigger == f"inside_tolls={inside_tolls}":
                total = round(rate * work_days)
                overhead_rows.append({
                    "description": o["label"],
                    "note":        f"{rate}kr × {work_days} arbetsdagar innanför tullarna",
                    "unit":        "dag",
                    "quantity":    work_days,
                    "unit_price":  round(rate),
                    "total":       total,
                    "type":        "overhead",
                    "source_id":   o["id"],
                })

        # Etablering / avetablering / fasta poster
        elif calc == "flat":
            include = False
            if not trigger:
                include = True
            elif trigger == "fasad OR material_total>15000":
                include = (job_type == "fasad") or (material_total > 15000)
            if include:
                total = round(rate)
                overhead_rows.append({
                    "description": o["label"],
                    "note":        o.get("notes") or "",
                    "unit":        "post",
                    "quantity":    1,
                    "unit_price":  total,
                    "total":       total,
                    "type":        "overhead",
                    "source_id":   o["id"],
                })

    if overhead_rows:
        # Ta bort AI:ns etableringsrader helt — backend har alltid sista ordet
        data["categories"] = [
            c for c in data.get("categories", [])
            if c.get("name") != "Etablering & resa"
        ]
        # Lägg in backends deterministiska version som första kategori
        data.setdefault("categories", []).insert(0, {
            "name":     "Etablering & resa",
            "rows":     overhead_rows,
            "subtotal": sum(r["total"] for r in overhead_rows),
        })


# ═════════════════════════════════════════════════════════════════════
# Räkna om work_norms-rader till kronor
# ═════════════════════════════════════════════════════════════════════
def _apply_work_norms_pricing(
    data: dict,
    pricing_ctx: dict,
    hourly_rate: float,
) -> None:
    """
    Räknar om unit_price för alla rader vars source_id pekar på en work_norms-rad.
    work_norms lagrar hours_per (timmar per enhet), INTE kronor.
    Denna funktion är sanningen: source_id → hours_per × hourly_rate.
    """
    norms_by_id: Dict[str, float] = {}
    for n in pricing_ctx.get("work_norms", []) or []:
        nid = str(n.get("id") or "")
        if nid:
            try:
                norms_by_id[nid] = float(n.get("hours_per") or 0)
            except (TypeError, ValueError):
                continue

    if not norms_by_id:
        return

    rate = float(hourly_rate or 650)
    corrections = []

    for cat in data.get("categories", []) or []:
        for row in cat.get("rows", []) or []:
            source_id = str(row.get("source_id") or "")
            if not source_id or source_id == "ESTIMATED":
                continue

            hours_per = norms_by_id.get(source_id)
            if hours_per is None:
                continue

            correct_unit_price = round(hours_per * rate)
            ai_unit_price = float(row.get("unit_price") or 0)

            row["unit_price"] = correct_unit_price
            if row.get("type") not in ("labor", None, ""):
                pass
            else:
                row["type"] = "labor"

            if abs(correct_unit_price - ai_unit_price) > 1:
                corrections.append({
                    "description":        row.get("description", ""),
                    "source_id":          source_id,
                    "ai_unit_price":      round(ai_unit_price),
                    "correct_unit_price": correct_unit_price,
                    "hours_per":          hours_per,
                    "hourly_rate":        rate,
                })

    if corrections:
        data.setdefault("corrections", []).extend(corrections)


# ═════════════════════════════════════════════════════════════════════
# Deterministisk totalkalkyl
# ═════════════════════════════════════════════════════════════════════
def recalculate_totals(
    data: dict,
    hourly_rate: float,
    margin_pct: float,
    include_rot: bool,
    ue_markup_pct: float = 12.5,
) -> dict:
    """Räknar alltid om totals deterministiskt. Litar aldrig på AI:ns aritmetik."""
    material_total      = 0.0
    labor_total         = 0.0
    equipment_total     = 0.0
    subcontractor_total = 0.0
    disposal_total      = 0.0
    overhead_total      = 0.0

    for cat in data.get("categories", []):
        cat_subtotal = 0.0
        for row in cat.get("rows", []):
            qty   = float(row.get("quantity", 0) or 0)
            price = float(row.get("unit_price", 0) or 0)
            total = round(qty * price)
            row["total"] = total
            cat_subtotal += total

            t = row.get("type", "labor")
            if   t == "material":      material_total      += total
            elif t == "equipment":     equipment_total     += total
            elif t == "subcontractor": subcontractor_total += total
            elif t == "disposal":      disposal_total      += total
            elif t == "overhead":      overhead_total      += total
            else:                      labor_total         += total

        cat["subtotal"] = round(cat_subtotal)

    margin_pct_val    = float(margin_pct or 15)
    ue_markup_pct_val = float(ue_markup_pct or 12.5)

    own_subtotal    = material_total + labor_total + equipment_total + disposal_total + overhead_total
    own_margin      = round(own_subtotal * margin_pct_val / 100)
    ue_markup       = round(subcontractor_total * ue_markup_pct_val / 100)
    subtotal_ex_vat = round(own_subtotal + own_margin + subcontractor_total + ue_markup)
    vat             = round(subtotal_ex_vat * 0.25)
    total_inc_vat   = round(subtotal_ex_vat + vat)

    if include_rot:
        ue_labor_part = subcontractor_total * UE_LABOR_SHARE
        rot_base      = labor_total + ue_labor_part
        rot_deduction = round(rot_base * 0.30)
    else:
        rot_deduction = 0

    customer_pays = round(total_inc_vat - rot_deduction)

    data["totals"] = {
        "material_total":      round(material_total),
        "labor_total":         round(labor_total),
        "equipment_total":     round(equipment_total),
        "subcontractor_total": round(subcontractor_total),
        "disposal_total":      round(disposal_total),
        "overhead_total":      round(overhead_total),
        "own_margin":          own_margin,
        "ue_markup":           ue_markup,
        "margin_amount":       own_margin + ue_markup,
        "subtotal":            round(own_subtotal + subcontractor_total),
        "subtotal_ex_vat":     subtotal_ex_vat,
        "total_ex_vat":        subtotal_ex_vat,
        "vat":                 vat,
        "total_inc_vat":       total_inc_vat,
        "rot_deduction":       rot_deduction,
        "customer_pays":       customer_pays,
    }
    data["meta"] = {
        **data.get("meta", {}),
        "hourly_rate":   float(hourly_rate or 650),
        "margin_pct":    margin_pct_val,
        "ue_markup_pct": ue_markup_pct_val,
        "rot_applied":   include_rot,
    }
    return data


# ═════════════════════════════════════════════════════════════════════
# Spårbarhet: bygg en debug-snapshot per offert
# ═════════════════════════════════════════════════════════════════════
def _build_pricing_snapshot(data: dict, pricing_ctx: dict) -> dict:
    id_to_source = {}
    for n in pricing_ctx.get("work_norms", []):
        id_to_source[str(n["id"])] = {"table": "work_norms", **n}
    for m in pricing_ctx.get("material_prices", []):
        id_to_source[str(m["id"])] = {"table": "material_prices", **m}
    for s in pricing_ctx.get("subcontractor_prices", []):
        id_to_source[str(s["id"])] = {"table": "subcontractor_prices", **s}
    for d in pricing_ctx.get("disposal_costs", []):
        id_to_source[str(d["id"])] = {"table": "disposal_costs", **d}
    for e in pricing_ctx.get("equipment_rental", []):
        id_to_source[str(e["id"])] = {"table": "equipment_rental", **e}
    for o in pricing_ctx.get("overhead_costs", []):
        id_to_source[str(o["id"])] = {"table": "overhead_costs", **o}

    snapshot_rows = []
    estimated_count = 0
    matched_count   = 0

    for cat in data.get("categories", []):
        for row in cat.get("rows", []):
            sid = row.get("source_id", "")
            if sid == "ESTIMATED" or not sid:
                estimated_count += 1
                snapshot_rows.append({
                    "row_description": row.get("description"),
                    "row_total":       row.get("total"),
                    "source_id":       sid or "MISSING",
                    "source":          None,
                    "category":        cat.get("name"),
                })
            elif sid in id_to_source:
                matched_count += 1
                snapshot_rows.append({
                    "row_description": row.get("description"),
                    "row_total":       row.get("total"),
                    "source_id":       sid,
                    "source":          id_to_source[sid],
                    "category":        cat.get("name"),
                })

    return {
        "rows":            snapshot_rows,
        "matched_count":   matched_count,
        "estimated_count": estimated_count,
        "match_pct":       round(100 * matched_count / max(1, matched_count + estimated_count)),
    }


# ═════════════════════════════════════════════════════════════════════
# Huvudfunktion
# ═════════════════════════════════════════════════════════════════════
async def generate_estimate(
    description: str,
    job_type: Optional[str] = None,
    area_sqm: Optional[float] = None,
    location: Optional[str] = None,
    address: Optional[str] = None,
    distance_km: Optional[float] = None,
    work_days: Optional[int] = None,
    quality: str = "standard",
    hourly_rate: float = 650,
    include_rot: bool = True,
    margin_pct: float = 15,
    ue_markup_pct: float = 12.5,
    build_params: Optional[Dict[str, str]] = None,
    images: Optional[List] = None,
    documents: Optional[List] = None,
    company_id: Optional[str] = None,
    **kwargs,
) -> dict:

    # ── 1. Härled region ──
    if address:
        region = _detect_region_from_address(address)
    else:
        region = _detect_region_from_address(location or "") or "default"

    # ── 2. Avgör innanför tullar ──
    inside_tolls: Optional[str] = None
    if address:
        if _is_inside_stockholm_tolls(address):
            inside_tolls = "stockholm"
        elif _is_inside_goteborg_tolls(address):
            inside_tolls = "goteborg"

    # ── 3. Hämta prislådan ──
    pricing_ctx = await fetch_pricing_context(
        job_type=job_type or "ovrigt",
        company_id=company_id,
        quality=quality,
        region=region,
    )

    # ── 4. Bygg system-prompt med jobbtypsspecifik checklista ──
    system = SYSTEM_PROMPT_BASE
    jt = (job_type or "").lower()
    if jt == "rivning":
        system += RIVNING_CHECKLIST
    elif jt == "fasad":
        system += FASAD_CHECKLIST
    elif jt == "altan":
        system += ALTAN_CHECKLIST
    system += _format_pricing_for_prompt(pricing_ctx)

    # ── 5. Bygg user-text ──
    user_text = _build_user_text(
        description=description,
        job_type=job_type,
        location=location,
        address=address,
        distance_km=distance_km,
        work_days=work_days,
        quality=quality,
        hourly_rate=hourly_rate or 650,
        include_rot=include_rot,
        margin_pct=margin_pct or 15,
        ue_markup_pct=ue_markup_pct or 12.5,
        inside_tolls=inside_tolls,
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
                is_drawing = name.startswith("[RITNING]")
                detail = "high" if is_drawing else "low"
                content_parts.append({
                    "type":      "image_url",
                    "image_url": {"url": data, "detail": detail},
                })
        names = [
            (img.name if hasattr(img, "name") else img.get("name", "bild"))
            for img in all_images[:8]
        ]
        content_parts[0]["text"] += f"\n\nBifogade bilder ({len(names)} st): {', '.join(names)}"
        messages.append({"role": "user", "content": content_parts})
    else:
        messages.append({"role": "user", "content": user_text})

    # ── 6. Anropa OpenAI ──
    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.2,
            max_tokens=4500,
            response_format={"type": "json_object"},
        )
        raw  = response.choices[0].message.content or "{}"
        data = json.loads(raw)

        # ── 7. Backend lägger till deterministiska overhead-rader ──
        _apply_overhead_rules(data, pricing_ctx, distance_km, work_days, inside_tolls, job_type or "ovrigt")

        # ── 7b. Räkna om work_norms-rader till kronor ──
        _apply_work_norms_pricing(data, pricing_ctx, hourly_rate or 650)

        # ── 8. Räkna om totals ──
        data = recalculate_totals(
            data,
            hourly_rate=hourly_rate or 650,
            margin_pct=margin_pct or 15,
            include_rot=include_rot,
            ue_markup_pct=ue_markup_pct or 12.5,
        )

        # ── 9. Bygg pricing snapshot ──
        data["pricing_snapshot"] = _build_pricing_snapshot(data, pricing_ctx)

        # ── 10. Metadata ──
        data["meta"] = {
            **data.get("meta", {}),
            "region":       region,
            "address":      address,
            "inside_tolls": inside_tolls,
            "distance_km":  distance_km,
            "work_days":    work_days,
            "quality":      quality,
        }

        return data

    except json.JSONDecodeError as e:
        raise ValueError(f"AI returnerade ogiltig JSON: {e}")
    except Exception as e:
        raise ValueError(f"OpenAI-anrop misslyckades: {e}")


# ═════════════════════════════════════════════════════════════════════
# Chat
# ═════════════════════════════════════════════════════════════════════
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


# ═════════════════════════════════════════════════════════════════════
# Bakåtkompatibilitet
# ═════════════════════════════════════════════════════════════════════
async def fetch_norms(job_type: str, house_age: str = "all") -> str:
    ctx = await fetch_pricing_context(job_type=job_type, quality="standard", region="default")
    norms = ctx.get("work_norms", [])
    if not norms:
        return ""
    lines = [f"\nARBETSTIDSNORMER FÖR {job_type.upper()}:"]
    for n in norms:
        lines.append(f"  {n['label']}: {n['hours_per']} timmar per {n['unit']}")
    return "\n".join(lines)

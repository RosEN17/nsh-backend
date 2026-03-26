import json
from app.core.config import OPENAI_API_KEY

def _get_client():
    from openai import OpenAI
    return OpenAI(api_key=OPENAI_API_KEY)

def ask_ai(question: str, pack) -> str:
    try:
        top_mom = (
            pack.top_mom.head(5).to_string(index=False)
            if pack.top_mom is not None and not pack.top_mom.empty
            else "No MoM drivers available"
        )
        top_budget = (
            pack.top_budget.head(5).to_string(index=False)
            if pack.top_budget is not None and not pack.top_budget.empty
            else "No budget drivers available"
        )

        prompt = f"""
You are a financial controller assistant.
Current period: {pack.current_period}
Previous period: {pack.previous_period}
Top MoM drivers:
{top_mom}

Top Budget drivers:
{top_budget}

User question:
{question}

Answer like a sharp financial controller:
- be concise
- explain the main drivers
- mention business impact
- avoid generic fluff
"""
        client = _get_client()
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": "You are an elite finance controller AI copilot."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        return response.choices[0].message.content or "Tomt svar från AI."
    except Exception as e:
        return f"AI error: {str(e)}"

def generate_ai_report_draft(pack, report_type, audience, tone, sections, report_items):
    try:
        top_mom = (
            pack.top_mom.head(5).to_string(index=False)
            if pack.top_mom is not None and not pack.top_mom.empty
            else "No MoM drivers available"
        )
        top_budget = (
            pack.top_budget.head(5).to_string(index=False)
            if pack.top_budget is not None and not pack.top_budget.empty
            else "No budget drivers available"
        )
        saved_points = []
        for item in report_items[:12]:
            txt = (item.get("text") or "").strip()
            if txt:
                saved_points.append(f"- {txt}")

        saved_points_txt = "\n".join(saved_points) if saved_points else "No saved report points"
        section_list = ", ".join(sections) if sections else "No sections selected"

        prompt = f"""
You are an elite financial reporting copilot.
Create a finance-ready draft report.

Context:
- Report type: {report_type}
- Audience: {audience}
- Tone: {tone}
- Included sections: {section_list}
- Current period: {pack.current_period}
- Previous period: {pack.previous_period}

Top MoM drivers:
{top_mom}

Top budget drivers:
{top_budget}

Saved controller notes:
{saved_points_txt}

Instructions:
- Write one section at a time
- Only include selected sections
- Be specific and financially grounded
- Avoid generic fluff
- Keep language clear, sharp and executive-friendly
- Do NOT write long text blocks
- Each section must contain 3 to 5 short bullet points
- Each bullet should be one sentence only
- Return valid JSON only
"""
        client = _get_client()
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are an elite finance reporting assistant."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
        )

        raw = response.choices[0].message.content.strip()
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

        return {"executive_summary": [raw]}
    except Exception as e:
        return {"executive_summary": [f"AI error: {str(e)}"]}
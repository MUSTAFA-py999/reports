import os
import asyncio
import threading
import logging
import html as html_lib
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler,
    MessageHandler, CallbackQueryHandler, filters
)
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field
from typing import List, Optional
from io import BytesIO
from weasyprint import HTML as WeasyHTML

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ Smart University Reports Bot v4.0"

@flask_app.route('/health')
def health():
    return {"status": "healthy", "version": "4.0"}, 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)


# ═══════════════════════════════════════════════════
# QUEUE SYSTEM
# ═══════════════════════════════════════════════════
report_queue: asyncio.Queue = None
active_jobs = {}          # user_id → True  (currently generating)
queue_positions = {}      # user_id → position in queue
MAX_CONCURRENT = 2        # عدد التقارير التي تُعالج في نفس الوقت


async def queue_worker(app):
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def process_one(user_id, session, msg_id):
        async with semaphore:
            active_jobs[user_id] = True
            # Update queue positions for waiting users
            for uid in list(queue_positions.keys()):
                if queue_positions[uid] > 0:
                    queue_positions[uid] -= 1

            try:
                loop = asyncio.get_event_loop()
                pdf_bytes, title = await loop.run_in_executor(
                    None, generate_report, session
                )

                lang      = session.get("language", "ar")
                lang_name = LANGUAGES[lang]["name"]
                depth     = session.get("depth", "medium")
                depth_name = DEPTH_OPTIONS[depth]["name"]
                tpl       = session.get("template", "classic")
                tpl_name  = TEMPLATES[tpl]["name"]

                if pdf_bytes:
                    safe_name  = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in title[:40])
                    safe_title = title.replace('<','&lt;').replace('>','&gt;').replace('&','&amp;')
                    caption = (
                        f"✅ <b>تقريرك جاهز يا طالبنا!</b>\n\n"
                        f"📄 <b>{safe_title}</b>\n"
                        f"🌐 {lang_name}  |  📏 {depth_name}  |  🎨 {tpl_name}\n\n"
                        f"🔄 أرسل موضوعاً جديداً لتقرير آخر!"
                    )
                    await app.bot.send_document(
                        chat_id=user_id,
                        document=BytesIO(pdf_bytes),
                        filename=f"{safe_name}.pdf",
                        caption=caption,
                        parse_mode='HTML'
                    )
                    try:
                        await app.bot.delete_message(chat_id=user_id, message_id=msg_id)
                    except Exception:
                        pass
                    logger.info(f"✅ Report sent to {user_id}")
                else:
                    err = str(title).replace('<','&lt;').replace('>','&gt;').replace('&','&amp;')
                    await app.bot.send_message(
                        chat_id=user_id,
                        text=f"❌ <b>فشل إنشاء التقرير:</b>\n{err[:300]}\n\n🔄 أرسل موضوعاً جديداً للمحاولة مجدداً.",
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"Queue worker error for {user_id}: {e}", exc_info=True)
                err = str(e)[:200].replace('<','&lt;').replace('>','&gt;').replace('&','&amp;')
                await app.bot.send_message(
                    chat_id=user_id,
                    text=f"❌ <b>خطأ غير متوقع:</b>\n<code>{err}</code>\n\n🔄 أرسل موضوعاً جديداً.",
                    parse_mode='HTML'
                )
            finally:
                active_jobs.pop(user_id, None)
                queue_positions.pop(user_id, None)
                user_sessions.pop(user_id, None)

    while True:
        item = await report_queue.get()
        user_id, session, msg_id = item
        asyncio.create_task(process_one(user_id, session, msg_id))
        report_queue.task_done()


# ═══════════════════════════════════════════════════
# PYDANTIC MODELS
# ═══════════════════════════════════════════════════
class SmartQuestions(BaseModel):
    questions: List[str] = Field(
        description=(
            "List of open-ended questions (between 2 and 5, based on topic complexity) "
            "to ask the student about their report. Decide the number based on how much "
            "clarification the topic needs."
        )
    )

class ReportBlock(BaseModel):
    block_type: str = Field(
        description=(
            "Block type — must be ONE of: "
            "'paragraph', 'bullets', 'numbered_list', 'table', "
            "'pros_cons', 'comparison', 'stats', 'examples', 'quote'"
        )
    )
    title: str = Field(description="Section heading")
    text: Optional[str] = Field(default=None)
    items: Optional[List[str]] = Field(default=None)
    pros: Optional[List[str]] = Field(default=None)
    cons: Optional[List[str]] = Field(default=None)
    headers: Optional[List[str]] = Field(default=None)
    rows: Optional[List[List[str]]] = Field(default=None)
    side_a: Optional[str] = Field(default=None)
    side_b: Optional[str] = Field(default=None)
    criteria: Optional[List[str]] = Field(default=None)
    side_a_values: Optional[List[str]] = Field(default=None)
    side_b_values: Optional[List[str]] = Field(default=None)

class DynamicReport(BaseModel):
    title: str = Field(description="Report title")
    introduction: str = Field(description="Short introduction: 2-3 sentences MAX. Simple and direct. No filler phrases.")
    blocks: List[ReportBlock] = Field(description="Content blocks")
    conclusion: str = Field(description="Conclusion: 2-4 sentences. Concrete takeaway. MANDATORY.")


# ═══════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════
user_sessions = {}

LANGUAGES = {
    "ar": {
        "name": "🇸🇦 العربية",
        "dir": "rtl", "align": "right", "lang_attr": "ar",
        "font": "'Traditional Arabic', 'Arial', sans-serif",
        "intro_label": "المقدمة",
        "conclusion_label": "الخاتمة",
        "pros_label": "✅ المزايا",
        "cons_label": "❌ العيوب",
        "instruction": "Write ALL content in formal Arabic (فصحى). Every word must be Arabic.",
        "q_prompt": (
            "أنت مساعد أكاديمي لطلاب الجامعة.\n"
            "الطالب يريد تقريراً عن: \"{topic}\".\n\n"
            "اكتب بالعربية 2-4 أسئلة قصيرة ومباشرة لتحديد ما يريده الطالب في تقريره.\n"
            "قواعد الأسئلة:\n"
            "- قصيرة (جملة واحدة فقط لكل سؤال)\n"
            "- مباشرة ومحددة\n"
            "- موضوعات بسيطة: 2 أسئلة — معقدة: 3-4 أسئلة\n"
        ),
        "answer_prompt": "اكتب التقرير باللغة العربية الفصحى بالكامل. كل كلمة يجب أن تكون عربية.",
    },
    "en": {
        "name": "🇬🇧 English",
        "dir": "ltr", "align": "left", "lang_attr": "en",
        "font": "'Arial', 'Helvetica', sans-serif",
        "intro_label": "Introduction",
        "conclusion_label": "Conclusion",
        "pros_label": "✅ Pros",
        "cons_label": "❌ Cons",
        "instruction": "Write ALL content in English. Every word must be English.",
        # ✅ الأسئلة دائماً بالعربية حتى للتقارير الإنجليزية
        "q_prompt": (
            "أنت مساعد أكاديمي لطلاب الجامعة.\n"
            "الطالب يريد تقريراً إنجليزياً عن: \"{topic}\".\n\n"
            "اكتب بالعربية 2-4 أسئلة قصيرة ومباشرة لتحديد ما يريده الطالب في تقريره.\n"
            "قواعد الأسئلة:\n"
            "- قصيرة (جملة واحدة فقط لكل سؤال)\n"
            "- مباشرة ومحددة\n"
            "- موضوعات بسيطة: 2 أسئلة — معقدة: 3-4 أسئلة\n"
        ),
        "answer_prompt": "Write the entire report in English. Every word must be English.",
    },
}

TEMPLATES = {
    "classic":      {"name": "🎓 كلاسيكي",   "primary": "#2c3e50", "accent": "#3498db", "bg": "#ecf0f1", "bg2": "#f8f9fa"},
    "modern":       {"name": "🚀 عصري",      "primary": "#5a67d8", "accent": "#667eea", "bg": "#ebf4ff", "bg2": "#ffffff"},
    "minimal":      {"name": "⚪ بسيط",      "primary": "#2d3748", "accent": "#718096", "bg": "#f7fafc", "bg2": "#ffffff"},
    "professional": {"name": "💼 احترافي",   "primary": "#1a365d", "accent": "#2b6cb0", "bg": "#bee3f8", "bg2": "#f0f4ff"},
    "dark_elegant": {"name": "🖤 أنيق داكن", "primary": "#d4af37", "accent": "#f6d860", "bg": "#2d3748", "bg2": "#4a5568"},
}

DEPTH_OPTIONS = {
    "short":    {"name": "📝 مختصر ",  "blocks": 3, "words": "200-300"},
    "medium":   {"name": "📄 متوسط ",  "blocks": 4, "words": "320-410"},
    "detailed": {"name": "📚 مفصل ",   "blocks": 5, "words": "420-540"},
}

# رسائل التوجيه لكل حالة عندما يرسل المستخدم نصاً بدل استخدام الأزرار
STATE_GUIDANCE = {
    "choosing_lang":        "🌐 من فضلك <b>اختر اللغة</b> من الأزرار أعلاه.",
    "generating_questions": "⏳ جاري تحليل موضوعك وتوليد الأسئلة... انتظر قليلاً.",
    "choosing_title":       "📌 من فضلك <b>اكتب عنوان التقرير</b> أو اضغط الزر أعلاه لتركه للذكاء الاصطناعي.",
    "choosing_depth":       "📏 من فضلك <b>اختر عمق التقرير</b> من الأزرار أعلاه.",
    "choosing_template":    "🎨 من فضلك <b>اختر تصميم التقرير</b> من الأزرار أعلاه.",
    "in_queue":             "⏳ تقريرك في الطابور، انتظر حتى يكتمل.\nأرسل /cancel لإلغاء الطلب.",
}


# ═══════════════════════════════════════════════════
# LLM HELPERS
# ═══════════════════════════════════════════════════
def get_llm():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise Exception("GOOGLE_API_KEY not set")
    return ChatGoogleGenerativeAI(
        # ✅ FIX 1: اسم النموذج الصحيح
        model="gemini-2.5-flash",
        temperature=0.5,
        google_api_key=api_key,
        max_retries=3
    )


def generate_dynamic_questions(topic: str, language_key: str) -> List[str]:
    lang   = LANGUAGES[language_key]
    llm    = get_llm()
    parser = PydanticOutputParser(pydantic_object=SmartQuestions)
    prompt = (
        lang["q_prompt"].format(topic=topic)
        + "\n\n"
        + parser.get_format_instructions()
    )
    result = llm.invoke([HumanMessage(content=prompt)])
    parsed = parser.parse(result.content)
    # ✅ FIX 2: لا نقيّد الأسئلة بـ 3، بل نحترم ما يقرره النموذج (2-5)
    return parsed.questions[:5]


def build_report_prompt(session: dict, format_instructions: str) -> str:
    topic       = session["topic"]
    lang_key    = session.get("language", "ar")
    depth       = session.get("depth", "medium")
    lang        = LANGUAGES[lang_key]
    d           = DEPTH_OPTIONS[depth]
    questions   = session.get("dynamic_questions", [])
    answers     = session.get("answers", [])
    custom_title = session.get("custom_title")

    title_instruction = (
        f'TITLE: Use EXACTLY this title: "{custom_title}" — do not change it.'
        if custom_title else
        "TITLE: Generate a concise, academic title that fits the topic and student requirements."
    )

    qa_block = ""
    for i, (q, a) in enumerate(zip(questions, answers), 1):
        qa_block += f"Q{i}: {q}\nA{i}: {a}\n\n"

    return f"""You are a skilled academic writer. Your goal is to write a university report that feels HUMAN-WRITTEN — not AI-generated.

══════════════════════════════════════
TOPIC: {topic}
LANGUAGE: {lang["instruction"]}
DEPTH: Exactly {d["blocks"]} content blocks.
{title_instruction}
══════════════════════════════════════

STUDENT'S REQUIREMENTS:
{qa_block.strip()}

══════════════════════════════════════
BLOCK TYPES:
- "paragraph"     → "text": flowing prose (use \\n to break mid-thought and start fresh line — varies rhythm)
- "bullets"       → "items": 4-6 items. Each item can contain a sub-note using " — " like: "Main point — short clarifying detail here"
- "numbered_list" → "items": 4-6 steps. Same sub-note style allowed.
- "table"         → "headers" + "rows" (4-5 rows)
- "pros_cons"     → "pros" + "cons" (3-5 each). Sub-notes allowed with " — "
- "comparison"    → "side_a", "side_b", "criteria", "side_a_values", "side_b_values"
- "stats"         → "items": "Label: value — brief context" (4-5 items)
- "examples"      → "items": 4-5 real examples with " — " sub-note
- "quote"         → "text": a sharp definition or key insight

══════════════════════════════════════
WRITING STYLE — CRITICAL RULES:

1. INTRODUCTION: 2-3 sentences only. Direct. No "يُعدّ هذا الموضوع من أهم..." filler.

2. SUB-BULLETS: Actively use " — " inside bullet/numbered/pros_cons items to embed short inline notes.
   Example: "الذكاء الاصطناعي التوليدي — يشمل النماذج اللغوية الكبيرة وأدوات إنشاء الصور"

3. LINE BREAKS FOR RHYTHM: In paragraph "text" fields, use \\n to end a thought mid-line and start the next on a new line.
   This creates breathing room and avoids walls of text. Use 2-4 breaks per paragraph block.

4. HUMAN WRITING PATTERNS — avoid AI tells:
   • Vary sentence length: mix short punchy sentences with longer analytical ones
   • NO formulaic openers like "يتناول هذا التقرير..." or "In this report, we will..."
   • NO symmetrical lists where every bullet is exactly the same length
   • Use occasional rhetorical questions or direct statements mid-section
   • Conclusions should feel like a genuine takeaway, not a summary of what was just said
   • Avoid starting every paragraph with the section title rephrased

5. BLOCK SELECTION: match content to block type naturally:
   • Comparisons → "comparison" or "pros_cons"
   • Processes → "numbered_list"
   • Data/numbers → "stats" or "table"
   • Analysis/opinion → "paragraph" with line breaks
   • Feature lists → "bullets" with sub-notes

6. ALL text in specified language. conclusion is MANDATORY.

{format_instructions}"""


def generate_report(session: dict):
    try:
        llm    = get_llm()
        parser = PydanticOutputParser(pydantic_object=DynamicReport)
        prompt = build_report_prompt(session, parser.get_format_instructions())

        report = None
        for attempt in range(3):
            try:
                result = llm.invoke([HumanMessage(content=prompt)])
                report = parser.parse(result.content)
                break
            except Exception as e:
                if attempt == 2:
                    raise e
                logger.warning(f"Parse attempt {attempt+1} failed: {e}")

        html_str  = render_html(report, session.get("template", "classic"), session.get("language", "ar"))
        pdf_bytes = WeasyHTML(string=html_str).write_pdf()
        return pdf_bytes, report.title

    except Exception as e:
        logger.error(f"❌ generate_report: {e}", exc_info=True)
        return None, str(e)


# ═══════════════════════════════════════════════════
# HTML RENDERER
# ═══════════════════════════════════════════════════
def esc(v):
    return html_lib.escape(str(v)) if v is not None else ""

def render_item_with_subnote(item: str, txt_color: str, accent: str) -> str:
    """Renders a list item, styling the sub-note after ' — ' in a muted smaller font."""
    sep = " — "
    if sep in str(item):
        parts = str(item).split(sep, 1)
        main = esc(parts[0].strip())
        note = esc(parts[1].strip())
        return (
            f'{main}'
            f'<span style="color:{accent};font-size:0.88em;font-weight:normal;"> — {note}</span>'
        )
    return esc(item)

def text_to_paras(text: str, align: str) -> str:
    lines = [l.strip() for l in str(text).split('\n') if l.strip()]
    if not lines:
        lines = [str(text)]
    return "".join(
        f'<p style="text-align:{align};margin:0 0 10px 0;line-height:1.95;">{esc(l)}</p>'
        for l in lines
    )

def render_block(b: ReportBlock, tc: dict, lang: dict) -> str:
    p   = tc["primary"]
    a   = tc["accent"]
    bg  = tc["bg"]
    bg2 = tc["bg2"]
    align  = lang["align"]
    is_rtl = lang["dir"] == "rtl"
    b_side = "border-right" if is_rtl else "border-left"
    p_side = "padding-right" if is_rtl else "padding-left"
    is_dark   = tc["primary"] == "#d4af37"
    txt_color = "#e2e8f0" if is_dark else "#333333"
    h2_bg     = "#3d4a5c" if is_dark else bg

    h2 = (
        f'<h2 style="color:{p};font-size:15px;font-weight:bold;'
        f'padding:10px 16px;background:{h2_bg};'
        f'{b_side}:5px solid {a};margin:0 0 13px 0;color:{p};">'
        f'{esc(b.title)}</h2>'
    )
    bt = (b.block_type or "paragraph").strip().lower()

    if bt == "paragraph":
        return f'<div style="margin:18px 0;">{h2}{text_to_paras(b.text or "", align)}</div>'

    elif bt in ("bullets", "numbered_list"):
        items = b.items or []
        tag   = "ol" if bt == "numbered_list" else "ul"
        lis   = "".join(
            f'<li style="margin-bottom:9px;line-height:1.85;color:{txt_color};">'
            f'{render_item_with_subnote(i, txt_color, a)}</li>'
            for i in items
        )
        return f'<div style="margin:18px 0;">{h2}<{tag} style="{p_side}:22px;margin:0;">{lis}</{tag}></div>'

    elif bt == "stats":
        items = b.items or []
        rows  = ""
        for idx, item in enumerate(items):
            parts = str(item).split(":", 1)
            bg_r  = bg if idx % 2 == 0 else bg2
            if len(parts) == 2:
                rows += (
                    f'<tr>'
                    f'<td style="font-weight:bold;color:{p};padding:8px 12px;'
                    f'background:{bg};border:1px solid #ddd;width:36%;">{esc(parts[0].strip())}</td>'
                    f'<td style="padding:8px 12px;border:1px solid #ddd;background:{bg_r};'
                    f'color:{txt_color};">{esc(parts[1].strip())}</td>'
                    f'</tr>'
                )
            else:
                rows += f'<tr><td colspan="2" style="padding:8px 12px;border:1px solid #ddd;">{esc(item)}</td></tr>'
        return (
            f'<div style="margin:18px 0;">{h2}'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">{rows}</table>'
            f'</div>'
        )

    elif bt == "examples":
        items = b.items or []
        rows  = ""
        for idx, item in enumerate(items, 1):
            bg_r = bg if idx % 2 == 1 else bg2
            rows += (
                f'<tr>'
                f'<td style="width:28px;text-align:center;font-weight:bold;color:#fff;'
                f'background:{a};padding:8px;border:1px solid #ddd;">{idx}</td>'
                f'<td style="padding:8px 12px;border:1px solid #ddd;background:{bg_r};'
                f'line-height:1.85;color:{txt_color};">'
                f'{render_item_with_subnote(item, txt_color, a)}</td>'
                f'</tr>'
            )
        return (
            f'<div style="margin:18px 0;">{h2}'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">{rows}</table>'
            f'</div>'
        )

    elif bt == "pros_cons":
        pros  = b.pros or []
        cons  = b.cons or []
        p_lis = "".join(
            f'<li style="margin-bottom:7px;font-size:13px;line-height:1.8;">'
            f'{render_item_with_subnote(x, txt_color, "#276749")}</li>'
            for x in pros
        )
        c_lis = "".join(
            f'<li style="margin-bottom:7px;font-size:13px;line-height:1.8;">'
            f'{render_item_with_subnote(x, txt_color, "#9b2c2c")}</li>'
            for x in cons
        )
        return (
            f'<div style="margin:18px 0;">{h2}'
            f'<table style="width:100%;border-collapse:separate;border-spacing:6px 0;">'
            f'<tr>'
            f'<td style="vertical-align:top;padding:14px;background:#f0fff4;'
            f'border:1px solid #9ae6b4;border-radius:6px;width:50%;">'
            f'<strong style="color:#276749;display:block;margin-bottom:8px;">{lang["pros_label"]}</strong>'
            f'<ul style="{p_side}:18px;margin:0;">{p_lis}</ul></td>'
            f'<td style="vertical-align:top;padding:14px;background:#fff5f5;'
            f'border:1px solid #feb2b2;border-radius:6px;width:50%;">'
            f'<strong style="color:#9b2c2c;display:block;margin-bottom:8px;">{lang["cons_label"]}</strong>'
            f'<ul style="{p_side}:18px;margin:0;">{c_lis}</ul></td>'
            f'</tr></table></div>'
        )

    elif bt == "table":
        headers   = b.headers or []
        rows_data = b.rows or []
        ths = "".join(
            f'<th style="background:{p};color:#fff;padding:9px 12px;'
            f'text-align:{align};font-weight:bold;">{esc(h)}</th>'
            for h in headers
        )
        rows = ""
        for ridx, row in enumerate(rows_data):
            bg_r = bg if ridx % 2 == 0 else bg2
            tds  = "".join(
                f'<td style="padding:8px 12px;border:1px solid #ddd;'
                f'background:{bg_r};color:{txt_color};">{esc(c)}</td>'
                for c in row
            )
            rows += f"<tr>{tds}</tr>"
        return (
            f'<div style="margin:18px 0;page-break-inside:avoid;">{h2}'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            f'<thead><tr>{ths}</tr></thead><tbody>{rows}</tbody></table>'
            f'</div>'
        )

    elif bt == "comparison":
        sa  = esc(b.side_a or "A")
        sb  = esc(b.side_b or "B")
        cr  = b.criteria or []
        av  = b.side_a_values or []
        bv  = b.side_b_values or []
        ths = (
            f'<th style="background:{p};color:#fff;padding:9px 12px;">المعيار</th>'
            f'<th style="background:{p};color:#fff;padding:9px 12px;">{sa}</th>'
            f'<th style="background:{p};color:#fff;padding:9px 12px;">{sb}</th>'
        )
        rows = ""
        for idx, crit in enumerate(cr):
            av_val = esc(av[idx]) if idx < len(av) else "-"
            bv_val = esc(bv[idx]) if idx < len(bv) else "-"
            bg_r   = bg if idx % 2 == 0 else bg2
            rows += (
                f'<tr>'
                f'<td style="font-weight:bold;color:{p};padding:8px 12px;border:1px solid #ddd;background:{bg};">{esc(crit)}</td>'
                f'<td style="padding:8px 12px;border:1px solid #ddd;background:{bg_r};">{av_val}</td>'
                f'<td style="padding:8px 12px;border:1px solid #ddd;background:{bg_r};">{bv_val}</td>'
                f'</tr>'
            )
        return (
            f'<div style="margin:18px 0;page-break-inside:avoid;">{h2}'
            f'<table style="width:100%;border-collapse:collapse;font-size:13px;">'
            f'<thead><tr>{ths}</tr></thead><tbody>{rows}</tbody></table>'
            f'</div>'
        )

    elif bt == "quote":
        bd = "border-right" if is_rtl else "border-left"
        pd = "padding-right" if is_rtl else "padding-left"
        return (
            f'<div style="margin:18px 0;">{h2}'
            f'<blockquote style="{bd}:5px solid {a};{pd}:16px;margin:0;'
            f'color:#555;font-style:italic;line-height:1.9;">'
            f'{esc(b.text or "")}</blockquote></div>'
        )

    else:
        return f'<div style="margin:18px 0;">{h2}{text_to_paras(b.text or "", align)}</div>'


def render_html(report: DynamicReport, template_name: str, language_key: str) -> str:
    tc   = TEMPLATES[template_name]
    lang = LANGUAGES[language_key]
    p    = tc["primary"]
    a    = tc["accent"]
    bg   = tc["bg"]
    dir_ = lang["dir"]
    align= lang["align"]
    font = lang["font"]
    is_rtl  = dir_ == "rtl"
    b_side  = "border-right" if is_rtl else "border-left"
    is_dark = (template_name == "dark_elegant")
    page_bg    = "#1a202c" if is_dark else "#ffffff"
    body_color = "#e2e8f0" if is_dark else "#333333"
    box_bg     = "#2d3748" if is_dark else bg

    # ✅ القالب الداكن: هوامش صفرية على الصفحة + padding داخلي على الـ body
    page_margin  = "0"        if is_dark else "2.5cm"
    body_padding = "2.5cm"    if is_dark else "0"

    blocks_html = "\n".join(render_block(bl, tc, lang) for bl in report.blocks)

    return f"""<!DOCTYPE html>
<html lang="{lang['lang_attr']}" dir="{dir_}">
<head>
<meta charset="UTF-8">
<style>
  @page {{ size: A4; margin: {page_margin}; background: {page_bg}; }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: {font};
    direction: {dir_};
    text-align: {align};
    line-height: 1.95;
    color: {body_color};
    background: {page_bg};
    font-size: 14px;
    margin: 0; padding: {body_padding};
  }}
</style>
</head>
<body>

<h1 style="text-align:center;color:{p};font-size:24px;font-weight:bold;
           padding-bottom:14px;margin-bottom:28px;
           border-bottom:3px solid {a};">
  {esc(report.title)}
</h1>

<div style="background:{box_bg};padding:18px 22px;border-radius:8px;
            margin:0 0 20px 0;{b_side}:5px solid {a};">
  <h2 style="color:{p};font-size:15px;font-weight:bold;margin:0 0 10px 0;">
    📚 {lang['intro_label']}
  </h2>
  {text_to_paras(report.introduction, align)}
</div>

{blocks_html}

<div style="background:{box_bg};padding:18px 22px;border-radius:8px;
            margin:20px 0 0 0;{b_side}:5px solid {a};">
  <h2 style="color:{p};font-size:15px;font-weight:bold;margin:0 0 10px 0;">
    🎯 {lang['conclusion_label']}
  </h2>
  {text_to_paras(report.conclusion, align)}
</div>

</body>
</html>"""


# ═══════════════════════════════════════════════════
# KEYBOARD HELPERS
# ═══════════════════════════════════════════════════
def title_keyboard():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🤖 اتركه للذكاء الاصطناعي", callback_data="title_auto")
    ]])

def lang_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(v["name"], callback_data=f"lang_{k}")]
        for k, v in LANGUAGES.items()
    ])

def depth_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(v["name"], callback_data=f"depth_{k}")]
        for k, v in DEPTH_OPTIONS.items()
    ])

def template_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(v["name"], callback_data=f"tpl_{k}")]
        for k, v in TEMPLATES.items()
    ])


# ═══════════════════════════════════════════════════
# TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_sessions:
        user_sessions.pop(user_id, None)
        queue_positions.pop(user_id, None)
        await update.message.reply_text(
            "❌ <b>تم إلغاء الجلسة الحالية.</b>\n\n🚀 أرسل موضوعاً جديداً لبدء تقرير جديد.",
            parse_mode='HTML'
        )
    else:
        await update.message.reply_text(
            "ℹ️ لا توجد جلسة نشطة.\n\n🚀 أرسل موضوعاً لبدء تقرير جديد.",
            parse_mode='HTML'
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    # نمسح الجلسة القديمة عند /start
    user_sessions.pop(user_id, None)

    name = update.effective_user.first_name
    msg = f"""
🎓 <b>مرحباً {name}!</b>

أنا <b>بوت التقارير الجامعية الذكي</b> 🤖

✨ <b>كيف يعمل البوت؟</b>
1️⃣ أرسل موضوع تقريرك
2️⃣ اختر اللغة
3️⃣ أجب على <b>أسئلة ذكية</b> مخصصة لموضوعك
4️⃣ اختر العمق والتصميم
5️⃣ احصل على تقريرك PDF احترافي 🎉

🧠 <b>ذكاء البوت:</b>
• يولّد أسئلة مخصصة لكل موضوع
• يبني الهيكل بناءً على إجاباتك
• يختار جداول ومقارنات ونقاط تلقائياً
• موجّه خصيصاً لطلاب الجامعة

🚀 <b>أرسل موضوع تقريرك الآن!</b>
"""
    await update.message.reply_text(msg, parse_mode='HTML')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text.strip()

    # ══════════════════════════════════════════════════════════
    # القاعدة الذهبية: إذا كان المستخدم في جلسة نشطة بأي حالة،
    # لا نُنشئ جلسة جديدة أبداً — نُعالج بناءً على الحالة فقط
    # ══════════════════════════════════════════════════════════
    if user_id in user_sessions:
        session = user_sessions[user_id]
        state   = session.get("state", "")

        # حالة استقبال الإجابات
        if state == "answering":
            answers   = session.setdefault("answers", [])
            questions = session.get("dynamic_questions", [])
            answers.append(text)

            if len(answers) < len(questions):
                # عرض السؤال التالي
                next_q = questions[len(answers)]
                q_num  = len(answers) + 1
                total  = len(questions)
                await update.message.reply_text(
                    f"✅ تم تسجيل إجابتك.\n\n"
                    f"❓ <b>السؤال {q_num}/{total}:</b>\n{next_q}\n\n"
                    f"<i>اكتب إجابتك 👇</i>",
                    parse_mode='HTML'
                )
            else:
                # انتهت الأسئلة → سؤال العنوان
                session["state"] = "choosing_title"
                await update.message.reply_text(
                    "✅ <b>ممتاز! تم تسجيل جميع إجاباتك.</b>\n\n"
                    "📌 <b>هل تريد تحديد عنوان للتقرير؟</b>\n"
                    "<i>اكتب العنوان الذي تريده، أو اضغط الزر أسفله لتركه للذكاء الاصطناعي.</i>",
                    reply_markup=title_keyboard(),
                    parse_mode='HTML'
                )
            return

        # حالة كتابة عنوان مخصص
        if state == "choosing_title":
            session["custom_title"] = text
            session["state"] = "choosing_depth"
            await update.message.reply_text(
                f"✅ <b>العنوان:</b> <i>{esc(text)}</i>\n\n"
                "📏 <b>اختر عمق التقرير:</b>",
                reply_markup=depth_keyboard(),
                parse_mode='HTML'
            )
            return

        # أي حالة أخرى (يجب على المستخدم استخدام الأزرار)
        guidance = STATE_GUIDANCE.get(
            state,
            "⏳ جاري معالجة طلبك... انتظر أو أرسل /cancel للبدء من جديد."
        )
        await update.message.reply_text(guidance, parse_mode='HTML')
        return  # ← لا نكمل للأسفل أبداً طالما توجد جلسة

    # ══════════════════════════════════════════════════════════
    # لا يوجد جلسة → موضوع جديد
    # ══════════════════════════════════════════════════════════
    if len(text) < 5:
        await update.message.reply_text("❌ الموضوع قصير جداً. أرسل موضوعاً أوضح.")
        return
    if len(text) > 250:
        await update.message.reply_text("❌ الموضوع طويل جداً. اختصره لأقل من 250 حرف.")
        return

    user_sessions[user_id] = {"topic": text, "state": "choosing_lang"}
    safe = text.replace('<','&lt;').replace('>','&gt;').replace('&','&amp;')

    await update.message.reply_text(
        f"📝 <b>الموضوع:</b> <i>{safe}</i>\n\n🌐 <b>اختر لغة التقرير:</b>",
        reply_markup=lang_keyboard(),
        parse_mode='HTML'
    )


async def title_auto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User chose to let AI generate the title."""
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية. أرسل موضوعاً جديداً.")
        return

    session = user_sessions[user_id]
    if session.get("state") != "choosing_title":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return

    session.pop("custom_title", None)   # بدون عنوان مخصص = الذكاء يولّده
    session["state"] = "choosing_depth"
    await query.edit_message_text(
        "🤖 <b>سيقوم الذكاء الاصطناعي باختيار العنوان المناسب.</b>\n\n"
        "📏 <b>اختر عمق التقرير:</b>",
        reply_markup=depth_keyboard(),
        parse_mode='HTML'
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang    = query.data.replace("lang_", "")

    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية. أرسل موضوعاً جديداً.")
        return

    session             = user_sessions[user_id]
    session["language"] = lang
    session["state"]    = "generating_questions"

    await query.edit_message_text(
        f"✅ <b>اللغة:</b> {LANGUAGES[lang]['name']}\n\n"
        f"⏳ <i>جاري تحليل موضوعك وتوليد الأسئلة المناسبة...</i>",
        parse_mode='HTML'
    )

    try:
        loop      = asyncio.get_event_loop()
        topic     = session["topic"]
        questions = await loop.run_in_executor(
            None, generate_dynamic_questions, topic, lang
        )

        if not questions:
            raise ValueError("لم يتم توليد أي أسئلة")

        session["dynamic_questions"] = questions
        session["state"]             = "answering"

        first_q   = questions[0]
        total_q   = len(questions)
        q_word    = "سؤال" if total_q == 1 else "أسئلة"

        hint = (
            "\n\n💡 <i>تلميح: يمكنك طلب جداول، قوائم مزايا/عيوب، "
            "أو نقاط فرعية داخل الأقسام الكبيرة في إجاباتك.</i>"
        )

        await query.edit_message_text(
            f"🧠 <b>لدي {total_q} {q_word} قبل إنشاء تقريرك:</b>{hint}\n\n"
            f"❓ <b>السؤال 1/{total_q}:</b>\n{first_q}\n\n"
            f"<i>اكتب إجابتك 👇</i>",
            parse_mode='HTML'
        )

    except Exception as e:
        logger.error(f"Question generation failed: {e}", exc_info=True)
        # Fallback: تخطى الأسئلة والذهاب للعمق مباشرة
        session["dynamic_questions"] = []
        session["answers"]           = []
        session["state"]             = "choosing_depth"
        await query.edit_message_text(
            "⚠️ تعذّر توليد الأسئلة. سنكمل مباشرةً.\n\n📏 <b>اختر عمق التقرير:</b>",
            reply_markup=depth_keyboard(),
            parse_mode='HTML'
        )


async def depth_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    depth   = query.data.replace("depth_", "")

    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية. أرسل موضوعاً جديداً.")
        return

    # ✅ تحقق أن المستخدم في الحالة الصحيحة
    state = user_sessions[user_id].get("state")
    if state != "choosing_depth":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return

    user_sessions[user_id]["depth"] = depth
    user_sessions[user_id]["state"] = "choosing_template"

    await query.edit_message_text(
        f"✅ <b>العمق:</b> {DEPTH_OPTIONS[depth]['name']}\n\n🎨 <b>اختر تصميم التقرير:</b>",
        reply_markup=template_keyboard(),
        parse_mode='HTML'
    )


async def template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    tpl     = query.data.replace("tpl_", "")

    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية. أرسل موضوعاً جديداً.")
        return

    # ✅ تحقق أن المستخدم في الحالة الصحيحة
    state = user_sessions[user_id].get("state")
    if state != "choosing_template":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return

    session = user_sessions[user_id]
    session["template"] = tpl
    session["state"]    = "in_queue"

    topic      = session["topic"]
    lang       = session.get("language", "ar")
    depth      = session.get("depth", "medium")
    lang_name  = LANGUAGES[lang]["name"]
    depth_name = DEPTH_OPTIONS[depth]["name"]
    tpl_name   = TEMPLATES[tpl]["name"]

    # Queue position
    pos = report_queue.qsize() + 1
    queue_positions[user_id] = pos
    safe = topic.replace('<','&lt;').replace('>','&gt;').replace('&','&amp;')

    if pos == 1:
        status_msg = "🔄 <b>تقريرك قيد الإنشاء الآن...</b>"
    else:
        status_msg = f"⏳ <b>أنت في الطابور — الترتيب {pos}</b>\nسيُنشأ تقريرك قريباً..."

    # ✅ FIX 5: استخدام message_id من query.message مباشرة وهو أكثر موثوقية
    msg_id = query.message.message_id

    await query.edit_message_text(
        f"{status_msg}\n\n"
        f"📝 <b>الموضوع:</b> <i>{safe}</i>\n"
        f"🌐 {lang_name}  |  📏 {depth_name}  |  🎨 {tpl_name}",
        parse_mode='HTML'
    )

    await report_queue.put((user_id, session.copy(), msg_id))


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update error: {context.error}", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text("❌ حدث خطأ. حاول مرة أخرى.")
    except Exception:
        pass


# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
async def post_init(app):
    global report_queue
    report_queue = asyncio.Queue()
    asyncio.create_task(queue_worker(app))
    logger.info("✅ Queue worker started")


if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask started")

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("❌ TELEGRAM_TOKEN missing")
        exit(1)

    try:
        app = (
            ApplicationBuilder()
            .token(token)
            .post_init(post_init)
            .build()
        )

        app.add_handler(CommandHandler('start', start))
        app.add_handler(CommandHandler('cancel', cancel))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        app.add_handler(CallbackQueryHandler(title_auto_callback,    pattern=r'^title_auto$'))
        app.add_handler(CallbackQueryHandler(language_callback, pattern=r'^lang_'))
        app.add_handler(CallbackQueryHandler(depth_callback,    pattern=r'^depth_'))
        app.add_handler(CallbackQueryHandler(template_callback, pattern=r'^tpl_'))
        app.add_error_handler(error_handler)

        logger.info("🤖 Smart University Reports Bot v4.0 Ready!")
        print("=" * 60)
        print("✅ Smart University Reports Bot — v4.0")
        print("=" * 60)

        app.run_polling(allowed_updates=Update.ALL_TYPES)

    except Exception as e:
        logger.error(f"❌ Startup failed: {e}", exc_info=True)
        exit(1)


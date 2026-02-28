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
    style: Optional[str] = Field(default=None, description="Visual style variant — used only for pros_cons: 'A', 'B', 'C', or 'D'")
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
    introduction: str = Field(description="Introduction: 3-5 sentences. Direct and engaging.")
    blocks: List[ReportBlock] = Field(description="Content blocks")
    conclusion: str = Field(description="Conclusion: 4-6 sentences. Genuine insight — not a summary. MANDATORY.")


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
    "emerald":      {"name": "🌿 زمردي",     "primary": "#1a4731", "accent": "#52b788", "bg": "#f0faf4", "bg2": "#ffffff"},
    "modern":       {"name": "🚀 عصري",      "primary": "#5a67d8", "accent": "#667eea", "bg": "#ebf4ff", "bg2": "#ffffff"},
    "minimal":      {"name": "⚪ بسيط",      "primary": "#2d3748", "accent": "#718096", "bg": "#f7fafc", "bg2": "#ffffff"},
    "professional": {"name": "💼 احترافي",   "primary": "#1a365d", "accent": "#2b6cb0", "bg": "#bee3f8", "bg2": "#f0f4ff"},
    "dark_elegant": {"name": "🖤 أنيق داكن", "primary": "#d4af37", "accent": "#f6d860", "bg": "#2d3748", "bg2": "#4a5568"},
}

DEPTH_OPTIONS = {
    "medium":   {"name": "📄 متوسط (3-4 صفحات)", "pages": 4,  "blocks_min": 5,  "blocks_max": 7},
    "detailed": {"name": "📚 مفصل (5-6 صفحات)",  "pages": 6,  "blocks_min": 7,  "blocks_max": 10},
    "extended": {"name": "📖 موسّع (7+ صفحات)",   "pages": 8,  "blocks_min": 10, "blocks_max": 14},
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

    return f"""You are a skilled academic writer. Write a university report that feels GENUINELY HUMAN-WRITTEN — not AI.

══════════════════════════════════════
TOPIC: {topic}
LANGUAGE: {lang["instruction"]}
{title_instruction}
TARGET: {d["pages"]} A4 pages — approximately {d["pages"] * 420} words total.
SECTIONS: {d["blocks_min"]} to {d["blocks_max"]} content blocks.
══════════════════════════════════════

STUDENT'S REQUIREMENTS:
{qa_block.strip()}

══════════════════════════════════════
BLOCK TYPES AND WORD COUNTS:
- "paragraph"    → "text": 160-220 words. Natural line breaks using \\n (3-5 times). End some sentences MID-LINE deliberately.
- "bullets"      → "items": 5-7 items. 40% have sub-note with " — ", 60% are standalone. Mix lengths — never uniform.
- "numbered_list"→ "items": 5-7 steps. Same 40/60 sub-note rule.
- "table"        → "headers" + "rows" (4-6 rows). Fits one page. Max 2 per report.
- "pros_cons"    → "pros": 4-5 items, "cons": 4-5 items.
                   Set "style" field to ONE of: "A", "B", "C", "D" — pick different style each report.
                   Style A = two-column cards with colored header
                   Style B = alternating green/red rows in single table
                   Style C = stacked vertical sections (pros above, cons below)
                   Style D = emoji list (✅/❌) in single flowing column
                   PLACEMENT RULE: pros_cons must appear either:
                   (a) after a full paragraph that fills the top of the page, OR
                   (b) at the start of a page followed by a bullets/paragraph to fill remaining space.
                   NEVER place pros_cons alone on a half-empty page.
- "comparison"   → criteria: 5-6. Max 2 per report.
- "stats"        → "items": 5-6. "Label: value — context" format.
- "examples"     → "items": 5-6 with " — " detail on 60% only.
- "quote"        → "text": 2-3 sentences. Sharp and direct.

══════════════════════════════════════
PAGE FILLING STRATEGY — CRITICAL:
Total words needed: ~{d["pages"] * 420}. Font is large (15.5px) — text fills pages faster than expected.
• After ANY block that is short (bullets, quote, pros_cons), the NEXT block must be a paragraph of 160-220 words.
• Never have two consecutive short blocks — always alternate: short → long → short → long.
• If you sense a page will be short, EXPAND the preceding paragraph — do NOT add another table/list.
• pros_cons text is justified — items will naturally fill their column width.

CONTENT BALANCE:
  • 45% paragraph blocks (most important)
  • 35% bullets / numbered / pros_cons
  • 20% table/stats/comparison/examples (max 2 total)

══════════════════════════════════════
HUMAN WRITING STYLE — MANDATORY:
1. Vary sentence length aggressively: "قصيرة. ثم جملة أطول تشرح وتوسّع الفكرة بشكل كامل ومقنع."
2. End sentences in the MIDDLE of a visual line — don't pad to reach line end.
3. Start some paragraphs mid-thought or with a rhetorical question.
4. List items: deliberately unequal lengths. Some 4 words, some 18 words.
5. Conclusions: one strong unexpected angle or forward-looking statement. NOT a summary.
6. NO formulaic openers: "يتناول" / "In this report" / "هذا الموضوع مهم" are FORBIDDEN.
7. Paragraphs: start with strong claim, develop it, end with a twist or implication.
8. Use \\n inside paragraph text to create natural visual breathing — not every sentence.

ALL text in specified language. Conclusion MANDATORY.
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
            f'<div class="block-stats" style="margin:18px 0;page-break-inside:avoid;">{h2}'
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
        style = (b.style or "A").upper().strip()

        def pro_item_full(x):
            sep = " — "
            if sep in str(x):
                pts = str(x).split(sep, 1)
                return (
                    f'<li style="margin-bottom:7px;line-height:1.75;font-size:13.5px;">'
                    f'<span style="font-weight:700;color:#1a5e38;">{esc(pts[0].strip())}</span>'
                    f'<br><span style="color:#2d6a4f;font-size:12.5px;{p_side}:6px;">↳ {esc(pts[1].strip())}</span></li>'
                )
            return f'<li style="margin-bottom:7px;line-height:1.75;font-size:13.5px;font-weight:600;color:#1a5e38;">{esc(x)}</li>'

        def con_item_full(x):
            sep = " — "
            if sep in str(x):
                pts = str(x).split(sep, 1)
                return (
                    f'<li style="margin-bottom:7px;line-height:1.75;font-size:13.5px;">'
                    f'<span style="font-weight:700;color:#7b1a1a;">{esc(pts[0].strip())}</span>'
                    f'<br><span style="color:#922b21;font-size:12.5px;{p_side}:6px;">↳ {esc(pts[1].strip())}</span></li>'
                )
            return f'<li style="margin-bottom:7px;line-height:1.75;font-size:13.5px;font-weight:600;color:#7b1a1a;">{esc(x)}</li>'

        if style == "A":
            # ── Style A: بطاقتان جانبيتان مع رأس ملوّن ──
            p_lis = "".join(pro_item_full(x) for x in pros)
            c_lis = "".join(con_item_full(x) for x in cons)
            pro_hdr = (
                f'<div style="background:#1a5e38;color:#fff;font-weight:700;font-size:14px;'
                f'padding:9px 16px;border-radius:6px 6px 0 0;">{lang["pros_label"]}</div>'
            )
            con_hdr = (
                f'<div style="background:#7b1a1a;color:#fff;font-weight:700;font-size:14px;'
                f'padding:9px 16px;border-radius:6px 6px 0 0;">{lang["cons_label"]}</div>'
            )
            inner = (
                f'<table style="width:100%;border-collapse:separate;border-spacing:8px 0;">'
                f'<tr>'
                f'<td style="vertical-align:top;width:50%;padding:0;">'
                f'{pro_hdr}'
                f'<div style="background:#f0fff4;border:2px solid #1a5e38;border-top:none;'
                f'border-radius:0 0 6px 6px;padding:10px 14px;">'
                f'<ul style="{p_side}:14px;margin:0;">{p_lis}</ul></div></td>'
                f'<td style="vertical-align:top;width:50%;padding:0;">'
                f'{con_hdr}'
                f'<div style="background:#fff5f5;border:2px solid #7b1a1a;border-top:none;'
                f'border-radius:0 0 6px 6px;padding:10px 14px;">'
                f'<ul style="{p_side}:14px;margin:0;">{c_lis}</ul></div></td>'
                f'</tr></table>'
            )

        elif style == "B":
            # ── Style B: صفوف متناوبة خضراء/حمراء في جدول واحد ──
            rows_html = ""
            all_items = [("+", x) for x in pros] + [("-", x) for x in cons]
            for sign, item in all_items:
                is_pro = sign == "+"
                row_bg   = "#f0fff4" if is_pro else "#fff5f5"
                dot_bg   = "#1a5e38" if is_pro else "#7b1a1a"
                dot_char = "✓" if is_pro else "✗"
                sep = " — "
                if sep in str(item):
                    pts = str(item).split(sep, 1)
                    cell = (
                        f'<span style="font-weight:700;">{esc(pts[0].strip())}</span>'
                        f'<span style="color:#555;font-size:12.5px;"> — {esc(pts[1].strip())}</span>'
                    )
                else:
                    cell = f'<span style="font-weight:600;">{esc(item)}</span>'
                rows_html += (
                    f'<tr style="background:{row_bg};">'
                    f'<td style="width:32px;text-align:center;font-weight:800;color:{dot_bg};'
                    f'font-size:16px;padding:9px 6px;border-bottom:1px solid #e8f0e8;">{dot_char}</td>'
                    f'<td style="padding:9px 12px;border-bottom:1px solid #e8e8e8;'
                    f'font-size:13.5px;line-height:1.7;">{cell}</td>'
                    f'</tr>'
                )
            inner = (
                f'<table style="width:100%;border-collapse:collapse;'
                f'border:1px solid #d0d0d0;border-radius:6px;overflow:hidden;">'
                f'<thead><tr>'
                f'<th style="background:#2d3748;color:#fff;padding:9px 6px;width:32px;font-size:13px;">±</th>'
                f'<th style="background:#2d3748;color:#fff;padding:9px 14px;text-align:{align};font-size:13px;">التفاصيل</th>'
                f'</tr></thead><tbody>{rows_html}</tbody></table>'
            )

        elif style == "C":
            # ── Style C: قسمان مكدّسان عموديًا (pros أعلى، cons أسفل) ──
            p_lis = "".join(pro_item_full(x) for x in pros)
            c_lis = "".join(con_item_full(x) for x in cons)
            inner = (
                f'<div style="border:2px solid #1a5e38;border-radius:8px;margin-bottom:10px;">'
                f'<div style="background:#1a5e38;color:#fff;font-weight:700;font-size:14px;'
                f'padding:9px 16px;border-radius:6px 6px 0 0;">{lang["pros_label"]}</div>'
                f'<div style="background:#f0fff4;padding:10px 16px;">'
                f'<ul style="{p_side}:16px;margin:0;">{p_lis}</ul></div></div>'
                f'<div style="border:2px solid #7b1a1a;border-radius:8px;">'
                f'<div style="background:#7b1a1a;color:#fff;font-weight:700;font-size:14px;'
                f'padding:9px 16px;border-radius:6px 6px 0 0;">{lang["cons_label"]}</div>'
                f'<div style="background:#fff5f5;padding:10px 16px;">'
                f'<ul style="{p_side}:16px;margin:0;">{c_lis}</ul></div></div>'
            )

        else:  # Style D
            # ── Style D: قائمة نصية واحدة بإيموجي ✅/❌ أسلوب إنساني ──
            items_html = ""
            for x in pros:
                sep = " — "
                if sep in str(x):
                    pts = str(x).split(sep, 1)
                    text_part = f'<b>{esc(pts[0].strip())}</b> — <span style="color:#555;">{esc(pts[1].strip())}</span>'
                else:
                    text_part = f'<b>{esc(x)}</b>'
                items_html += (
                    f'<div style="display:flex;gap:10px;margin-bottom:9px;align-items:flex-start;">'
                    f'<span style="font-size:16px;flex-shrink:0;">✅</span>'
                    f'<span style="font-size:13.5px;line-height:1.75;">{text_part}</span></div>'
                )
            for x in cons:
                sep = " — "
                if sep in str(x):
                    pts = str(x).split(sep, 1)
                    text_part = f'<b>{esc(pts[0].strip())}</b> — <span style="color:#555;">{esc(pts[1].strip())}</span>'
                else:
                    text_part = f'<b>{esc(x)}</b>'
                items_html += (
                    f'<div style="display:flex;gap:10px;margin-bottom:9px;align-items:flex-start;">'
                    f'<span style="font-size:16px;flex-shrink:0;">❌</span>'
                    f'<span style="font-size:13.5px;line-height:1.75;">{text_part}</span></div>'
                )
            inner = (
                f'<div style="background:{bg};border:{b_side.split("-")[1]}:3px solid {a};'
                f'padding:14px 18px;border-radius:6px;">{items_html}</div>'
            )

        return f'<div style="margin:18px 0;">{h2}{inner}</div>'

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
            f'<div class="block-table" style="margin:18px 0;page-break-inside:avoid;">{h2}'
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
            f'<div class="block-comparison" style="margin:18px 0;page-break-inside:avoid;">{h2}'
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
    body_color = "#e2e8f0" if is_dark else "#2d3436"
    box_bg     = "#2d3748" if is_dark else bg

    # ══════════════════════════════════════════════════════
    # الإطار عبر @page — مستطيل كامل على كل صفحة تلقائياً
    # ══════════════════════════════════════════════════════
    if template_name == "emerald":
        # إطار زمردي مزدوج مع خط داخلي ذهبي رفيع
        page_border       = f"3px solid {p}"
        page_margin_outer = "0.35cm"
        page_padding      = "0.7cm"
        extra_page_css    = f"outline: 1.5px solid {a}; outline-offset: -7px;"

    elif template_name == "modern":
        page_border       = f"4px solid {a}"
        page_margin_outer = "0.35cm"
        page_padding      = "0.7cm"
        extra_page_css    = ""

    elif template_name == "minimal":
        page_border       = f"1.5px solid {p}"
        page_margin_outer = "0.4cm"
        page_padding      = "0.7cm"
        extra_page_css    = ""

    elif template_name == "professional":
        # إطار رسمي مزدوج: خط خارجي سميك + خط داخلي رفيع
        page_border       = f"2px solid {p}"
        page_margin_outer = "0.35cm"
        page_padding      = "0.65cm"
        extra_page_css    = f"outline: 4px solid {p}; outline-offset: -10px;"

    elif template_name == "dark_elegant":
        page_border       = f"2px solid {a}"
        page_margin_outer = "0.35cm"
        page_padding      = "0.7cm"
        extra_page_css    = ""

    else:
        page_border       = "none"
        page_margin_outer = "2cm"
        page_padding      = "0cm"
        extra_page_css    = ""

    # شريط احترافي رسمي: رأس يتضمن خطين وشعار
    if template_name == "professional":
        prof_top = (
            f'<div style="margin-bottom:24px;">'
            f'<div style="height:5px;background:{p};"></div>'
            f'<div style="height:2px;background:{a};margin-top:3px;"></div>'
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:8px 4px 6px 4px;">'
            f'<span style="font-size:11px;color:{a};font-weight:700;letter-spacing:2px;text-transform:uppercase;">تقرير أكاديمي رسمي</span>'
            f'<span style="font-size:10px;color:#8b9bb4;letter-spacing:1px;">OFFICIAL ACADEMIC REPORT</span>'
            f'</div>'
            f'<div style="height:1px;background:#d0dae8;"></div>'
            f'</div>'
        )
        prof_bot = (
            f'<div style="margin-top:24px;">'
            f'<div style="height:1px;background:#d0dae8;"></div>'
            f'<div style="display:flex;justify-content:space-between;align-items:center;'
            f'padding:6px 4px 6px 4px;">'
            f'<span style="font-size:10px;color:#8b9bb4;">سري — للاستخدام الأكاديمي فقط</span>'
            f'<span style="font-size:10px;color:#8b9bb4;">Confidential — Academic Use Only</span>'
            f'</div>'
            f'<div style="height:2px;background:{a};"></div>'
            f'<div style="height:5px;background:{p};margin-top:3px;"></div>'
            f'</div>'
        )
    else:
        prof_top = ""
        prof_bot = ""

    blocks_html = "\n".join(render_block(bl, tc, lang) for bl in report.blocks)

    return f"""<!DOCTYPE html>
<html lang="{lang['lang_attr']}" dir="{dir_}">
<head>
<meta charset="UTF-8">
<style>
  @page {{
    size: A4;
    margin: {page_margin_outer};
    border: {page_border};
    padding: {page_padding};
    background: {page_bg};
    {extra_page_css}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: {font};
    direction: {dir_};
    text-align: justify;
    line-height: 2.0;
    color: {body_color};
    background: {page_bg};
    font-size: 15.5px;
    margin: 0; padding: 0;
    word-spacing: 0.05em;
  }}
  p {{ text-align: justify; margin: 0 0 8px 0; }}
  h1 {{ font-size: 22px !important; text-align: center; }}
  h2 {{ font-size: 15px !important; text-align: {align}; }}
  li {{ text-align: {align}; }}
  /* orphans/widows منخفضة = كسر طبيعي = صفحات ممتلئة */
  p, li {{ orphans: 2; widows: 2; }}
  /* منع انقسام الجداول الفعلية فقط */
  .block-table  {{ page-break-inside: avoid; }}
  .block-stats  {{ page-break-inside: avoid; }}
  .block-comparison {{ page-break-inside: avoid; }}
  /* تجنب بقاء h2 وحده في نهاية صفحة */
  h2 {{ page-break-after: avoid; orphans: 3; widows: 3; }}
</style>
</head>
<body>

{prof_top}

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

{prof_bot}

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

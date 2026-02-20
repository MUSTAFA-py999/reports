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
active_jobs = {}
queue_positions = {}
MAX_CONCURRENT = 2

async def queue_worker(app):
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def process_one(user_id, session, msg_id):
        async with semaphore:
            active_jobs[user_id] = True
            for uid in list(queue_positions.keys()):
                if queue_positions[uid] > 0:
                    queue_positions[uid] -= 1

            try:
                loop = asyncio.get_event_loop()
                pdf_bytes, title = await loop.run_in_executor(
                    None, generate_report, session
                )

                lang     = session.get("language", "ar")
                lang_name= LANGUAGES[lang]["name"]
                depth    = session.get("depth", "medium")
                depth_name = DEPTH_OPTIONS[depth]["name"]
                tpl      = session.get("template", "classic")
                tpl_name = TEMPLATES[tpl]["name"]

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
                    except:
                        pass
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
        description="List of 2-5 open-ended questions to clarify the topic requirements."
    )

class ReportBlock(BaseModel):
    block_type: str = Field(description="'paragraph', 'bullets', 'numbered_list', 'table', 'pros_cons', 'comparison', 'stats', 'examples', 'quote'")
    title: str = Field(description="Section heading")
    text: Optional[str] = None
    items: Optional[List[str]] = None
    pros: Optional[List[str]] = None
    cons: Optional[List[str]] = None
    headers: Optional[List[str]] = None
    rows: Optional[List[List[str]]] = None
    side_a: Optional[str] = None
    side_b: Optional[str] = None
    criteria: Optional[List[str]] = None
    side_a_values: Optional[List[str]] = None
    side_b_values: Optional[List[str]] = None

class DynamicReport(BaseModel):
    title: str
    introduction: str
    blocks: List[ReportBlock]
    conclusion: str

# ═══════════════════════════════════════════════════
# CONFIG
# ═══════════════════════════════════════════════════
user_sessions = {}

LANGUAGES = {
    "ar": {
        "name": "🇸🇦 العربية", "dir": "rtl", "align": "right", "lang_attr": "ar",
        "font": "'Traditional Arabic', 'Arial', sans-serif",
        "intro_label": "المقدمة", "conclusion_label": "الخاتمة",
        "pros_label": "✅ المزايا", "cons_label": "❌ العيوب",
        "instruction": "Write ALL content in formal Arabic (فصحى). Every word must be Arabic.",
        "q_prompt": "أنت مساعد أكاديمي. الطالب يكتب تقريراً عن: \"{topic}\". اكتب 2-5 أسئلة مفتوحة بالعربية لتفهم ما يريده بالتحديد."
    },
    "en": {
        "name": "🇬🇧 English", "dir": "ltr", "align": "left", "lang_attr": "en",
        "font": "'Arial', 'Helvetica', sans-serif",
        "intro_label": "Introduction", "conclusion_label": "Conclusion",
        "pros_label": "✅ Pros", "cons_label": "❌ Cons",
        "instruction": "Write ALL content in English. Every word must be English.",
        "q_prompt": "You are an academic assistant. Topic: \"{topic}\". Write 2-5 open questions in English to clarify requirements."
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
    "short":    {"name": "📝 مختصر (3 أقسام)",  "blocks": 3, "words": "80-120"},
    "medium":   {"name": "📄 متوسط (4 أقسام)",  "blocks": 4, "words": "160-220"},
    "detailed": {"name": "📚 مفصل (5 أقسام)",   "blocks": 5, "words": "250-320"},
}

# ═══════════════════════════════════════════════════
# LLM HELPERS
# ═══════════════════════════════════════════════════
def get_llm():
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise Exception("GOOGLE_API_KEY not set")
    return ChatGoogleGenerativeAI(
        model="gemini-2.5-flash",
        temperature=0.5,
        google_api_key=api_key,
        max_retries=3
    )

def generate_dynamic_questions(topic: str, language_key: str) -> List[str]:
    lang = LANGUAGES[language_key]
    llm = get_llm().with_structured_output(SmartQuestions)
    prompt = lang["q_prompt"].format(topic=topic)
    result = llm.invoke([HumanMessage(content=prompt)])
    return result.questions[:3]

def build_report_prompt(session: dict) -> str:
    topic    = session["topic"]
    lang     = LANGUAGES[session.get("language", "ar")]
    depth    = DEPTH_OPTIONS[session.get("depth", "medium")]
    qa_block = "".join(f"Q{i}: {q}\nA{i}: {a}\n\n" for i, (q, a) in enumerate(zip(session.get("dynamic_questions", []), session.get("answers", [])), 1))

    return f"""You are an expert academic writer.
TOPIC: {topic}
LANGUAGE: {lang["instruction"]}
DEPTH: Exactly {depth["blocks"]} content blocks. Each block: {depth["words"]} words.
STUDENT REQUIREMENTS:
{qa_block.strip()}
RULES: Choose optimal block types. Conclusion is MANDATORY. Output in {lang["instruction"]}."""

def generate_report(session: dict):
    try:
        llm = get_llm().with_structured_output(DynamicReport)
        prompt = build_report_prompt(session)
        
        report = None
        for attempt in range(3):
            try:
                report = llm.invoke([HumanMessage(content=prompt)])
                if report: break
            except Exception as e:
                if attempt == 2: raise e
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
def esc(v): return html_lib.escape(str(v)) if v is not None else ""

def text_to_paras(text: str, align: str) -> str:
    lines = [l.strip() for l in str(text).split('\n') if l.strip()] or [str(text)]
    return "".join(f'<p style="text-align:{align};margin:0 0 10px 0;line-height:1.95;">{esc(l)}</p>' for l in lines)

def render_block(b: ReportBlock, tc: dict, lang: dict) -> str:
    p, a, bg, bg2, align = tc["primary"], tc["accent"], tc["bg"], tc["bg2"], lang["align"]
    is_rtl = lang["dir"] == "rtl"
    b_side = "border-right" if is_rtl else "border-left"
    p_side = "padding-right" if is_rtl else "padding-left"
    txt_color, h2_bg = ("#e2e8f0", "#3d4a5c") if p == "#d4af37" else ("#333333", bg)

    h2 = f'<h2 style="color:{p};font-size:15px;font-weight:bold;padding:10px 16px;background:{h2_bg};{b_side}:5px solid {a};margin:0 0 13px 0;">{esc(b.title)}</h2>'
    bt = (b.block_type or "paragraph").strip().lower()

    if bt in ("bullets", "numbered_list"):
        tag = "ol" if bt == "numbered_list" else "ul"
        lis = "".join(f'<li style="margin-bottom:7px;line-height:1.8;color:{txt_color};">{esc(i)}</li>' for i in (b.items or []))
        return f'<div style="margin:18px 0;">{h2}<{tag} style="{p_side}:22px;margin:0;">{lis}</{tag}></div>'
    elif bt == "stats":
        rows = "".join(f'<tr><td style="font-weight:bold;color:{p};padding:8px;background:{bg};border:1px solid #ddd;">{esc(str(i).split(":",1)[0])}</td><td style="padding:8px;border:1px solid #ddd;color:{txt_color};">{esc(str(i).split(":",1)[1] if ":" in str(i) else "")}</td></tr>' for i in (b.items or []))
        return f'<div style="margin:18px 0;">{h2}<table style="width:100%;border-collapse:collapse;font-size:13px;">{rows}</table></div>'
    elif bt == "examples":
        rows = "".join(f'<tr><td style="width:28px;text-align:center;font-weight:bold;color:#fff;background:{a};padding:8px;border:1px solid #ddd;">{idx}</td><td style="padding:8px;border:1px solid #ddd;color:{txt_color};">{esc(i)}</td></tr>' for idx, i in enumerate(b.items or [], 1))
        return f'<div style="margin:18px 0;">{h2}<table style="width:100%;border-collapse:collapse;font-size:13px;">{rows}</table></div>'
    elif bt == "pros_cons":
        p_lis = "".join(f'<li style="margin-bottom:6px;font-size:13px;">{esc(x)}</li>' for x in (b.pros or []))
        c_lis = "".join(f'<li style="margin-bottom:6px;font-size:13px;">{esc(x)}</li>' for x in (b.cons or []))
        return f'<div style="margin:18px 0;">{h2}<table style="width:100%;border-collapse:separate;border-spacing:6px 0;"><tr><td style="vertical-align:top;padding:14px;background:#f0fff4;border:1px solid #9ae6b4;border-radius:6px;width:50%;"><strong style="color:#276749;">{lang["pros_label"]}</strong><ul style="{p_side}:18px;">{p_lis}</ul></td><td style="vertical-align:top;padding:14px;background:#fff5f5;border:1px solid #feb2b2;border-radius:6px;width:50%;"><strong style="color:#9b2c2c;">{lang["cons_label"]}</strong><ul style="{p_side}:18px;">{c_lis}</ul></td></tr></table></div>'
    elif bt == "table":
        ths = "".join(f'<th style="background:{p};color:#fff;padding:9px;text-align:{align};">{esc(h)}</th>' for h in (b.headers or []))
        rows = "".join(f"<tr>{''.join(f'<td style=\"padding:8px;border:1px solid #ddd;color:{txt_color};\">{esc(c)}</td>' for c in r)}</tr>" for r in (b.rows or []))
        return f'<div style="margin:18px 0;">{h2}<table style="width:100%;border-collapse:collapse;font-size:13px;"><thead><tr>{ths}</tr></thead><tbody>{rows}</tbody></table></div>'
    elif bt == "comparison":
        sa, sb = esc(b.side_a or "A"), esc(b.side_b or "B")
        rows = "".join(f'<tr><td style="font-weight:bold;color:{p};padding:8px;border:1px solid #ddd;background:{bg};">{esc(c)}</td><td style="padding:8px;border:1px solid #ddd;">{esc((b.side_a_values or [])[idx] if idx < len(b.side_a_values or []) else "-")}</td><td style="padding:8px;border:1px solid #ddd;">{esc((b.side_b_values or [])[idx] if idx < len(b.side_b_values or []) else "-")}</td></tr>' for idx, c in enumerate(b.criteria or []))
        return f'<div style="margin:18px 0;">{h2}<table style="width:100%;border-collapse:collapse;font-size:13px;"><thead><tr><th style="background:{p};color:#fff;padding:9px;">المعيار</th><th style="background:{p};color:#fff;padding:9px;">{sa}</th><th style="background:{p};color:#fff;padding:9px;">{sb}</th></tr></thead><tbody>{rows}</tbody></table></div>'
    elif bt == "quote":
        return f'<div style="margin:18px 0;">{h2}<blockquote style="{b_side}:5px solid {a};{p_side}:16px;margin:0;color:#555;font-style:italic;">{esc(b.text or "")}</blockquote></div>'
    
    return f'<div style="margin:18px 0;">{h2}{text_to_paras(b.text or "", align)}</div>'

def render_html(report: DynamicReport, template_name: str, language_key: str) -> str:
    tc, lang = TEMPLATES[template_name], LANGUAGES[language_key]
    is_dark = template_name == "dark_elegant"
    page_bg, body_color, box_bg = ("#1a202c", "#e2e8f0", "#2d3748") if is_dark else ("#ffffff", "#333333", tc["bg"])
    b_side = "border-right" if lang["dir"] == "rtl" else "border-left"
    blocks_html = "\n".join(render_block(bl, tc, lang) for bl in report.blocks)

    return f"""<!DOCTYPE html>
<html lang="{lang['lang_attr']}" dir="{lang['dir']}">
<head>
<meta charset="UTF-8">
<style>
  @page {{ size: A4; margin: 2.5cm; }}
  * {{ box-sizing: border-box; }}
  body {{ font-family: {lang['font']}; direction: {lang['dir']}; text-align: {lang['align']}; line-height: 1.95; color: {body_color}; background: {page_bg}; font-size: 14px; margin: 0; padding: 0; }}
</style>
</head>
<body>
<h1 style="text-align:center;color:{tc['primary']};font-size:24px;font-weight:bold;padding-bottom:14px;margin-bottom:28px;border-bottom:3px solid {tc['accent']};">{esc(report.title)}</h1>
<div style="background:{box_bg};padding:18px 22px;border-radius:8px;margin:0 0 20px 0;{b_side}:5px solid {tc['accent']};">
  <h2 style="color:{tc['primary']};font-size:15px;font-weight:bold;margin:0 0 10px 0;">📚 {lang['intro_label']}</h2>
  {text_to_paras(report.introduction, lang['align'])}
</div>
{blocks_html}
<div style="background:{box_bg};padding:18px 22px;border-radius:8px;margin:20px 0 0 0;{b_side}:5px solid {tc['accent']};">
  <h2 style="color:{tc['primary']};font-size:15px;font-weight:bold;margin:0 0 10px 0;">🎯 {lang['conclusion_label']}</h2>
  {text_to_paras(report.conclusion, lang['align'])}
</div>
</body>
</html>"""

# ═══════════════════════════════════════════════════
# KEYBOARD HELPERS
# ═══════════════════════════════════════════════════
def lang_keyboard(): return InlineKeyboardMarkup([[InlineKeyboardButton(v["name"], callback_data=f"lang_{k}")] for k, v in LANGUAGES.items()])
def depth_keyboard(): return InlineKeyboardMarkup([[InlineKeyboardButton(v["name"], callback_data=f"depth_{k}")] for k, v in DEPTH_OPTIONS.items()])
def template_keyboard(): return InlineKeyboardMarkup([[InlineKeyboardButton(v["name"], callback_data=f"tpl_{k}")] for k, v in TEMPLATES.items()])

# ═══════════════════════════════════════════════════
# TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = f"🎓 <b>مرحباً {update.effective_user.first_name}!</b>\n\nأرسل موضوع تقريرك للبدء."
    await update.message.reply_text(msg, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text    = update.message.text.strip()

    if user_id in user_sessions and user_sessions[user_id].get("state") == "answering":
        session = user_sessions[user_id]
        answers, questions = session.setdefault("answers", []), session.get("dynamic_questions", [])
        answers.append(text)

        if len(answers) < len(questions):
            await update.message.reply_text(f"✅ تم. ❓ <b>السؤال {len(answers)+1}/{len(questions)}:</b>\n{questions[len(answers)]}", parse_mode='HTML')
        else:
            session["state"] = "choosing_depth"
            await update.message.reply_text("✅ <b>تم حفظ الإجابات.</b>\n\n📏 <b>اختر عمق التقرير:</b>", reply_markup=depth_keyboard(), parse_mode='HTML')
        return

    if len(text) < 5 or len(text) > 250:
        await update.message.reply_text("❌ طول الموضوع غير مناسب.")
        return

    user_sessions[user_id] = {"topic": text, "state": "choosing_lang"}
    await update.message.reply_text(f"📝 <b>الموضوع:</b> <i>{html_lib.escape(text)}</i>\n\n🌐 <b>اختر اللغة:</b>", reply_markup=lang_keyboard(), parse_mode='HTML')

async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id, lang = query.from_user.id, query.data.replace("lang_", "")

    if user_id not in user_sessions:
        return await query.edit_message_text("❌ الجلسة منتهية.")

    session = user_sessions[user_id]
    session["language"], session["state"] = lang, "generating_questions"
    await query.edit_message_text(f"✅ جاري التحليل...")

    try:
        questions = await asyncio.get_event_loop().run_in_executor(None, generate_dynamic_questions, session["topic"], lang)
        session["dynamic_questions"], session["state"] = questions, "answering"
        await query.edit_message_text(f"🧠 <b>لدي {len(questions)} أسئلة لتوضيح الطلب:</b>\n\n❓ <b>السؤال 1/{len(questions)}:</b>\n{questions[0]}\n\n<i>اكتب إجابتك 👇</i>", parse_mode='HTML')
    except Exception as e:
        logger.error(f"Questions err: {e}")
        session["dynamic_questions"], session["answers"], session["state"] = [], [], "choosing_depth"
        await query.edit_message_text("⚠️ تعذر التوليد. <b>اختر العمق:</b>", reply_markup=depth_keyboard(), parse_mode='HTML')

async def depth_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id not in user_sessions: return await query.edit_message_text("❌ انتهت الجلسة.")
    user_sessions[query.from_user.id].update({"depth": query.data.replace("depth_", ""), "state": "choosing_template"})
    await query.edit_message_text("🎨 <b>اختر التصميم:</b>", reply_markup=template_keyboard(), parse_mode='HTML')

async def template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in user_sessions: return await query.edit_message_text("❌ انتهت الجلسة.")

    session = user_sessions[user_id]
    session.update({"template": query.data.replace("tpl_", ""), "state": "in_queue"})
    pos = report_queue.qsize() + 1
    queue_positions[user_id] = pos

    status = "🔄 <b>تقريرك قيد الإنشاء...</b>" if pos == 1 else f"⏳ <b>الترتيب {pos}</b>"
    sent = await query.edit_message_text(status, parse_mode='HTML')
    await report_queue.put((user_id, session.copy(), sent.message_id))

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Error: {context.error}")

# ═══════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════
async def post_init(app):
    global report_queue
    report_queue = asyncio.Queue()
    asyncio.create_task(queue_worker(app))

if __name__ == '__main__':
    threading.Thread(target=run_flask, daemon=True).start()
    token = os.getenv("TELEGRAM_TOKEN")
    app = ApplicationBuilder().token(token).post_init(post_init).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(language_callback, pattern=r'^lang_'))
    app.add_handler(CallbackQueryHandler(depth_callback,    pattern=r'^depth_'))
    app.add_handler(CallbackQueryHandler(template_callback, pattern=r'^tpl_'))
    app.add_error_handler(error_handler)
    app.run_polling(allowed_updates=Update.ALL_TYPES)

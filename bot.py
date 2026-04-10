import os
import re
import asyncio
import threading
import logging
import html as html_lib
import requests
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

# ------------------- الإعدادات الأساسية -------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ------------------- تحميل الخطوط -------------------
FONTS_DIR = "/tmp/repooreto_fonts"

# الخطوط: الاسم الحقيقي → اسم Family في Google Fonts
_FONTS_TO_DOWNLOAD = {
    "Cairo":             "Cairo",
    "Tajawal":           "Tajawal",
    "Amiri":             "Amiri",
    "Noto Naskh Arabic": "Noto+Naskh+Arabic",
    "Lateef":            "Lateef",
    "Roboto":            "Roboto",
    "Merriweather":      "Merriweather",
    "Lato":              "Lato",
    "Playfair Display":  "Playfair+Display",
    "Source Sans Pro":   "Source+Sans+3",
}

def _download_fonts():
    os.makedirs(FONTS_DIR, exist_ok=True)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; Repooreto/1.0)"}
    ok = 0
    for name, query in _FONTS_TO_DOWNLOAD.items():
        path = os.path.join(FONTS_DIR, f"{name.replace(' ','_')}.ttf")
        if os.path.exists(path):
            ok += 1
            continue
        try:
            css = requests.get(
                f"https://fonts.googleapis.com/css2?family={query}&display=swap",
                headers=headers, timeout=10
            ).text
            urls = re.findall(r'url\((https://fonts\.gstatic[^)]+)\)', css)
            if urls:
                data = requests.get(urls[0], timeout=15).content
                open(path, 'wb').write(data)
                ok += 1
                logger.info(f"✅ Font: {name}")
        except Exception as e:
            logger.warning(f"⚠️ Font fail ({name}): {e}")
    logger.info(f"🔤 Fonts: {ok}/{len(_FONTS_TO_DOWNLOAD)}")

_download_fonts()

_font_face_css_cache: str = None

def _font_face_css() -> str:
    global _font_face_css_cache
    if _font_face_css_cache is not None:
        return _font_face_css_cache
    css = ""
    for name in _FONTS_TO_DOWNLOAD:
        path = os.path.join(FONTS_DIR, f"{name.replace(' ','_')}.ttf")
        if os.path.exists(path):
            css += f"@font-face{{font-family:'{name}';src:url('file://{path}');}}\n"
    _font_face_css_cache = css
    return _font_face_css_cache

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ Repooreto Bot v5.5"

@flask_app.route('/health')
def health():
    return {"status": "healthy", "version": "5.5"}, 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)


# ------------------- نظام الطابور -------------------
report_queue: asyncio.Queue = None
active_jobs = {}
queue_positions = {}
MAX_CONCURRENT = 2
main_app_ref = None  # مرجع البوت الرئيسي لإرسال الإشعارات من بوت الأدمن


async def queue_worker(app):
    semaphore = asyncio.Semaphore(MAX_CONCURRENT)

    async def process_one(user_id, session, msg_id):
        async with semaphore:
            active_jobs[user_id] = True
            for uid in list(queue_positions.keys()):
                if queue_positions[uid] > 0:
                    queue_positions[uid] -= 1

            try:
                loop = asyncio.get_running_loop()
                pdf_bytes, title = await loop.run_in_executor(None, generate_report, session)

                lang_name = LANGUAGES[session.get("language", "ar")]["name"]
                depth_name = DEPTH_OPTIONS[session.get("depth", "medium")]["name"]
                tpl_name = "🎨 مخصص" if session.get("custom_mode") else TEMPLATES.get(session.get("template", "emerald"), {}).get("name", "")

                if pdf_bytes:
                    safe_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in title[:40])
                    safe_title = title.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
                    caption = (
                        f"👻 <b>تقريرك جاهز يا طالبنا!</b>\n\n"
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
                    count_report(user_id)
                    # تذكير بالمحاولات المتبقية
                    remaining = get_remaining(user_id)
                    admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")
                    if remaining != 999 and remaining > 0:
                        await app.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"⚠️ <b>تذكير:</b> متبقٍ لك <b>{remaining}</b> تقرير مجاني.\n"
                                f"للاشتراك تواصل مع: @{admin_user}"
                            ),
                            parse_mode='HTML'
                        )
                    elif remaining != 999 and remaining == 0:
                        await app.bot.send_message(
                            chat_id=user_id,
                            text=(
                                f"🔒 <b>انتهت تجربتك المجانية!</b>\n\n"
                                f"📩 للاشتراك تواصل مع: @{admin_user}\n"
                                f"🆔 رقمك: <code>{user_id}</code>"
                            ),
                            parse_mode='HTML'
                        )
                    logger.info(f"✅ Report sent to {user_id}")
                else:
                    await app.bot.send_message(
                        chat_id=user_id,
                        text="👻 <b>الشبح مشغول قليلاً!</b>\n\nحاول مرة أخرى بعد عدة دقائق 🕐\n\n🔄 أرسل موضوعاً جديداً للمحاولة مجدداً.",
                        parse_mode='HTML'
                    )
            except Exception as e:
                logger.error(f"Queue worker error for {user_id}: {e}", exc_info=True)
                await app.bot.send_message(
                    chat_id=user_id,
                    text="👻 <b>الشبح مشغول قليلاً!</b>\n\nحاول مرة أخرى بعد عدة دقائق 🕐\n\n🔄 أرسل موضوعاً جديداً للمحاولة مجدداً.",
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


# ------------------- نماذج Pydantic -------------------
class SmartQuestions(BaseModel):
    questions: List[str] = Field(
        description="List of open-ended questions (2-5) to ask the student about their report."
    )

class ReportBlock(BaseModel):
    block_type: str = Field(
        description=(
            "Block type — ONE of: 'paragraph','bullets','numbered_list',"
            "'table','pros_cons','comparison','stats','examples','quote'"
        )
    )
    title: str = Field(description="Section heading")
    style: Optional[str] = Field(default=None, description="pros_cons style: A/B/C/D")
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
    introduction: str = Field(description="Introduction: 2-3 short sentences.")
    blocks: List[ReportBlock] = Field(description="Content blocks")
    conclusion: str = Field(description="Conclusion: 1-2 sentences. Very brief.")


# ------------------- الإعدادات والتكوين -------------------
user_sessions = {}

LANGUAGES = {
    "ar": {
        "name": "🇸🇦 العربية",
        "dir": "rtl",
        "align": "right",
        "lang_attr": "ar",
        "font": "'Cairo', 'Arial', sans-serif",
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
    },
    "en": {
        "name": "🇬🇧 English",
        "dir": "ltr",
        "align": "left",
        "lang_attr": "en",
        "font": "'Arial', 'Helvetica', sans-serif",
        "intro_label": "Introduction",
        "conclusion_label": "Conclusion",
        "pros_label": "✅ Pros",
        "cons_label": "❌ Cons",
        "instruction": "Write ALL content in English. Every word must be English.",
        "q_prompt": (
            "أنت مساعد أكاديمي لطلاب الجامعة.\n"
            "الطالب يريد تقريراً إنجليزياً عن: \"{topic}\".\n\n"
            "اكتب بالعربية 2-4 أسئلة قصيرة ومباشرة لتحديد ما يريده الطالب في تقريره.\n"
            "قواعد الأسئلة:\n"
            "- قصيرة (جملة واحدة فقط لكل سؤال)\n"
            "- مباشرة ومحددة\n"
            "- موضوعات بسيطة: 2 أسئلة — معقدة: 3-4 أسئلة\n"
        ),
    },
}

# قوالب جاهزة
TEMPLATES = {
    "emerald":      {"name": "🌿 زمردي",     "primary": "#1a4731", "accent": "#52b788", "bg": "#f0faf4", "bg2": "#ffffff"},
    "modern":       {"name": "🚀 عصري",      "primary": "#5a67d8", "accent": "#667eea", "bg": "#ebf4ff", "bg2": "#ffffff"},
    "minimal":      {"name": "⚪ بسيط",      "primary": "#2d3748", "accent": "#718096", "bg": "#f7fafc", "bg2": "#ffffff"},
    "professional": {"name": "💼 احترافي",   "primary": "#1a365d", "accent": "#2b6cb0", "bg": "#bee3f8", "bg2": "#f0f4ff"},
    "dark_elegant": {"name": "🖤 أنيق داكن", "primary": "#d4af37", "accent": "#f6d860", "bg": "#2d3748", "bg2": "#4a5568"},
    "royal":        {"name": "👑 ملكي ذهبي", "primary": "#5b0e2d", "accent": "#c9a227", "bg": "#fdf6e3", "bg2": "#fff9f0"},
}

# ألوان مخصصة
CUSTOM_COLORS = {
    "royal_blue": {"label": "🔵 أزرق ملكي",    "primary": "#1a365d", "accent": "#3182ce", "bg": "#ebf8ff", "bg2": "#ffffff"},
    "emerald_g":  {"label": "🌿 زمردي",        "primary": "#1a4731", "accent": "#38a169", "bg": "#f0fff4", "bg2": "#ffffff"},
    "purple":     {"label": "💜 بنفسجي",       "primary": "#44337a", "accent": "#805ad5", "bg": "#faf5ff", "bg2": "#ffffff"},
    "orange":     {"label": "🟠 برتقالي دافئ", "primary": "#7b341e", "accent": "#dd6b20", "bg": "#fffaf0", "bg2": "#ffffff"},
    "slate":      {"label": "⚫ رمادي راقٍ",   "primary": "#1a202c", "accent": "#718096", "bg": "#f7fafc", "bg2": "#ffffff"},
    "crimson":    {"label": "🔴 أحمر كلاسيك",  "primary": "#742a2a", "accent": "#c53030", "bg": "#fff5f5", "bg2": "#ffffff"},
    "teal":       {"label": "🩵 تيل أنيق",     "primary": "#1d4044", "accent": "#2c7a7b", "bg": "#e6fffa", "bg2": "#ffffff"},
    "gold":       {"label": "✨ ذهبي ملكي",    "primary": "#5b0e2d", "accent": "#c9a227", "bg": "#fdf6e3", "bg2": "#fff9f0"},
}

# أحجام الخطوط
CUSTOM_FONT_SIZES = {
    "xsmall": {"label": "🔹 صغير جداً (12px)", "size": "12px"},
    "small":  {"label": "🔸 صغير (14px)",      "size": "14px"},
    "medium": {"label": "🔹 متوسط (16px)",     "size": "16px"},
    "large":  {"label": "🔸 كبير (18px)",      "size": "18px"},
    "xlarge": {"label": "🔹 كبير جداً (20px)", "size": "20px"},
}

# خطوط عربية
ARABIC_FONTS = {
    "cairo":       {"label": "🏙 Cairo",              "value": "'Cairo', sans-serif"},
    "tajawal":     {"label": "✍️ Tajawal",            "value": "'Tajawal', sans-serif"},
    "amiri":       {"label": "🕌 Amiri",              "value": "'Amiri', serif"},
    "noto_naskh":  {"label": "📜 Noto Naskh",         "value": "'Noto Naskh Arabic', serif"},
    "lateef":      {"label": "🖋 Lateef",             "value": "'Lateef', serif"},
}

# خطوط إنجليزية
ENGLISH_FONTS = {
    "roboto":       {"label": "🤖 Roboto",            "value": "'Roboto', sans-serif"},
    "merriweather": {"label": "📰 Merriweather",      "value": "'Merriweather', serif"},
    "lato":         {"label": "✉️ Lato",              "value": "'Lato', sans-serif"},
    "playfair":     {"label": "👑 Playfair Display",  "value": "'Playfair Display', serif"},
    "source_sans":  {"label": "💼 Source Sans Pro",   "value": "'Source Sans Pro', sans-serif"},
}

# دمج الكل (سيتم تصفيته حسب اللغة لاحقاً)
CUSTOM_FONTS = {**ARABIC_FONTS, **ENGLISH_FONTS}

# تباعد الأسطر
LINE_HEIGHTS = {
    "compact": {"label": "📏 مضغوط (1.4)", "value": "1.4"},
    "normal":  {"label": "📐 عادي (1.6)",   "value": "1.6"},
    "relaxed": {"label": "📏 واسع (1.9)",   "value": "1.9"},
}

# هوامش الصفحة
PAGE_MARGINS = {
    "small":  {"label": "🔹 ضيقة (0.8 سم)",   "value": "0.8cm"},
    "medium": {"label": "🔸 متوسطة (2 سم)",    "value": "2cm"},
    "large":  {"label": "🔻 واسعة (2.5 سم)",   "value": "2.5cm"},
}

# أنماط العنوان الرئيسي
HEADER_STYLES = {
    "formal":    {"label": "🏛 رسمي — خط كبير + خط سفلي مزدوج",   "color": "auto", "size": "24px", "style": "formal"},
    "classic":   {"label": "📄 كلاسيكي — توسيط بسيط + خط سفلي",   "color": "auto", "size": "22px", "style": "classic"},
    "modern":    {"label": "🎯 عصري — خلفية ملونة + نص أبيض",      "color": "#ffffff", "size": "22px", "style": "modern"},
}

# إظهار الترويسة
SHOW_HEADER_FOOTER = {
    "yes": {"label": "✅ نعم، أظهرها", "show": True},
    "no":  {"label": "❌ لا، أخفها", "show": False},
}

# خيارات العمق — عدد الكلمات يُحسب ديناميكياً حسب إعدادات التنسيق
DEPTH_OPTIONS = {
    "medium":   {"name": "📄 متوسط (3-4 صفحات)", "pages": 4,  "blocks_min": 5,  "blocks_max": 7},
    "detailed": {"name": "📚 مفصل (5-6 صفحات)",  "pages": 6,  "blocks_min": 7,  "blocks_max": 10},
    "extended": {"name": "📖 موسّع (7+ صفحات)",   "pages": 8,  "blocks_min": 10, "blocks_max": 14},
}

# ------------------- قيود الخطة المجانية -------------------
FREE_DEPTHS        = {"medium"}
FREE_FONT_SIZES    = {"medium"}
FREE_FONTS_AR      = {"cairo"}
FREE_FONTS_EN      = {"roboto"}
FREE_COLORS        = {"royal_blue", "crimson"}
FREE_MARGINS       = {"medium"}
FREE_HEADER_STYLES = {"formal"}
FREE_TEMPLATES     = {"emerald", "minimal", "dark_elegant"}

LOCK = "🔒 "   # بادئة الأقفال

# ------------------- مصفوفة كلمات الصفحة الدقيقة -------------------
# الحساب: A4 (210×297mm) — كل تركيبة (حجم_خط × تباعد_أسطر × هامش)
# المعادلة: words_per_page = (content_h / (font_mm × lh)) × (content_w / (font_mm × 0.58)) / 5.5 × 0.62
# content_w: small=180mm, medium=160mm, large=140mm
# content_h: small=267mm, medium=247mm, large=227mm
WORDS_PER_PAGE_MATRIX = {
    # ─── xsmall (12px / 3.18mm) ────────────────────────────────
    ("xsmall", "compact", "small"):   620,
    ("xsmall", "compact", "medium"):  460,
    ("xsmall", "compact", "large"):   380,
    ("xsmall", "normal",  "small"):   520,
    ("xsmall", "normal",  "medium"):  385,
    ("xsmall", "normal",  "large"):   320,
    ("xsmall", "relaxed", "small"):   430,
    ("xsmall", "relaxed", "medium"):  315,
    ("xsmall", "relaxed", "large"):   260,
    # ─── small (14px / 3.70mm) ─────────────────────────────────
    ("small",  "compact", "small"):   460,
    ("small",  "compact", "medium"):  340,
    ("small",  "compact", "large"):   280,
    ("small",  "normal",  "small"):   385,
    ("small",  "normal",  "medium"):  285,
    ("small",  "normal",  "large"):   235,
    ("small",  "relaxed", "small"):   315,
    ("small",  "relaxed", "medium"):  232,
    ("small",  "relaxed", "large"):   192,
    # ─── medium (16px / 4.23mm) ────────────────────────────────
    ("medium", "compact", "small"):   355,
    ("medium", "compact", "medium"):  260,
    ("medium", "compact", "large"):   215,
    ("medium", "normal",  "small"):   295,
    ("medium", "normal",  "medium"):  218,
    ("medium", "normal",  "large"):   180,
    ("medium", "relaxed", "small"):   242,
    ("medium", "relaxed", "medium"):  178,
    ("medium", "relaxed", "large"):   147,
    # ─── large (18px / 4.76mm) ─────────────────────────────────
    ("large",  "compact", "small"):   280,
    ("large",  "compact", "medium"):  206,
    ("large",  "compact", "large"):   170,
    ("large",  "normal",  "small"):   234,
    ("large",  "normal",  "medium"):  172,
    ("large",  "normal",  "large"):   142,
    ("large",  "relaxed", "small"):   191,
    ("large",  "relaxed", "medium"):  140,
    ("large",  "relaxed", "large"):   116,
    # ─── xlarge (20px / 5.29mm) ────────────────────────────────
    ("xlarge", "compact", "small"):   228,
    ("xlarge", "compact", "medium"):  167,
    ("xlarge", "compact", "large"):   138,
    ("xlarge", "normal",  "small"):   190,
    ("xlarge", "normal",  "medium"):  139,
    ("xlarge", "normal",  "large"):   115,
    ("xlarge", "relaxed", "small"):   155,
    ("xlarge", "relaxed", "medium"):  113,
    ("xlarge", "relaxed", "large"):    94,
}

# القوالب الجاهزة: 16.5px ≈ medium، line-height 1.8 = normal، margin 2.5cm = medium
PRESET_WORDS_PER_PAGE = 236

# إرشادات الحالات
STATE_GUIDANCE = {
    "choosing_lang":        "🌐 من فضلك <b>اختر اللغة</b> من الأزرار أعلاه.",
    "generating_questions": "👻 الشبح يحلل موضوعك... انتظر لحظة.",
    "choosing_title":       "📌 من فضلك <b>اكتب عنوان التقرير</b> أو اضغط الزر لتركه للشبح.",
    "choosing_depth":       "📏 من فضلك <b>اختر عمق التقرير</b> من الأزرار أعلاه.",
    "choosing_style_mode":  "🎨 من فضلك <b>اختر طريقة التصميم</b> من الأزرار أعلاه.",
    "choosing_template":    "🎭 من فضلك <b>اختر قالباً</b> من الأزرار أعلاه.",
    "choosing_font_size":   "🔡 من فضلك <b>اختر حجم الخط</b> من الأزرار أعلاه.",
    "choosing_font":        "✍️ من فضلك <b>اختر نوع الخط</b> من الأزرار أعلاه.",
    "choosing_colors":      "🎨 من فضلك <b>اختر نظام الألوان</b> من الأزرار أعلاه.",
    "choosing_line_height": "📏 من فضلك <b>اختر تباعد الأسطر</b> من الأزرار أعلاه.",
    "choosing_page_margin": "📐 من فضلك <b>اختر هوامش الصفحة</b> من الأزرار أعلاه.",
    "choosing_pros_cons":   "✅ من فضلك <b>اختر تضمين المزايا/العيوب</b> من الأزرار أعلاه.",
    "choosing_tables":      "📊 من فضلك <b>اختر تضمين الجداول</b> من الأزرار أعلاه.",
    "choosing_header_style":"🎯 من فضلك <b>اختر شكل العنوان</b> من الأزرار أعلاه.",
    "choosing_show_header": "📰 من فضلك <b>اختر إظهار الترويسة والتذييل</b> من الأزرار أعلاه.",
    "asking_comparison":    "📊 من فضلك <b>اختر</b> من الأزرار أعلاه.",
    "entering_comparison":  "✏️ اكتب الشيئين اللذين تريد مقارنتهما.\nمثال: <code>Python مقابل Java</code>",
    "in_queue":             "👻 تقريرك في الطابور... أرسل /cancel لإلغاء.",
}


# ------------------- دوال مساعدة -------------------
def hex_to_rgb(hex_color):
    """تحويل HEX إلى tuple RGB (للاستخدام المستقبلي)"""
    hex_color = hex_color.lstrip('#')
    return tuple(int(hex_color[i:i+2], 16) for i in (0, 2, 4))


def get_fonts_by_language(lang_key):
    """إرجاع قائمة الخطوط المناسبة للغة المحددة"""
    if lang_key == "ar":
        return ARABIC_FONTS
    else:
        return ENGLISH_FONTS


def get_words_per_page(session: dict) -> int:
    if session.get("custom_mode"):
        font_key   = session.get("custom_font_size_key", "medium")
        lh_key     = session.get("custom_line_height",   "normal")
        margin_key = session.get("custom_page_margin",   "medium")
        base = WORDS_PER_PAGE_MATRIX.get((font_key, lh_key, margin_key), 218)
    else:
        base = PRESET_WORDS_PER_PAGE

    # تعديل بناءً على الكتل البصرية — الجداول والمزايا/العيوب تستهلك مساحة أكبر من كلماتها
    include_tables    = session.get("include_tables", True)
    include_pros_cons = session.get("include_pros_cons", True)
    if include_tables and include_pros_cons:
        base = int(base * 0.82)   # خصم 18% — كتل بصرية ثقيلة
    elif include_tables:
        base = int(base * 0.88)   # خصم 12%
    elif include_pros_cons:
        base = int(base * 0.91)   # خصم 9%
    return base


# ------------------- دوال LLM -------------------
_api_key_cycle = None

def get_llm():
    global _api_key_cycle
    import itertools
    keys = [
        os.getenv("GOOGLE_API_KEY"),
        os.getenv("GOOGLE_API_KEY2"),
        os.getenv("GOOGLE_API_KEY3"),
    ]
    keys = [k for k in keys if k]
    if not keys:
        raise Exception("No GOOGLE_API_KEY set")
    if _api_key_cycle is None:
        _api_key_cycle = itertools.cycle(keys)
    api_key = next(_api_key_cycle)
    logger.info(f"🔑 Using API key ending: ...{api_key[-6:]}")
    return ChatGoogleGenerativeAI(
        model="gemini-1.5-flash",
        temperature=0.5,
        google_api_key=api_key,
        max_retries=2
    )


def generate_dynamic_questions(topic: str, language_key: str) -> List[str]:
    lang = LANGUAGES[language_key]
    llm = get_llm()
    parser = PydanticOutputParser(pydantic_object=SmartQuestions)
    prompt = lang["q_prompt"].format(topic=topic) + "\n\n" + parser.get_format_instructions()
    result = llm.invoke([HumanMessage(content=prompt)])
    return parser.parse(result.content).questions[:5]


def build_report_prompt(session: dict, format_instructions: str) -> str:
    topic = session["topic"]
    lang_key = session.get("language", "ar")
    depth_key = session.get("depth", "medium")
    lang = LANGUAGES[lang_key]
    depth = DEPTH_OPTIONS[depth_key]
    questions = session.get("dynamic_questions", [])
    answers = session.get("answers", [])
    custom_title = session.get("custom_title")

    # ── حساب عدد الكلمات الدقيق بناءً على إعدادات التنسيق الفعلية ──
    words_per_page = get_words_per_page(session)
    target_pages   = depth["pages"]
    target_words   = target_pages * words_per_page
    min_words      = int(target_words * 0.88)   # نطاق 12% أقل
    max_words      = int(target_words * 1.12)   # نطاق 12% أكثر

    title_instruction = (
        f'TITLE: Use EXACTLY this title: "{custom_title}" — do not change it.'
        if custom_title else "TITLE: Generate a concise academic title."
    )

    qa_block = ""
    for i, (q, a) in enumerate(zip(questions, answers), 1):
        qa_block += f"Q{i}: {q}\nA{i}: {a}\n\n"

    comparison_injection = ""
    if session.get("comparison_query"):
        cq = session["comparison_query"]
        comparison_injection = (
            f"\n\n══════════════════════════════════════\n"
            f"MANDATORY COMPARISON BLOCK — DO NOT SKIP:\n"
            f"You MUST include a 'comparison' block that compares: {cq}\n"
            f"- Set side_a and side_b to the two items\n"
            f"- Include 4-6 meaningful criteria (max 6)\n"
            f"- Place this block in the middle of the report\n"
            f"══════════════════════════════════════"
        )

    # حساب كلمات الفقرة المناسب حسب العمق
    paragraph_words = max(80, min(200, target_words // max(depth["blocks_min"], 1) - 20))
    para_min = max(60, paragraph_words - 30)
    para_max = paragraph_words + 30

    # تعليمات صارمة لضبط عدد الصفحات وتقصير المقدمة/الخاتمة
    length_instruction = (
        f"\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"LENGTH — ABSOLUTE RULES (VIOLATIONS NOT ACCEPTED):\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• TOTAL words: {target_words} words. Hard range: {min_words}–{max_words}.\n"
        f"• Each page holds ~{words_per_page} words (based on font/spacing/margin).\n"
        f"• Target: {target_pages} A4 pages.\n"
        f"\n"
        f"• INTRODUCTION: 1 sentence ONLY. One. Single. Sentence.\n"
        f"  Example: 'This report examines X through the lens of Y and Z.'\n"
        f"• CONCLUSION: 1 sentence ONLY. Period.\n"
        f"  Example: 'X remains the dominant approach due to Y.'\n"
        f"\n"
        f"• Each paragraph block: {para_min}–{para_max} words.\n"
        f"• DO NOT write long introductions or conclusions. They waste page space.\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    )

    human_style_instruction = (
        "\nSTYLE: Natural academic writing. Vary sentence lengths. No repetitive structures.\n"
    )

    table_instruction = (
        "\nTABLES: Max 6 rows per table. Must fit on one page. No cross-page tables.\n"
    )

    # قيود الكتل بناءً على اختيار المستخدم
    include_tables    = session.get("include_tables", True)
    include_pros_cons = session.get("include_pros_cons", True)
    block_restrictions = ""
    if not include_tables:
        block_restrictions += "• DO NOT use 'table' or 'stats' blocks — user disabled tables.\n"
    if not include_pros_cons:
        block_restrictions += "• DO NOT use 'pros_cons' blocks — user disabled pros/cons.\n"
    if block_restrictions:
        block_restrictions = f"\nBLOCK RESTRICTIONS (MANDATORY):\n{block_restrictions}"

    return f"""Academic report writer. Output valid JSON only.

TOPIC: {topic}
LANG: {lang["instruction"]}
{title_instruction}
BLOCKS: {depth["blocks_min"]}-{depth["blocks_max"]}
LENGTH: {min_words}-{max_words} words total ({target_pages} A4 pages, ~{words_per_page}/page)
INTRO: 1 sentence. CONCLUSION: 1 sentence.
PARAGRAPH: {para_min}-{para_max} words each.

STUDENT:
{qa_block.strip()}
{comparison_injection}
{block_restrictions}
TYPES: paragraph(text)|bullets(items 4-6)|numbered_list(items 4-6)|table(headers+rows≤5,max1)|pros_cons(pros3-4,cons3-4,max1)|comparison(side_a,side_b,criteria3-5,max1)|stats(items4-5)|examples(items4-5)|quote(text1-2sent)
MIX: 55% paragraph, 30% list, 15% table. Max 1 pros_cons block total. Max 1 table block total. No 2 short blocks consecutive. ALL blocks must fit within their page — never split a block across pages.
STYLE: Natural academic. Vary sentence length. No "In this report" opener. Direct start.

{format_instructions}"""


def count_words(text: str) -> int:
    """تقدير عدد الكلمات في النص"""
    return len(text.split())


def truncate_to_sentences(text: str, max_sentences: int) -> str:
    """يقتطع النص إلى عدد محدد من الجمل كحد أقصى."""
    sentences = [s.strip() for s in text.replace('؟', '.').replace('!', '.').split('.') if s.strip()]
    if len(sentences) <= max_sentences:
        return text
    return '. '.join(sentences[:max_sentences]) + '.'


def generate_report(session: dict):
    """توليد تقرير PDF مع التحكم الدقيق في عدد الصفحات بناءً على إعدادات التنسيق الفعلية"""
    try:
        llm = get_llm()
        parser = PydanticOutputParser(pydantic_object=DynamicReport)
        prompt = build_report_prompt(session, parser.get_format_instructions())

        best_report = None
        best_diff = float('inf')

        depth_key = session.get("depth", "medium")
        target_pages   = DEPTH_OPTIONS[depth_key]["pages"]
        words_per_page = get_words_per_page(session)
        expected_words = target_pages * words_per_page
        min_words      = int(expected_words * 0.88)
        max_words      = int(expected_words * 1.12)
        logger.info(
            f"📐 Word target: {expected_words} "
            f"({words_per_page}/page × {target_pages} pages) "
            f"range [{min_words}-{max_words}]"
        )

        last_report = None
        for attempt in range(2):  # محاولتان كافيتان — الأولى غالباً تنجح
            try:
                result = llm.invoke([HumanMessage(content=prompt)])
                report = parser.parse(result.content)
                last_report = report

                # ── إجبار المقدمة على جملة واحدة والخاتمة على جملة واحدة ──
                report.introduction = truncate_to_sentences(report.introduction, 1)
                report.conclusion   = truncate_to_sentences(report.conclusion, 1)

                total_words = (
                    count_words(report.title) +
                    count_words(report.introduction) +
                    sum(count_words(block.text or "") for block in report.blocks if block.block_type == "paragraph") +
                    sum(len(block.items or []) * 8 for block in report.blocks if block.block_type in ("bullets", "numbered_list", "stats", "examples")) +
                    sum((len(block.pros or []) + len(block.cons or [])) * 8 for block in report.blocks if block.block_type == "pros_cons") +
                    sum(len(block.rows or []) * len(block.headers or []) * 5 for block in report.blocks if block.block_type == "table") +
                    sum(len(block.criteria or []) * 8 for block in report.blocks if block.block_type == "comparison") +
                    count_words(report.conclusion)
                )

                diff = abs(total_words - expected_words)
                logger.info(f"  attempt {attempt+1}: {total_words} words (target {expected_words}, diff {diff})")

                if min_words <= total_words <= max_words:
                    best_report = report
                    break

                if diff < best_diff:
                    best_diff = diff
                    best_report = report

            except Exception as e:
                logger.warning(f"Parse attempt {attempt+1} failed: {e}")
                if attempt == 1 and last_report is None:
                    raise e

        # إذا لم نجد ضمن النطاق نستخدم الأقرب
        if best_report is None:
            if last_report:
                best_report = last_report
                best_report.introduction = truncate_to_sentences(best_report.introduction, 1)
                best_report.conclusion   = truncate_to_sentences(best_report.conclusion, 1)
                logger.warning("⚠️ Using closest report outside target range")
            else:
                raise Exception("Failed to generate valid report after 2 attempts")

        html_str = render_html(best_report, session)
        pdf_bytes = WeasyHTML(string=html_str).write_pdf()
        return pdf_bytes, best_report.title

    except Exception as e:
        logger.error(f"❌ generate_report: {e}", exc_info=True)
        return None, str(e)


# ------------------- Render HTML -------------------
def esc(v):
    return html_lib.escape(str(v)) if v is not None else ""

def render_item_with_subnote(item: str, txt_color: str, accent: str) -> str:
    sep = " — "
    if sep in str(item):
        parts = str(item).split(sep, 1)
        return (
            f'{esc(parts[0].strip())}'
            f'<span style="color:{accent};font-size:0.88em;font-weight:normal;"> — {esc(parts[1].strip())}</span>'
        )
    return esc(item)

def text_to_paras(text: str, align: str) -> str:
    lines = [l.strip() for l in str(text).split('\n') if l.strip()]
    if not lines:
        lines = [str(text)]
    return "".join(
        f'<p style="text-align:{align};margin:0 0 10px 0;line-height:2.05;">{esc(l)}</p>'
        for l in lines
    )

def render_block(b: ReportBlock, tc: dict, lang: dict) -> str:
    p = tc["primary"]
    a = tc["accent"]
    bg = tc["bg"]
    bg2 = tc["bg2"]
    align = lang["align"]
    is_rtl = lang["dir"] == "rtl"
    b_side = "border-right" if is_rtl else "border-left"
    p_side = "padding-right" if is_rtl else "padding-left"
    is_dark = (p == "#d4af37")
    txt_color = "#e2e8f0" if is_dark else "#2d3436"
    h2_bg = "#3d4a5c" if is_dark else bg
    shadow = "box-shadow:0 1px 4px rgba(0,0,0,0.07);"

    h2 = (
        f'<h2 style="color:{p};font-size:1.05em;font-weight:700;'
        f'padding:9px 16px;background:{h2_bg};'
        f'{b_side}:4px solid {a};margin:0 0 0 0;'
        f'border-radius:4px 4px 0 0;letter-spacing:0.01em;">'
        f'{esc(b.title)}</h2>'
    )
    bt = (b.block_type or "paragraph").strip().lower()

    wrap_open  = f'<div style="margin:20px 0;border-radius:6px;overflow:hidden;{shadow}">'
    wrap_close = '</div>'

    if bt == "paragraph":
        return (
            f'{wrap_open}{h2}'
            f'<div style="padding:14px 16px;background:{bg2 if not is_dark else "#2d3748"};">'
            f'{text_to_paras(b.text or "", align)}</div>{wrap_close}'
        )

    elif bt in ("bullets", "numbered_list"):
        tag = "ol" if bt == "numbered_list" else "ul"
        lis = "".join(
            f'<li style="margin-bottom:9px;line-height:1.9;color:{txt_color};">'
            f'{render_item_with_subnote(i, txt_color, a)}</li>'
            for i in (b.items or [])
        )
        return (
            f'{wrap_open}{h2}'
            f'<div style="padding:14px 16px;background:{bg2 if not is_dark else "#2d3748"};">'
            f'<{tag} style="{p_side}:20px;margin:0;">{lis}</{tag}></div>{wrap_close}'
        )

    elif bt == "stats":
        rows = ""
        for idx, item in enumerate(b.items or []):
            parts = str(item).split(":", 1)
            bg_r = bg if idx % 2 == 0 else bg2
            if len(parts) == 2:
                rows += (
                    f'<tr><td style="font-weight:700;color:{p};padding:9px 14px;background:{bg};'
                    f'border:1px solid rgba(0,0,0,0.08);width:36%;">{esc(parts[0].strip())}</td>'
                    f'<td style="padding:9px 14px;border:1px solid rgba(0,0,0,0.08);background:{bg_r};'
                    f'color:{txt_color};">{esc(parts[1].strip())}</td></tr>'
                )
            else:
                rows += f'<tr><td colspan="2" style="padding:9px 14px;border:1px solid rgba(0,0,0,0.08);">{esc(item)}</td></tr>'
        return (
            f'<div class="block-stats" style="margin:20px 0;page-break-inside:avoid;border-radius:6px;overflow:hidden;{shadow}">{h2}'
            f'<table style="width:100%;border-collapse:collapse;">{rows}</table></div>'
        )

    elif bt == "examples":
        rows = ""
        for idx, item in enumerate(b.items or [], 1):
            bg_r = bg if idx % 2 == 1 else bg2
            rows += (
                f'<tr><td style="width:30px;text-align:center;font-weight:700;color:#fff;'
                f'background:{a};padding:9px 6px;border:1px solid rgba(0,0,0,0.08);">{idx}</td>'
                f'<td style="padding:9px 14px;border:1px solid rgba(0,0,0,0.08);background:{bg_r};'
                f'line-height:1.9;color:{txt_color};">{render_item_with_subnote(item, txt_color, a)}</td></tr>'
            )
        return (
            f'<div style="margin:20px 0;border-radius:6px;overflow:hidden;{shadow}">{h2}'
            f'<table style="width:100%;border-collapse:collapse;">{rows}</table></div>'
        )

    elif bt == "pros_cons":
        pros = b.pros or []
        cons = b.cons or []
        style = (b.style or "A").upper().strip()

        def pro_li(x):
            sep = " — "
            if sep in str(x):
                pts = str(x).split(sep, 1)
                return (
                    f'<li style="margin-bottom:8px;line-height:1.85;">'
                    f'<span style="font-weight:700;color:#1a5e38;">{esc(pts[0].strip())}</span>'
                    f'<br><span style="color:#2d6a4f;font-size:0.88em;{p_side}:6px;">↳ {esc(pts[1].strip())}</span></li>'
                )
            return f'<li style="margin-bottom:8px;line-height:1.85;font-weight:600;color:#1a5e38;">{esc(x)}</li>'

        def con_li(x):
            sep = " — "
            if sep in str(x):
                pts = str(x).split(sep, 1)
                return (
                    f'<li style="margin-bottom:8px;line-height:1.85;">'
                    f'<span style="font-weight:700;color:#7b1a1a;">{esc(pts[0].strip())}</span>'
                    f'<br><span style="color:#922b21;font-size:0.88em;{p_side}:6px;">↳ {esc(pts[1].strip())}</span></li>'
                )
            return f'<li style="margin-bottom:8px;line-height:1.85;font-weight:600;color:#7b1a1a;">{esc(x)}</li>'

        if style == "A":
            p_lis = "".join(pro_li(x) for x in pros)
            c_lis = "".join(con_li(x) for x in cons)
            inner = (
                f'<table style="width:100%;border-collapse:separate;border-spacing:6px 0;padding:10px 10px 12px;background:{bg2 if not is_dark else "#2d3748"};"><tr>'
                f'<td style="vertical-align:top;width:50%;padding:0;">'
                f'<div style="background:#1a5e38;color:#fff;font-weight:700;padding:8px 14px;border-radius:5px 5px 0 0;">{lang["pros_label"]}</div>'
                f'<div style="background:#f0fff4;border:1.5px solid #1a5e38;border-top:none;border-radius:0 0 5px 5px;padding:10px 14px;">'
                f'<ul style="{p_side}:14px;margin:0;">{p_lis}</ul></div></td>'
                f'<td style="vertical-align:top;width:50%;padding:0;">'
                f'<div style="background:#7b1a1a;color:#fff;font-weight:700;padding:8px 14px;border-radius:5px 5px 0 0;">{lang["cons_label"]}</div>'
                f'<div style="background:#fff5f5;border:1.5px solid #7b1a1a;border-top:none;border-radius:0 0 5px 5px;padding:10px 14px;">'
                f'<ul style="{p_side}:14px;margin:0;">{c_lis}</ul></div></td>'
                f'</tr></table>'
            )
        elif style == "B":
            rows_html = ""
            for sign, item in [("+", x) for x in pros] + [("-", x) for x in cons]:
                is_pro = sign == "+"
                row_bg = "#f0fff4" if is_pro else "#fff5f5"
                dot_bg = "#1a5e38" if is_pro else "#7b1a1a"
                dot_char = "✓" if is_pro else "✗"
                sep = " — "
                if sep in str(item):
                    pts = str(item).split(sep, 1)
                    cell = f'<span style="font-weight:700;">{esc(pts[0].strip())}</span><span style="color:#666;font-size:0.88em;"> — {esc(pts[1].strip())}</span>'
                else:
                    cell = f'<span style="font-weight:600;">{esc(item)}</span>'
                rows_html += (
                    f'<tr style="background:{row_bg};">'
                    f'<td style="width:32px;text-align:center;font-weight:800;color:{dot_bg};font-size:1.1em;padding:10px 6px;border-bottom:1px solid rgba(0,0,0,0.06);">{dot_char}</td>'
                    f'<td style="padding:10px 14px;border-bottom:1px solid rgba(0,0,0,0.06);line-height:1.8;">{cell}</td></tr>'
                )
            inner = (
                f'<table style="width:100%;border-collapse:collapse;">'
                f'<thead><tr>'
                f'<th style="background:#2d3748;color:#fff;padding:9px 6px;width:32px;">±</th>'
                f'<th style="background:#2d3748;color:#fff;padding:9px 14px;text-align:{align};">التفاصيل</th>'
                f'</tr></thead><tbody>{rows_html}</tbody></table>'
            )
        elif style == "C":
            p_lis = "".join(pro_li(x) for x in pros)
            c_lis = "".join(con_li(x) for x in cons)
            inner = (
                f'<div style="padding:10px 12px;background:{bg2 if not is_dark else "#2d3748"};">'
                f'<div style="border:1.5px solid #1a5e38;border-radius:6px;margin-bottom:10px;">'
                f'<div style="background:#1a5e38;color:#fff;font-weight:700;padding:8px 14px;border-radius:5px 5px 0 0;">{lang["pros_label"]}</div>'
                f'<div style="background:#f0fff4;padding:10px 16px;"><ul style="{p_side}:16px;margin:0;">{p_lis}</ul></div></div>'
                f'<div style="border:1.5px solid #7b1a1a;border-radius:6px;">'
                f'<div style="background:#7b1a1a;color:#fff;font-weight:700;padding:8px 14px;border-radius:5px 5px 0 0;">{lang["cons_label"]}</div>'
                f'<div style="background:#fff5f5;padding:10px 16px;"><ul style="{p_side}:16px;margin:0;">{c_lis}</ul></div></div></div>'
            )
        else:  # D
            items_html = ""
            for emoji, lst in [("✅", pros), ("❌", cons)]:
                for x in lst:
                    sep = " — "
                    if sep in str(x):
                        pts = str(x).split(sep, 1)
                        t = f'<b>{esc(pts[0].strip())}</b> — <span style="color:#666;">{esc(pts[1].strip())}</span>'
                    else:
                        t = f'<b>{esc(x)}</b>'
                    items_html += (
                        f'<div style="display:flex;gap:10px;margin-bottom:9px;align-items:flex-start;">'
                        f'<span style="font-size:1.1em;flex-shrink:0;">{emoji}</span>'
                        f'<span style="line-height:1.85;">{t}</span></div>'
                    )
            inner = f'<div style="background:{bg2 if not is_dark else "#2d3748"};padding:14px 18px;">{items_html}</div>'

        return f'<div class="block-pros-cons" style="margin:20px 0;border-radius:6px;overflow:hidden;{shadow};page-break-inside:avoid;">{h2}{inner}</div>'

    elif bt == "table":
        ths = "".join(
            f'<th style="background:{p};color:#fff;padding:10px 14px;text-align:{align};font-weight:700;">{esc(h)}</th>'
            for h in (b.headers or [])
        )
        rows = ""
        for ridx, row in enumerate(b.rows or []):
            bg_r = bg if ridx % 2 == 0 else bg2
            tds = "".join(
                f'<td style="padding:9px 14px;border:1px solid rgba(0,0,0,0.08);background:{bg_r};color:{txt_color};">{esc(c)}</td>'
                for c in row
            )
            rows += f"<tr>{tds}</tr>"
        return (
            f'<div class="block-table" style="margin:20px 0;page-break-inside:avoid;border-radius:6px;overflow:hidden;{shadow}">{h2}'
            f'<table style="width:100%;border-collapse:collapse;">'
            f'<thead><tr>{ths}</tr></thead><tbody>{rows}</tbody></table></div>'
        )

    elif bt == "comparison":
        sa = esc(b.side_a or "A")
        sb = esc(b.side_b or "B")
        cr = b.criteria or []
        av = b.side_a_values or []
        bv = b.side_b_values or []
        ths = (
            f'<th style="background:{p};color:#fff;padding:10px 14px;text-align:{align};">المعيار</th>'
            f'<th style="background:{a};color:#fff;padding:10px 14px;text-align:center;">{sa}</th>'
            f'<th style="background:{p};color:#fff;padding:10px 14px;text-align:center;opacity:0.85;">{sb}</th>'
        )
        rows = ""
        for idx, crit in enumerate(cr):
            bg_r = bg if idx % 2 == 0 else bg2
            rows += (
                f'<tr><td style="font-weight:700;color:{p};padding:9px 14px;border:1px solid rgba(0,0,0,0.08);background:{bg};">{esc(crit)}</td>'
                f'<td style="padding:9px 14px;border:1px solid rgba(0,0,0,0.08);background:{bg_r};text-align:center;">{esc(av[idx]) if idx < len(av) else "—"}</td>'
                f'<td style="padding:9px 14px;border:1px solid rgba(0,0,0,0.08);background:{bg_r};text-align:center;">{esc(bv[idx]) if idx < len(bv) else "—"}</td></tr>'
            )
        return (
            f'<div class="block-comparison" style="margin:20px 0;page-break-inside:avoid;border-radius:6px;overflow:hidden;{shadow}">{h2}'
            f'<table style="width:100%;border-collapse:collapse;">'
            f'<thead><tr>{ths}</tr></thead><tbody>{rows}</tbody></table></div>'
        )

    elif bt == "quote":
        bd = "border-right" if is_rtl else "border-left"
        pd = "padding-right" if is_rtl else "padding-left"
        return (
            f'<div style="margin:20px 0;border-radius:6px;overflow:hidden;{shadow}">{h2}'
            f'<div style="background:{bg2 if not is_dark else "#2d3748"};padding:14px 20px;">'
            f'<blockquote style="{bd}:4px solid {a};{pd}:16px;margin:0;'
            f'color:#555;font-style:italic;line-height:2.0;">{esc(b.text or "")}</blockquote>'
            f'</div></div>'
        )

    else:
        return (
            f'{wrap_open}{h2}'
            f'<div style="padding:14px 16px;background:{bg2 if not is_dark else "#2d3748"};">'
            f'{text_to_paras(b.text or "", align)}</div>{wrap_close}'
        )


def render_html(report: DynamicReport, session: dict) -> str:
    language_key = session.get("language", "ar")
    lang = LANGUAGES[language_key]
    is_custom = session.get("custom_mode", False)
    template_name = "_custom" if is_custom else session.get("template", "emerald")

    if is_custom:
        colors = CUSTOM_COLORS[session.get("custom_color_key", "royal_blue")]
        p, a, bg, bg2 = colors["primary"], colors["accent"], colors["bg"], colors["bg2"]
        font_size = CUSTOM_FONT_SIZES[session.get("custom_font_size_key", "medium")]["size"]
        font_key = session.get("custom_font_key", "cairo")
        if language_key == "ar":
            font = ARABIC_FONTS.get(font_key, ARABIC_FONTS["cairo"])["value"]
        else:
            font = ENGLISH_FONTS.get(font_key, ENGLISH_FONTS["roboto"])["value"]
        line_height = LINE_HEIGHTS[session.get("custom_line_height", "normal")]["value"]
        page_margin = PAGE_MARGINS[session.get("custom_page_margin", "medium")]["value"]
        title_style_key = session.get("custom_header_style", "formal")
        show_hf = False
    else:
        tc = TEMPLATES[template_name]
        p, a, bg, bg2 = tc["primary"], tc["accent"], tc["bg"], tc["bg2"]
        font_size = "17px"
        font = lang["font"]
        line_height = "1.6"
        page_margin = "1.2cm"
        title_style_key = "classic"
        show_hf = False

    # شكل العنوان الرئيسي
    hs = HEADER_STYLES[title_style_key]
    title_color = a if hs["color"] == "auto" else hs["color"]
    title_size  = hs["size"]
    title_style = hs["style"]

    tc_dict = {"primary": p, "accent": a, "bg": bg, "bg2": bg2}
    dir_ = lang["dir"]
    align = lang["align"]
    is_rtl = dir_ == "rtl"
    b_side = "border-right" if is_rtl else "border-left"

    # ألوان الصفحة حسب القالب
    if template_name == "dark_elegant":
        page_bg, body_color, box_bg = "#1a202c", "#e2e8f0", "#2d3748"
    elif template_name == "royal":
        page_bg, body_color, box_bg = "#fffdf7", "#2c1810", "#fdf6e3"
    else:
        page_bg, body_color, box_bg = "#ffffff", "#2d3436", bg

    # إطارات الصفحة
    borders = {
        "emerald":      (f"3px solid {p}", "0.35cm", "0.7cm", f"outline:1.5px solid {a};outline-offset:-7px;"),
        "modern":       (f"4px solid {a}", "0.35cm", "0.7cm", ""),
        "minimal":      (f"1.5px solid {p}", "0.4cm",  "0.7cm", ""),
        "professional": (f"2px solid {p}", "0.35cm", "0.65cm", f"outline:4px solid {p};outline-offset:-10px;"),
        "dark_elegant": (f"2px solid {a}", "0.35cm", "0.7cm", ""),
        "royal":        (f"3px solid {p}", "0.35cm", "0.7cm", f"outline:2px solid {a};outline-offset:-8px;"),
        "_custom":      (f"3px solid {p}", "0.35cm", "0.7cm", f"outline:1.5px solid {a};outline-offset:-8px;"),
    }
    page_border, page_margin_extra, page_padding, extra_css = borders.get(
        template_name, ("none", "2cm", "0cm", "")
    )
    final_margin = page_margin if is_custom else page_margin_extra

    # ترويسة وتذييل
    gdir = "left" if is_rtl else "right"
    if template_name == "professional":
        prof_top = (
            f'<div style="margin-bottom:24px;">'
            f'<div style="height:5px;background:{p};"></div>'
            f'<div style="height:2px;background:{a};margin-top:3px;"></div>'
            f'<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 4px 6px 4px;">'
            f'<span style="font-size:11px;color:{a};font-weight:700;letter-spacing:2px;">تقرير أكاديمي رسمي</span>'
            f'<span style="font-size:10px;color:#8b9bb4;letter-spacing:1px;">OFFICIAL ACADEMIC REPORT</span>'
            f'</div><div style="height:1px;background:#d0dae8;"></div></div>'
        )
        prof_bot = (
            f'<div style="margin-top:24px;">'
            f'<div style="height:1px;background:#d0dae8;"></div>'
            f'<div style="display:flex;justify-content:space-between;padding:6px 4px;">'
            f'<span style="font-size:10px;color:#8b9bb4;">سري — للاستخدام الأكاديمي فقط</span>'
            f'<span style="font-size:10px;color:#8b9bb4;">Confidential — Academic Use Only</span>'
            f'</div>'
            f'<div style="height:2px;background:{a};"></div>'
            f'<div style="height:5px;background:{p};margin-top:3px;"></div></div>'
        )
    elif template_name == "royal":
        prof_top = (
            f'<div style="margin-bottom:22px;text-align:center;">'
            f'<div style="height:4px;background:linear-gradient(to {gdir},{p},{a},{p});border-radius:2px;"></div>'
            f'<div style="padding:8px 4px 5px;"><span style="font-size:12px;color:{a};font-weight:700;letter-spacing:3px;">✦ تقرير أكاديمي جامعي ✦</span></div>'
            f'<div style="height:1px;background:{a};opacity:0.35;"></div></div>'
        )
        prof_bot = (
            f'<div style="margin-top:22px;text-align:center;">'
            f'<div style="height:1px;background:{a};opacity:0.35;"></div>'
            f'<div style="padding:6px 4px;"><span style="font-size:11px;color:{a};letter-spacing:2px;">✦ إعداد أكاديمي رسمي — جميع الحقوق محفوظة ✦</span></div>'
            f'<div style="height:4px;background:linear-gradient(to {gdir},{p},{a},{p});border-radius:2px;"></div></div>'
        )
    elif template_name == "_custom":
        prof_top = (
            f'<div style="margin-bottom:16px;">'
            f'<div style="height:3px;background:linear-gradient(to {gdir},{p},{a},{p});border-radius:2px;"></div>'
            f'</div>'
        )
        prof_bot = (
            f'<div style="margin-top:16px;">'
            f'<div style="height:3px;background:linear-gradient(to {gdir},{p},{a},{p});border-radius:2px;"></div>'
            f'</div>'
        )
    else:
        prof_top = prof_bot = ""

    if not show_hf:
        prof_top = ""
        prof_bot = ""

    blocks_html = "\n".join(render_block(bl, tc_dict, lang) for bl in report.blocks)

    # بناء HTML العنوان حسب الشكل المختار
    if title_style == "formal":
        title_html = (
            f'<div style="text-align:center;margin-bottom:22px;padding-bottom:4px;">'
            f'<div style="height:3px;background:{p};margin-bottom:2px;border-radius:2px;"></div>'
            f'<div style="height:1px;background:{a};margin-bottom:12px;"></div>'
            f'<h1 style="color:{p};">{esc(report.title)}</h1>'
            f'<div style="height:1px;background:{a};margin-top:12px;"></div>'
            f'<div style="height:3px;background:{p};margin-top:2px;border-radius:2px;"></div>'
            f'</div>'
        )
    elif title_style == "classic":
        title_html = (
            f'<div style="text-align:center;margin-bottom:24px;'
            f'padding-bottom:14px;border-bottom:2px solid {a};">'
            f'<h1 style="color:{title_color};">{esc(report.title)}</h1>'
            f'</div>'
        )
    else:  # modern
        title_html = (
            f'<div style="text-align:center;margin-bottom:24px;'
            f'background:{p};padding:16px 20px;border-radius:6px;">'
            f'<h1 style="color:#ffffff;">{esc(report.title)}</h1>'
            f'</div>'
        )

    return f"""<!DOCTYPE html>
<html lang="{lang['lang_attr']}" dir="{dir_}">
<head>
<meta charset="UTF-8">
<style>
{_font_face_css()}
  @page {{
    size: A4;
    margin: {final_margin};
    border: {page_border};
    padding: {page_padding};
    background: {page_bg};
    {extra_css}
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: {font};
    direction: {dir_};
    text-align: justify;
    line-height: {line_height};
    color: {body_color};
    background: {page_bg};
    font-size: {font_size};
    margin: 0; padding: 0;
    word-spacing: 0.04em;
  }}
  p   {{ text-align: justify; margin: 0 0 10px 0; font-size: 1em; }}
  h1  {{ font-size: {title_size} !important; text-align: center;
         margin: 0; font-weight: 800; letter-spacing: 0.01em; }}
  h2  {{ font-size: 1.05em !important; text-align: {align}; font-weight: 700; margin: 0; }}
  li  {{ text-align: {align}; font-size: 1em; }}
  td, th {{ font-size: 0.95em; }}
  p, li {{ orphans: 2; widows: 2; }}
  .block-table, .block-stats, .block-comparison, .block-pros-cons {{ page-break-inside: avoid; }}
  h2 {{ page-break-after: avoid; orphans: 3; widows: 3; }}
</style>
</head>
<body>

{prof_top}

{title_html}

<div style="background:{box_bg};padding:16px 20px;border-radius:6px;
            margin:0 0 20px 0;{b_side}:4px solid {a};
            box-shadow:0 1px 4px rgba(0,0,0,0.07);">
  <h2 style="color:{p};font-weight:700;margin:0 0 10px 0;">
    📚 {lang['intro_label']}
  </h2>
  {text_to_paras(report.introduction, align)}
</div>

{blocks_html}

<div style="background:{box_bg};padding:16px 20px;border-radius:6px;
            margin:20px 0 0 0;{b_side}:4px solid {a};
            box-shadow:0 1px 4px rgba(0,0,0,0.07);">
  <h2 style="color:{p};font-weight:700;margin:0 0 10px 0;">
    🎯 {lang['conclusion_label']}
  </h2>
  {text_to_paras(report.conclusion, align)}
</div>

{prof_bot}

</body>
</html>"""


# ------------------- لوحات المفاتيح -------------------
def title_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton("👻 اتركه للشبح", callback_data="title_auto")]])

def lang_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(v["name"], callback_data=f"lang_{k}")]
        for k, v in LANGUAGES.items()
    ])

def depth_keyboard(is_free: bool = False):
    def _btn(k, v):
        locked = is_free and k not in FREE_DEPTHS
        return InlineKeyboardButton((LOCK + v["name"]) if locked else v["name"], callback_data=f"depth_{k}")
    free_rows    = [[_btn(k, v)] for k, v in DEPTH_OPTIONS.items() if k in FREE_DEPTHS]
    premium_rows = [[_btn(k, v)] for k, v in DEPTH_OPTIONS.items() if k not in FREE_DEPTHS]
    rows = free_rows + premium_rows
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_choosing_title")])
    return InlineKeyboardMarkup(rows)

def style_mode_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎭 قوالب جاهزة",   callback_data="style_preset")],
        [InlineKeyboardButton("🎨 تخصيص كامل ✨", callback_data="style_custom")],
        [InlineKeyboardButton("🔙 رجوع",          callback_data="back_choosing_depth")],
    ])

def template_keyboard(is_free: bool = False):
    def _btn(k, v):
        locked = is_free and k not in FREE_TEMPLATES
        return InlineKeyboardButton((LOCK + v["name"]) if locked else v["name"], callback_data=f"tpl_{k}")
    free_rows    = [[_btn(k, v)] for k, v in TEMPLATES.items() if k in FREE_TEMPLATES]
    premium_rows = [[_btn(k, v)] for k, v in TEMPLATES.items() if k not in FREE_TEMPLATES]
    rows = free_rows + premium_rows
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_choosing_style_mode")])
    return InlineKeyboardMarkup(rows)

def font_size_keyboard(is_free: bool = False):
    def _btn(k, v):
        locked = is_free and k not in FREE_FONT_SIZES
        return InlineKeyboardButton((LOCK + v["label"]) if locked else v["label"], callback_data=f"fsize_{k}")
    free_rows    = [[_btn(k, v)] for k, v in CUSTOM_FONT_SIZES.items() if k in FREE_FONT_SIZES]
    premium_rows = [[_btn(k, v)] for k, v in CUSTOM_FONT_SIZES.items() if k not in FREE_FONT_SIZES]
    rows = free_rows + premium_rows
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_choosing_style_mode")])
    return InlineKeyboardMarkup(rows)

def font_keyboard_for_language(lang_key, is_free: bool = False):
    fonts = get_fonts_by_language(lang_key)
    free_set = FREE_FONTS_AR if lang_key == "ar" else FREE_FONTS_EN
    def _btn(k, v):
        locked = is_free and k not in free_set
        return InlineKeyboardButton((LOCK + v["label"]) if locked else v["label"], callback_data=f"cfont_{k}")
    free_items    = [(k, v) for k, v in fonts.items() if k in free_set]
    premium_items = [(k, v) for k, v in fonts.items() if k not in free_set]
    all_items = free_items + premium_items
    rows = []
    for i in range(0, len(all_items), 2):
        row = [_btn(*all_items[i])]
        if i + 1 < len(all_items):
            row.append(_btn(*all_items[i + 1]))
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_choosing_font_size")])
    return InlineKeyboardMarkup(rows)

def colors_keyboard(is_free: bool = False):
    def _btn(k, v):
        locked = is_free and k not in FREE_COLORS
        return InlineKeyboardButton((LOCK + v["label"]) if locked else v["label"], callback_data=f"color_{k}")
    free_items    = [(k, v) for k, v in CUSTOM_COLORS.items() if k in FREE_COLORS]
    premium_items = [(k, v) for k, v in CUSTOM_COLORS.items() if k not in FREE_COLORS]
    all_items = free_items + premium_items
    rows = []
    for i in range(0, len(all_items), 2):
        row = [_btn(*all_items[i])]
        if i + 1 < len(all_items):
            row.append(_btn(*all_items[i + 1]))
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_choosing_font")])
    return InlineKeyboardMarkup(rows)

def line_height_keyboard():
    rows = [
        [InlineKeyboardButton(v["label"], callback_data=f"lh_{k}")]
        for k, v in LINE_HEIGHTS.items()
    ]
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_choosing_colors")])
    return InlineKeyboardMarkup(rows)

def page_margin_keyboard(is_free: bool = False):
    def _btn(k, v):
        locked = is_free and k not in FREE_MARGINS
        return InlineKeyboardButton((LOCK + v["label"]) if locked else v["label"], callback_data=f"pm_{k}")
    free_rows    = [[_btn(k, v)] for k, v in PAGE_MARGINS.items() if k in FREE_MARGINS]
    premium_rows = [[_btn(k, v)] for k, v in PAGE_MARGINS.items() if k not in FREE_MARGINS]
    rows = free_rows + premium_rows
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_choosing_line_height")])
    return InlineKeyboardMarkup(rows)

def pros_cons_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ نعم، أضف مزايا وعيوب", callback_data="pc_yes")],
        [InlineKeyboardButton("❌ لا، بدون مزايا/عيوب",  callback_data="pc_no")],
        [InlineKeyboardButton("🔙 رجوع",                 callback_data="back_choosing_page_margin")],
    ])

def tables_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 نعم، أضف جداول",   callback_data="tbl_yes")],
        [InlineKeyboardButton("❌ لا، بدون جداول",    callback_data="tbl_no")],
        [InlineKeyboardButton("🔙 رجوع",             callback_data="back_choosing_pros_cons")],
    ])

def header_style_keyboard(is_free: bool = False):
    def _btn(k, v):
        locked = is_free and k not in FREE_HEADER_STYLES
        return InlineKeyboardButton((LOCK + v["label"]) if locked else v["label"], callback_data=f"hs_{k}")
    free_rows    = [[_btn(k, v)] for k, v in HEADER_STYLES.items() if k in FREE_HEADER_STYLES]
    premium_rows = [[_btn(k, v)] for k, v in HEADER_STYLES.items() if k not in FREE_HEADER_STYLES]
    rows = free_rows + premium_rows
    rows.append([InlineKeyboardButton("🔙 رجوع", callback_data="back_choosing_tables")])
    return InlineKeyboardMarkup(rows)

def show_header_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(v["label"], callback_data=f"sh_{k}")]
        for k, v in SHOW_HEADER_FOOTER.items()
    ])

def comparison_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 نعم، أضف جدول مقارنة!", callback_data="comp_yes")],
        [InlineKeyboardButton("❌ لا شكراً",               callback_data="comp_no")],
        [InlineKeyboardButton("🔙 رجوع",                  callback_data="back_choosing_header_style")],
    ])


# ------------------- دالة مساعدة لنص الطابور -------------------
def build_queue_text(session: dict, pos: int) -> str:
    if pos == 1:
        status = "✍️ 👻 <b>الشبح يكتب تقريرك الآن...</b>"
    else:
        status = f"⏳ <b>في الطابور — الترتيب {pos}</b>\n✍️ 👻 <b>الشبح يكتب تقريرك قريباً...</b>"
    patience = "\n\n⏱ <b>قد يستغرق الإنشاء عدة دقائق، الجودة تستحق الانتظار! ☕</b>"
    tip = "\n\n✨ <b>نصيحة: جرّب خيار التخصيص الكامل لتقرير فريد من نوعه!</b>"
    return f"{status}{patience}{tip}"


# ------------------- معالجات التيليجرام -------------------
async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالج زر الرجوع في خطوات الاختيار"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    target = query.data.replace("back_", "")

    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية. أرسل موضوعاً جديداً.")
        return

    session = user_sessions[user_id]
    session["state"] = target
    is_free = not is_premium_user(user_id)

    if target == "choosing_title":
        session.pop("custom_title", None)
        await query.edit_message_text(
            "📌 <b>هل تريد تحديد عنوان للتقرير؟</b>\n"
            "<i>اكتب العنوان، أو دع الشبح يختاره 👇</i>",
            reply_markup=title_keyboard(), parse_mode='HTML'
        )
    elif target == "choosing_depth":
        await query.edit_message_text(
            "📏 <b>اختر عمق التقرير:</b>",
            reply_markup=depth_keyboard(is_free), parse_mode='HTML'
        )
    elif target == "choosing_style_mode":
        await query.edit_message_text(
            "🎨 <b>كيف تريد تصميم تقريرك؟</b>\n\n"
            "🎭 <b>قوالب جاهزة</b> — 6 قوالب احترافية\n"
            "✨ <b>تخصيص كامل</b> — خط، ألوان، مقارنة خاصة",
            reply_markup=style_mode_keyboard(), parse_mode='HTML'
        )
    elif target == "choosing_template":
        await query.edit_message_text(
            "🎭 <b>اختر قالباً من مجموعة Repooreto:</b>",
            reply_markup=template_keyboard(is_free), parse_mode='HTML'
        )
    elif target == "choosing_font_size":
        await query.edit_message_text(
            "📐 <b>الخطوة 1 من 8 — حجم الخط:</b>\n"
            "اختر الحجم الذي يريح عينيك 👇",
            reply_markup=font_size_keyboard(is_free), parse_mode='HTML'
        )
    elif target == "choosing_font":
        lang_key = session.get("language", "ar")
        await query.edit_message_text(
            "✍️ <b>الخطوة 2 من 8 — نوع الخط:</b>\n"
            "اختر الخط المناسب 👇",
            reply_markup=font_keyboard_for_language(lang_key, is_free), parse_mode='HTML'
        )
    elif target == "choosing_colors":
        await query.edit_message_text(
            "🎨 <b>الخطوة 3 من 8 — نظام الألوان:</b>\n"
            "اختر الروح البصرية لتقريرك 👇",
            reply_markup=colors_keyboard(is_free), parse_mode='HTML'
        )
    elif target == "choosing_line_height":
        await query.edit_message_text(
            "📏 <b>الخطوة 4 من 8 — تباعد الأسطر:</b>\n"
            "اختر المسافة بين السطور 👇",
            reply_markup=line_height_keyboard(), parse_mode='HTML'
        )
    elif target == "choosing_page_margin":
        await query.edit_message_text(
            "📐 <b>الخطوة 5 من 8 — هوامش الصفحة:</b>\n"
            "اختر حجم الهوامش 👇",
            reply_markup=page_margin_keyboard(is_free), parse_mode='HTML'
        )
    elif target == "choosing_pros_cons":
        await query.edit_message_text(
            "✅❌ <b>الخطوة 6 من 8 — المزايا والعيوب:</b>\n"
            "هل تريد تضمين أقسام المزايا والعيوب في التقرير؟",
            reply_markup=pros_cons_keyboard(), parse_mode='HTML'
        )
    elif target == "choosing_tables":
        await query.edit_message_text(
            "📊 <b>الخطوة 7 من 8 — الجداول:</b>\n"
            "هل تريد تضمين جداول في التقرير؟",
            reply_markup=tables_keyboard(), parse_mode='HTML'
        )
    elif target == "choosing_header_style":
        await query.edit_message_text(
            "🎨 <b>الخطوة 8 من 8 — شكل العنوان الرئيسي:</b>\n"
            "اختر كيف يظهر عنوان تقريرك 👇",
            reply_markup=header_style_keyboard(is_free), parse_mode='HTML'
        )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_sessions:
        user_sessions.pop(user_id, None)
        queue_positions.pop(user_id, None)
        await update.message.reply_text("❌ <b>تم إلغاء الجلسة.</b>\n\n👻 أرسل موضوعاً جديداً لبدء تقرير جديد.", parse_mode='HTML')
    else:
        await update.message.reply_text("ℹ️ لا توجد جلسة نشطة.\n\n👻 أرسل موضوعاً لبدء تقرير جديد.", parse_mode='HTML')


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user = update.effective_user
    register(user_id, user.username or "", user.full_name or "")
    user_sessions.pop(user_id, None)
    name = user.first_name
    admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")

    # رسالة 1 — تعريف بالبوت
    await update.message.reply_text(
        f"👻 <b>أهلاً {name}! أنا Repooreto</b>\n"
        f"الشبح الذي يكتب تقاريرك الجامعية! 🎓\n\n"
        f"✨ <b>كيف أعمل؟</b>\n"
        f"1️⃣ أرسل موضوع تقريرك\n"
        f"2️⃣ اختر اللغة\n"
        f"3️⃣ أجب على أسئلتي الذكية 🧠\n"
        f"4️⃣ اختر العمق والتصميم:\n"
        f"   • 🎭 <b>قوالب جاهزة</b> — 6 قوالب احترافية\n"
        f"   • 🎨 <b>تخصيص كامل</b> — خط، ألوان، مقارنة خاصة ✨\n"
        f"5️⃣ استلم تقريرك PDF! 🎉",
        parse_mode='HTML'
    )

    # رسالة 2 — حالة الاشتراك
    remaining = get_remaining(user_id)
    u = _get_user(user_id)
    if u and u["is_active"]:
        until = str(u["expires_at"])[:10] if u["expires_at"] else "—"
        status_msg = (
            f"✅ <b>أنت مشترك!</b>\n"
            f"اشتراكك فعّال حتى: <b>{until}</b>\n\n"
            f"👻 <b>أرسل موضوع تقريرك الآن!</b>"
        )
    elif remaining > 0:
        status_msg = (
            f"🆓 <b>تجربة مجانية</b>\n"
            f"متبقٍ لك: <b>{remaining} من {FREE_LIMIT}</b> تقارير مجانية.\n\n"
            f"💳 للاشتراك بعد انتهاء التجربة تواصل مع: @{admin_user}\n\n"
            f"👻 <b>أرسل موضوع تقريرك الآن!</b>"
        )
    else:
        status_msg = (
            f"🔒 <b>انتهت تجربتك المجانية!</b>\n\n"
            f"📩 للاشتراك تواصل مع: @{admin_user}\n"
            f"🆔 رقمك: <code>{user_id}</code>"
        )
    await update.message.reply_text(status_msg, parse_mode='HTML')


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    # تسجيل المستخدم والتحقق من الاشتراك
    user = update.effective_user
    register(user_id, user.username or "", user.full_name or "")
    allowed, block_msg = check_access(user_id)
    if not allowed:
        await update.message.reply_text(block_msg, parse_mode='HTML')
        return

    if user_id in user_sessions:
        session = user_sessions[user_id]
        state = session.get("state", "")

        if state == "answering":
            answers = session.setdefault("answers", [])
            questions = session.get("dynamic_questions", [])
            answers.append(text)
            if len(answers) < len(questions):
                nq = questions[len(answers)]
                q_num = len(answers) + 1
                total = len(questions)
                await update.message.reply_text(
                    f"✅ تم تسجيل إجابتك.\n\n❓ <b>السؤال {q_num}/{total}:</b>\n{nq}\n\n<i>اكتب إجابتك 👇</i>",
                    parse_mode='HTML'
                )
            else:
                session["state"] = "choosing_title"
                await update.message.reply_text(
                    "✅ <b>ممتاز! تم تسجيل جميع إجاباتك.</b>\n\n"
                    "📌 <b>هل تريد تحديد عنوان للتقرير؟</b>\n"
                    "<i>اكتب العنوان، أو دع الشبح يختاره 👇</i>",
                    reply_markup=title_keyboard(), parse_mode='HTML'
                )
            return

        if state == "choosing_title":
            session["custom_title"] = text
            session["state"] = "choosing_depth"
            is_free = not is_premium_user(user_id)
            await update.message.reply_text(
                f"✅ <b>العنوان:</b> <i>{esc(text)}</i>\n\n📏 <b>اختر عمق التقرير:</b>",
                reply_markup=depth_keyboard(is_free), parse_mode='HTML'
            )
            return

        if state == "entering_comparison":
            session["comparison_query"] = text
            session["state"] = "in_queue"
            pos = report_queue.qsize() + 1
            queue_positions[user_id] = pos
            status = await update.message.reply_text(build_queue_text(session, pos), parse_mode='HTML')
            await report_queue.put((user_id, session.copy(), status.message_id))
            return

        guidance = STATE_GUIDANCE.get(state, "⏳ جاري المعالجة... أرسل /cancel للبدء من جديد.")
        await update.message.reply_text(guidance, parse_mode='HTML')
        return

    if len(text) < 5:
        await update.message.reply_text("👻 الموضوع قصير جداً! أرسل موضوعاً أوضح.")
        return
    if len(text) > 250:
        await update.message.reply_text("👻 الموضوع طويل جداً! اختصره لأقل من 250 حرف.")
        return

    user_sessions[user_id] = {"topic": text, "state": "choosing_lang"}
    safe = text.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')

    # تذكير بالمحاولات المتبقية
    remaining = get_remaining(user_id)
    admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")
    if remaining != 999:
        trial_note = f"\n\n⚠️ <i>متبقٍ لك {remaining} تقرير مجاني. للاشتراك: @{admin_user}</i>"
    else:
        trial_note = ""

    await update.message.reply_text(
        f"📝 <b>الموضوع:</b> <i>{safe}</i>{trial_note}\n\n🌐 <b>اختر لغة التقرير:</b>",
        reply_markup=lang_keyboard(), parse_mode='HTML'
    )


async def title_auto_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    session = user_sessions[user_id]
    if session.get("state") != "choosing_title":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    session.pop("custom_title", None)
    session["state"] = "choosing_depth"
    is_free = not is_premium_user(user_id)
    await query.edit_message_text(
        "👻 <b>سيختار الشبح العنوان المناسب!</b>\n\n📏 <b>اختر عمق التقرير:</b>",
        reply_markup=depth_keyboard(is_free), parse_mode='HTML'
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    lang = query.data.replace("lang_", "")
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    session = user_sessions[user_id]
    session["language"] = lang
    session["state"] = "generating_questions"
    await query.edit_message_text(
        f"✅ <b>اللغة:</b> {LANGUAGES[lang]['name']}\n\n👻 <i>الشبح يحلل موضوعك ويولّد الأسئلة...</i>",
        parse_mode='HTML'
    )
    try:
        loop = asyncio.get_running_loop()
        questions = await loop.run_in_executor(None, generate_dynamic_questions, session["topic"], lang)
        if not questions:
            raise ValueError("no questions")
        session["dynamic_questions"] = questions
        session["state"] = "answering"
        total = len(questions)
        q_word = "سؤال" if total == 1 else "أسئلة"
        hint = "\n\n💡 <i>يمكنك طلب جداول، مزايا/عيوب، أو مقارنات في إجاباتك.</i>"
        await query.edit_message_text(
            f"🧠 <b>لديّ {total} {q_word} قبل الكتابة!</b>{hint}\n\n"
            f"❓ <b>السؤال 1/{total}:</b>\n{questions[0]}\n\n<i>اكتب إجابتك 👇</i>",
            parse_mode='HTML'
        )
    except Exception as e:
        logger.error(f"Questions failed: {e}", exc_info=True)
        session["dynamic_questions"] = []
        session["answers"] = []
        session["state"] = "choosing_depth"
        is_free = not is_premium_user(user_id)
        await query.edit_message_text(
            "⚠️ تعذّر توليد الأسئلة. سنكمل مباشرةً.\n\n📏 <b>اختر عمق التقرير:</b>",
            reply_markup=depth_keyboard(is_free), parse_mode='HTML'
        )


async def depth_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    depth = query.data.replace("depth_", "")
    if user_id not in user_sessions:
        await query.answer()
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_depth":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    is_free = not is_premium_user(user_id)
    if is_free and depth not in FREE_DEPTHS:
        admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")
        await query.answer(
            f"🔒 هذا الخيار للمشتركين فقط!\nتواصل مع @{admin_user} للاشتراك.",
            show_alert=True
        )
        return
    await query.answer()
    user_sessions[user_id]["depth"] = depth
    user_sessions[user_id]["state"] = "choosing_style_mode"
    await query.edit_message_text(
        "🎨 <b>كيف تريد تصميم تقريرك؟</b>\n\n"
        "🎭 <b>قوالب جاهزة</b> — 6 قوالب احترافية جاهزة للاستخدام\n"
        "✨ <b>تخصيص كامل</b> — اختر الخط، الألوان، وأضف مقارنة خاصة",
        reply_markup=style_mode_keyboard(), parse_mode='HTML'
    )


async def style_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    mode = query.data.replace("style_", "")
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_style_mode":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    session = user_sessions[user_id]
    is_free = not is_premium_user(user_id)
    if mode == "preset":
        session["custom_mode"] = False
        session["state"] = "choosing_template"
        await query.edit_message_text(
            "🎭 <b>اختر قالباً من مجموعة Repooreto:</b>",
            reply_markup=template_keyboard(is_free), parse_mode='HTML'
        )
    else:
        session["custom_mode"] = True
        session["custom_font_size_key"] = "medium"
        session["custom_font_key"] = "cairo" if session.get("language", "ar") == "ar" else "roboto"
        session["custom_color_key"] = "royal_blue"
        session["custom_line_height"] = "normal"
        session["custom_page_margin"] = "medium"
        session["custom_header_style"] = "formal"
        session["include_pros_cons"] = True
        session["include_tables"] = True
        session["state"] = "choosing_font_size"
        await query.edit_message_text(
            "🎨 <b>رحلة التخصيص بدأت! 👻</b>\n\n"
            "📐 <b>الخطوة 1 من 8 — حجم الخط:</b>\n"
            "اختر الحجم الذي يريح عينيك 👇",
            reply_markup=font_size_keyboard(is_free), parse_mode='HTML'
        )


async def font_size_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    key = query.data.replace("fsize_", "")
    if user_id not in user_sessions:
        await query.answer()
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_font_size":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    is_free = not is_premium_user(user_id)
    if is_free and key not in FREE_FONT_SIZES:
        admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")
        await query.answer(f"🔒 هذا الخيار للمشتركين فقط!\nتواصل مع @{admin_user} للاشتراك.", show_alert=True)
        return
    await query.answer()
    session = user_sessions[user_id]
    session["custom_font_size_key"] = key
    session["state"] = "choosing_font"
    lang_key = session.get("language", "ar")
    await query.edit_message_text(
        "✍️ <b>الخطوة 2 من 8 — نوع الخط:</b>\n"
        "اختر الخط المناسب 👇",
        reply_markup=font_keyboard_for_language(lang_key, is_free), parse_mode='HTML'
    )


async def font_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    key = query.data.replace("cfont_", "")
    if user_id not in user_sessions:
        await query.answer()
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_font":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    is_free = not is_premium_user(user_id)
    lang_key = user_sessions[user_id].get("language", "ar")
    free_set = FREE_FONTS_AR if lang_key == "ar" else FREE_FONTS_EN
    if is_free and key not in free_set:
        admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")
        await query.answer(f"🔒 هذا الخيار للمشتركين فقط!\nتواصل مع @{admin_user} للاشتراك.", show_alert=True)
        return
    await query.answer()
    session = user_sessions[user_id]
    session["custom_font_key"] = key
    session["state"] = "choosing_colors"
    await query.edit_message_text(
        "🎨 <b>الخطوة 3 من 8 — نظام الألوان:</b>\n"
        "اختر الروح البصرية لتقريرك 👇",
        reply_markup=colors_keyboard(is_free), parse_mode='HTML'
    )


async def colors_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    key = query.data.replace("color_", "")
    if user_id not in user_sessions:
        await query.answer()
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_colors":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    is_free = not is_premium_user(user_id)
    if is_free and key not in FREE_COLORS:
        admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")
        await query.answer(f"🔒 هذا الخيار للمشتركين فقط!\nتواصل مع @{admin_user} للاشتراك.", show_alert=True)
        return
    await query.answer()
    session = user_sessions[user_id]
    session["custom_color_key"] = key
    session["state"] = "choosing_line_height"
    await query.edit_message_text(
        "📏 <b>الخطوة 4 من 8 — تباعد الأسطر:</b>\n"
        "اختر المسافة بين السطور 👇",
        reply_markup=line_height_keyboard(), parse_mode='HTML'
    )


async def line_height_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    key = query.data.replace("lh_", "")
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_line_height":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    session = user_sessions[user_id]
    session["custom_line_height"] = key
    session["state"] = "choosing_page_margin"
    is_free = not is_premium_user(user_id)
    await query.edit_message_text(
        "📐 <b>الخطوة 5 من 8 — هوامش الصفحة:</b>\n"
        "اختر حجم الهوامش 👇",
        reply_markup=page_margin_keyboard(is_free), parse_mode='HTML'
    )


async def page_margin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    key = query.data.replace("pm_", "")
    if user_id not in user_sessions:
        await query.answer()
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_page_margin":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    is_free = not is_premium_user(user_id)
    if is_free and key not in FREE_MARGINS:
        admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")
        await query.answer(f"🔒 هذا الخيار للمشتركين فقط!\nتواصل مع @{admin_user} للاشتراك.", show_alert=True)
        return
    await query.answer()
    session = user_sessions[user_id]
    session["custom_page_margin"] = key
    session["state"] = "choosing_pros_cons"
    await query.edit_message_text(
        "✅❌ <b>الخطوة 6 من 8 — المزايا والعيوب:</b>\n"
        "هل تريد تضمين أقسام المزايا والعيوب في التقرير؟\n"
        "<i>تُضاف كجداول مقارنة جانبية</i>",
        reply_markup=pros_cons_keyboard(), parse_mode='HTML'
    )


async def pros_cons_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    choice = query.data  # "pc_yes" or "pc_no"
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_pros_cons":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    session = user_sessions[user_id]
    session["include_pros_cons"] = (choice == "pc_yes")
    session["state"] = "choosing_tables"
    is_free = not is_premium_user(user_id)
    await query.edit_message_text(
        "📊 <b>الخطوة 7 من 8 — الجداول:</b>\n"
        "هل تريد تضمين جداول في التقرير؟\n"
        "<i>تشمل: جداول البيانات، جداول المقارنة، الإحصائيات</i>",
        reply_markup=tables_keyboard(), parse_mode='HTML'
    )


async def tables_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    choice = query.data  # "tbl_yes" or "tbl_no"
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_tables":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    session = user_sessions[user_id]
    session["include_tables"] = (choice == "tbl_yes")
    session["state"] = "choosing_header_style"
    is_free = not is_premium_user(user_id)
    await query.edit_message_text(
        "🎨 <b>الخطوة 8 من 8 — شكل العنوان الرئيسي:</b>\n"
        "اختر كيف يظهر عنوان تقريرك 👇",
        reply_markup=header_style_keyboard(is_free), parse_mode='HTML'
    )


async def header_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    key = query.data.replace("hs_", "")
    if user_id not in user_sessions:
        await query.answer()
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_header_style":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    is_free = not is_premium_user(user_id)
    if is_free and key not in FREE_HEADER_STYLES:
        admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")
        await query.answer(f"🔒 هذا الخيار للمشتركين فقط!\nتواصل مع @{admin_user} للاشتراك.", show_alert=True)
        return
    await query.answer()
    session = user_sessions[user_id]
    session["custom_header_style"] = key
    session["state"] = "asking_comparison"
    await query.edit_message_text(
        "📊 <b>هل تريد إضافة جدول مقارنة خاص في التقرير؟</b>\n"
        "<i>مثال: مقارنة Python مع Java، أو الطاقة الشمسية مع النووية...</i>",
        reply_markup=comparison_keyboard(), parse_mode='HTML'
    )


async def comp_yes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "asking_comparison":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    user_sessions[user_id]["state"] = "entering_comparison"
    await query.edit_message_text(
        "📊 <b>اكتب الشيئين اللذين تريد مقارنتهما:</b>\n\n"
        "💡 <i>أمثلة:</i>\n"
        "• <code>Python مقابل Java</code>\n"
        "• <code>العمل الحر مقابل الوظيفة</code>\n"
        "• <code>الطاقة الشمسية مقابل النووية</code>\n\n"
        "✏️ <b>اكتب الآن 👇</b>",
        parse_mode='HTML'
    )


async def comp_no_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "asking_comparison":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    session = user_sessions[user_id]
    session.pop("comparison_query", None)
    session["state"] = "in_queue"
    pos = report_queue.qsize() + 1
    queue_positions[user_id] = pos
    await query.edit_message_text(build_queue_text(session, pos), parse_mode='HTML')
    await report_queue.put((user_id, session.copy(), query.message.message_id))


async def template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    tpl = query.data.replace("tpl_", "")
    if user_id not in user_sessions:
        await query.answer()
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    if user_sessions[user_id].get("state") != "choosing_template":
        await query.answer("هذا الزر لم يعد فعالاً.", show_alert=True)
        return
    is_free = not is_premium_user(user_id)
    if is_free and tpl not in FREE_TEMPLATES:
        admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")
        await query.answer(f"🔒 هذا القالب للمشتركين فقط!\nتواصل مع @{admin_user} للاشتراك.", show_alert=True)
        return
    await query.answer()
    session = user_sessions[user_id]
    session["template"] = tpl
    session["custom_mode"] = False
    session["state"] = "in_queue"
    pos = report_queue.qsize() + 1
    queue_positions[user_id] = pos
    await query.edit_message_text(build_queue_text(session, pos), parse_mode='HTML')
    await report_queue.put((user_id, session.copy(), query.message.message_id))


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update error: {context.error}", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text("❌ حدث خطأ. حاول مرة أخرى.")
    except Exception:
        pass



# ═══════════════════════════════════════════════════════════════
# نظام الاشتراكات — PostgreSQL (Supabase)
# ═══════════════════════════════════════════════════════════════
import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool
from contextlib import contextmanager
from datetime import datetime, timedelta

FREE_LIMIT = 3
SUB_DAYS = 20
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "0").split(",") if x.strip().isdigit()]
MAIN_BOT_USERNAME = os.getenv("MAIN_BOT_USERNAME", "YourMainBot")


_db_pool: ThreadedConnectionPool = None

def _get_db_pool() -> ThreadedConnectionPool:
    global _db_pool
    if _db_pool is None:
        _db_pool = ThreadedConnectionPool(
            minconn=1, maxconn=10,
            dsn=os.getenv("DATABASE_URL"),
            cursor_factory=psycopg2.extras.RealDictCursor
        )
    return _db_pool

@contextmanager
def _db_conn():
    pool = _get_db_pool()
    conn = pool.getconn()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        pool.putconn(conn)


def _init_db():
    with _db_conn() as c:
        with c.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id    BIGINT PRIMARY KEY,
                    username   TEXT DEFAULT '',
                    full_name  TEXT DEFAULT '',
                    used       INTEGER DEFAULT 0,
                    is_active  INTEGER DEFAULT 0,
                    expires_at TIMESTAMP DEFAULT NULL,
                    joined_at  TIMESTAMP DEFAULT NOW()
                )
            """)
        c.commit()


_init_db()


def register(user_id: int, username: str = "", full_name: str = ""):
    with _db_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "INSERT INTO users (user_id, username, full_name) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (user_id, username, full_name)
            )
        c.commit()


def _get_user(user_id: int):
    with _db_conn() as c:
        with c.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id=%s", (user_id,))
            row = cur.fetchone()
    return dict(row) if row else None


def _expire_user(user_id: int):
    with _db_conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE users SET is_active=0 WHERE user_id=%s", (user_id,))
        c.commit()


def check_access(user_id: int) -> tuple:
    u = _get_user(user_id)
    if not u:
        return True, ""
    if u["is_active"] and u["expires_at"]:
        exp = u["expires_at"] if isinstance(u["expires_at"], datetime) else datetime.strptime(str(u["expires_at"])[:19], "%Y-%m-%d %H:%M:%S")
        if datetime.now() > exp:
            _expire_user(user_id)
            u["is_active"] = 0
    if u["is_active"]:
        return True, ""
    remaining = FREE_LIMIT - u["used"]
    if remaining > 0:
        return True, ""
    admin_user = os.getenv("MAIN_BOT_USERNAME", "Admin")
    return False, (
        "🔒 <b>انتهت تجربتك المجانية!</b>\n\n"
        f"استخدمت <b>{FREE_LIMIT}</b> تقارير مجانية.\n\n"
        "📩 <b>للاشتراك وفتح البوت:</b>\n"
        f"تواصل مع المسؤول مباشرة 👇\n"
        f"@{admin_user}\n\n"
        f"🆔 رقمك: <code>{user_id}</code>\n"
        "<i>أرسل هذا الرقم للأدمن ليفعّلك فوراً ✅</i>"
    )


def get_remaining(user_id: int) -> int:
    u = _get_user(user_id)
    if not u:
        return FREE_LIMIT
    if u["is_active"]:
        return 999
    return max(0, FREE_LIMIT - u["used"])


def is_premium_user(user_id: int) -> bool:
    """يرجع True إذا كان المستخدم مشتركاً (غير مجاني)"""
    u = _get_user(user_id)
    if not u:
        return False
    return bool(u["is_active"])


def count_report(user_id: int):
    with _db_conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE users SET used = used + 1 WHERE user_id=%s", (user_id,))
        c.commit()


def sub_activate(user_id: int, days: int = SUB_DAYS) -> str:
    expires = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with _db_conn() as c:
        with c.cursor() as cur:
            cur.execute("INSERT INTO users (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,))
            cur.execute(
                "UPDATE users SET is_active=1, expires_at=%s WHERE user_id=%s",
                (expires, user_id)
            )
        c.commit()
    return expires


def sub_deactivate(user_id: int):
    with _db_conn() as c:
        with c.cursor() as cur:
            cur.execute("UPDATE users SET is_active=0, expires_at=NULL WHERE user_id=%s", (user_id,))
        c.commit()


def sub_get_user(user_id: int):
    return _get_user(user_id)


def sub_all_users() -> list:
    with _db_conn() as c:
        with c.cursor() as cur:
            cur.execute(
                "SELECT user_id, username, full_name, used, is_active, expires_at FROM users ORDER BY joined_at DESC"
            )
            rows = cur.fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════
# معالجات بوت الأدمن
# ═══════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════
# معالجات بوت الأدمن — نظام InlineKeyboard
# ═══════════════════════════════════════════════════════════════
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

admin_sessions = {}  # {user_id: state}

def _admin_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تفعيل مستخدم",     callback_data="adm_activate")],
        [InlineKeyboardButton("❌ إلغاء اشتراك",      callback_data="adm_deactivate")],
        [InlineKeyboardButton("🔍 معلومات مستخدم",   callback_data="adm_info")],
        [InlineKeyboardButton("🔎 بحث بالمعرف",      callback_data="adm_find")],
        [InlineKeyboardButton("👥 قائمة المستخدمين", callback_data="adm_users")],
        [InlineKeyboardButton("📊 إحصائيات",         callback_data="adm_stats")],
        [InlineKeyboardButton("📢 إرسال للجميع",     callback_data="adm_broadcast")],
    ])

_BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="adm_back")]])

async def admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("⛔️")
        return
    admin_sessions.pop(update.effective_user.id, None)
    await update.message.reply_text(
        "👋 <b>لوحة تحكم Repooreto</b>\n\nاختر عملية:",
        reply_markup=_admin_kb(), parse_mode='HTML'
    )

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    if not is_admin(uid):
        return
    d = query.data

    if d == "adm_back":
        admin_sessions.pop(uid, None)
        await query.edit_message_text(
            "👋 <b>لوحة تحكم Repooreto</b>\n\nاختر عملية:",
            reply_markup=_admin_kb(), parse_mode='HTML'
        )
        return

    if d == "adm_stats":
        users = sub_all_users()
        total   = len(users)
        active  = sum(1 for u in users if u["is_active"])
        trial   = sum(1 for u in users if not u["is_active"] and u["used"] < FREE_LIMIT)
        blocked = sum(1 for u in users if not u["is_active"] and u["used"] >= FREE_LIMIT)
        reports = sum(u["used"] for u in users)
        await query.edit_message_text(
            f"📊 <b>إحصائيات Repooreto</b>\n\n"
            f"👥 إجمالي: <b>{total}</b>\n"
            f"✅ مشتركون: <b>{active}</b>\n"
            f"🆓 في التجربة: <b>{trial}</b>\n"
            f"🔒 منتهية: <b>{blocked}</b>\n"
            f"📄 إجمالي التقارير: <b>{reports}</b>",
            reply_markup=_BACK_KB, parse_mode='HTML'
        )
        return

    if d == "adm_users":
        users = sub_all_users()
        if not users:
            await query.edit_message_text("لا يوجد مستخدمون بعد.", reply_markup=_BACK_KB)
            return
        import csv, io
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["user_id", "username", "full_name", "used", "status", "expires_at", "joined_at"])
        for u in users:
            if u["is_active"]:
                status = "مشترك"
            elif u["used"] < FREE_LIMIT:
                status = "تجربة"
            else:
                status = "منتهي"
            writer.writerow([
                u["user_id"],
                u["username"] or "",
                u["full_name"] or "",
                u["used"],
                status,
                str(u["expires_at"])[:10] if u["expires_at"] else "",
                str(u.get("joined_at", ""))[:10],
            ])
        csv_bytes = buf.getvalue().encode("utf-8-sig")  # utf-8-sig لدعم Excel
        await query.edit_message_text(f"📤 جاري إرسال قائمة {len(users)} مستخدم...", reply_markup=_BACK_KB)
        await context.bot.send_document(
            chat_id=uid,
            document=BytesIO(csv_bytes),
            filename="users.csv",
            caption=f"👥 <b>قائمة المستخدمين</b>\nالإجمالي: <b>{len(users)}</b>",
            parse_mode='HTML'
        )
        return

    # خيارات تحتاج إدخال نص
    _prompts = {
        "adm_activate":   ("✅ <b>تفعيل مستخدم</b>\n\nأرسل: <code>user_id [days]</code>\nمثال: <code>123456789 20</code>", "activate"),
        "adm_deactivate": ("❌ <b>إلغاء اشتراك</b>\n\nأرسل: <code>user_id</code>", "deactivate"),
        "adm_info":       ("🔍 <b>معلومات مستخدم</b>\n\nأرسل: <code>user_id</code>", "info"),
        "adm_find":       ("🔎 <b>بحث بالمعرف</b>\n\nأرسل: <code>username</code> أو <code>@username</code>", "find"),
        "adm_broadcast":  ("📢 <b>إرسال للجميع</b>\n\nأرسل نص الرسالة:", "broadcast"),
    }
    if d in _prompts:
        text, state = _prompts[d]
        admin_sessions[uid] = state
        await query.edit_message_text(text, reply_markup=_BACK_KB, parse_mode='HTML')


async def admin_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """معالجة النصوص في بوت الأدمن"""
    uid = update.effective_user.id
    if not is_admin(uid):
        return
    state = admin_sessions.get(uid)
    if not state:
        await update.message.reply_text(
            "👋 <b>لوحة تحكم Repooreto</b>\n\nاختر عملية:",
            reply_markup=_admin_kb(), parse_mode='HTML'
        )
        return

    text = update.message.text.strip()
    admin_sessions.pop(uid, None)

    if state == "activate":
        parts = text.split()
        try:
            target_uid = int(parts[0])
            days = int(parts[1]) if len(parts) > 1 else SUB_DAYS
        except (ValueError, IndexError):
            await update.message.reply_text("❌ صيغة خاطئة. مثال: <code>123456 20</code>", parse_mode='HTML', reply_markup=_admin_kb())
            return
        expires = sub_activate(target_uid, days)
        await update.message.reply_text(
            f"✅ <b>تم التفعيل!</b>\n👤 <code>{target_uid}</code>\n📅 حتى: <b>{expires[:10]}</b>\n⏱ {days} يوم",
            reply_markup=_admin_kb(), parse_mode='HTML'
        )
        try:
            if main_app_ref:
                await main_app_ref.bot.send_message(
                    chat_id=target_uid,
                    text=f"🎉 <b>تم تفعيل اشتراكك!</b>\n\n✅ مفتوح لمدة <b>{days} يوم</b>\n📅 ينتهي: <b>{expires[:10]}</b>\n\n👻 ابدأ الآن!",
                    parse_mode='HTML'
                )
        except Exception:
            pass
        return

    if state == "deactivate":
        try:
            target_uid = int(text)
        except ValueError:
            await update.message.reply_text("❌ أرسل user_id رقمياً.", reply_markup=_admin_kb())
            return
        sub_deactivate(target_uid)
        await update.message.reply_text(
            f"❌ تم إلغاء اشتراك <code>{target_uid}</code>",
            reply_markup=_admin_kb(), parse_mode='HTML'
        )
        try:
            if main_app_ref:
                await main_app_ref.bot.send_message(
                    chat_id=target_uid,
                    text="⚠️ <b>تم إيقاف اشتراكك.</b>\nتواصل مع الأدمن لتجديده.",
                    parse_mode='HTML'
                )
        except Exception:
            pass
        return

    if state == "info":
        try:
            target_uid = int(text)
        except ValueError:
            await update.message.reply_text("❌ أرسل user_id رقمياً.", reply_markup=_admin_kb())
            return
        u = sub_get_user(target_uid)
        if not u:
            await update.message.reply_text("❌ المستخدم غير موجود.", reply_markup=_admin_kb())
            return
        status    = "✅ مشترك" if u["is_active"] else ("🆓 تجربة" if u["used"] < FREE_LIMIT else "🔒 منتهي")
        until_val = str(u["expires_at"])[:10] if u["expires_at"] else "-"
        await update.message.reply_text(
            f"👤 <b>معلومات المستخدم</b>\n\n"
            f"🆔 ID: <code>{u['user_id']}</code>\n"
            f"📛 الاسم: {u['full_name'] or '-'}\n"
            f"👤 يوزر: @{u['username'] or '-'}\n"
            f"📊 الحالة: {status}\n"
            f"📄 التقارير: {u['used']}\n"
            f"📅 ينتهي: {until_val}",
            reply_markup=_admin_kb(), parse_mode='HTML'
        )
        return

    if state == "find":
        username = text.lstrip('@').lower()
        with _db_conn() as c:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT user_id, username, full_name, used, is_active, expires_at FROM users WHERE lower(username)=%s",
                    (username,)
                )
                rows = cur.fetchall()
        if not rows:
            await update.message.reply_text(f"❌ لا يوجد: @{username}", reply_markup=_admin_kb())
            return
        for r in rows:
            u      = dict(r)
            status = "✅ مشترك" if u["is_active"] else ("🆓 تجربة" if u["used"] < FREE_LIMIT else "🔒 منتهي")
            until  = str(u["expires_at"])[:10] if u["expires_at"] else "-"
            await update.message.reply_text(
                f"🔍 <b>نتيجة البحث</b>\n\n"
                f"🆔 ID: <code>{u['user_id']}</code>\n"
                f"📛 الاسم: {u['full_name'] or '-'}\n"
                f"👤 يوزر: @{u['username'] or '-'}\n"
                f"📊 الحالة: {status}\n"
                f"📄 التقارير: {u['used']}\n"
                f"📅 ينتهي: {until}",
                reply_markup=_admin_kb(), parse_mode='HTML'
            )
        return

    if state == "broadcast":
        users = sub_all_users()
        sent = 0; failed = 0
        await update.message.reply_text(f"📤 جاري الإرسال لـ {len(users)} مستخدم...")
        for u in users:
            try:
                if main_app_ref:
                    await main_app_ref.bot.send_message(
                        chat_id=u["user_id"],
                        text=f"📢 <b>رسالة من الإدارة:</b>\n\n{text}",
                        parse_mode='HTML'
                    )
                    sent += 1
                await asyncio.sleep(0.05)
            except Exception:
                failed += 1
        await update.message.reply_text(
            f"✅ <b>اكتمل الإرسال</b>\n📨 أُرسل: <b>{sent}</b>\n❌ فشل: <b>{failed}</b>",
            reply_markup=_admin_kb(), parse_mode='HTML'
        )


# ═══════════════════════════════════════════════════════════════
# بدء التشغيل — البوتان معاً
# ═══════════════════════════════════════════════════════════════
if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask started")

    main_token = os.getenv("TELEGRAM_TOKEN")
    admin_token = os.getenv("ADMIN_BOT_TOKEN")

    if not main_token:
        logger.error("❌ TELEGRAM_TOKEN missing")
        exit(1)
    if not admin_token:
        logger.error("❌ ADMIN_BOT_TOKEN missing")
        exit(1)

    async def run_all():
        global report_queue, main_app_ref

        main_app = (
            ApplicationBuilder()
            .token(main_token)
            .build()
        )
        main_app_ref = main_app  # حفظ المرجع لإرسال الإشعارات
        main_app.add_handler(CommandHandler('start', start))
        main_app.add_handler(CommandHandler('cancel', cancel))
        main_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        main_app.add_handler(CallbackQueryHandler(title_auto_callback,  pattern=r'^title_auto$'))
        main_app.add_handler(CallbackQueryHandler(language_callback,    pattern=r'^lang_'))
        main_app.add_handler(CallbackQueryHandler(depth_callback,       pattern=r'^depth_'))
        main_app.add_handler(CallbackQueryHandler(style_mode_callback,  pattern=r'^style_'))
        main_app.add_handler(CallbackQueryHandler(template_callback,    pattern=r'^tpl_'))
        main_app.add_handler(CallbackQueryHandler(font_size_callback,   pattern=r'^fsize_'))
        main_app.add_handler(CallbackQueryHandler(font_callback,        pattern=r'^cfont_'))
        main_app.add_handler(CallbackQueryHandler(colors_callback,      pattern=r'^color_'))
        main_app.add_handler(CallbackQueryHandler(line_height_callback, pattern=r'^lh_'))
        main_app.add_handler(CallbackQueryHandler(page_margin_callback, pattern=r'^pm_'))
        main_app.add_handler(CallbackQueryHandler(pros_cons_callback,   pattern=r'^pc_'))
        main_app.add_handler(CallbackQueryHandler(tables_callback,      pattern=r'^tbl_'))
        main_app.add_handler(CallbackQueryHandler(header_style_callback,pattern=r'^hs_'))
        main_app.add_handler(CallbackQueryHandler(comp_yes_callback,    pattern=r'^comp_yes$'))
        main_app.add_handler(CallbackQueryHandler(comp_no_callback,     pattern=r'^comp_no$'))
        main_app.add_handler(CallbackQueryHandler(back_callback,        pattern=r'^back_'))
        main_app.add_error_handler(error_handler)

        admin_app = ApplicationBuilder().token(admin_token).build()
        admin_app.add_handler(CommandHandler('start', admin_start))
        admin_app.add_handler(CallbackQueryHandler(admin_callback, pattern=r'^adm_'))
        admin_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, admin_message))

        # تهيئة الطابور قبل تشغيل البوتين
        await main_app.initialize()
        await admin_app.initialize()

        report_queue = asyncio.Queue()
        asyncio.create_task(queue_worker(main_app))
        logger.info("✅ Queue worker started")

        await main_app.start()
        await admin_app.start()
        await main_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        await admin_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
        logger.info("✅ Both bots are running!")

        try:
            await asyncio.Event().wait()
        finally:
            await main_app.updater.stop()
            await admin_app.updater.stop()
            await main_app.stop()
            await admin_app.stop()
            await main_app.shutdown()
            await admin_app.shutdown()

    try:
        asyncio.run(run_all())
    except (KeyboardInterrupt, SystemExit):
        logger.info("🛑 Shutting down...")

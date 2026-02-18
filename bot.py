import os
import threading
import logging
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
from jinja2 import Template
from typing import List
from io import BytesIO
from weasyprint import HTML
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ Academic Reports Bot - Production Ready!"

@flask_app.route('/health')
def health():
    return {"status": "healthy", "bot": "active", "version": "2.0"}, 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

user_sessions = {}

class Section(BaseModel):
    title: str = Field(description="عنوان القسم")
    content: str = Field(description="المحتوى")

class AcademicReport(BaseModel):
    title: str = Field(description="عنوان التقرير")
    introduction: str = Field(description="المقدمة")
    sections: List[Section] = Field(description="الأقسام")
    conclusion: str = Field(description="الخاتمة")

TEMPLATES = {
    "classic": {
        "name": "🎓 كلاسيكي أكاديمي",
        "html": """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
    @page { size: A4; margin: 2.5cm; }
    body { font-family: 'Traditional Arabic', 'Arial', sans-serif; direction: rtl; text-align: right; line-height: 1.9; color: #2c3e50; }
    .header { text-align: center; border-bottom: 4px solid #34495e; padding-bottom: 20px; margin-bottom: 40px; }
    h1 { color: #2c3e50; font-size: 32px; margin-bottom: 10px; }
    h2 { color: #34495e; margin-top: 30px; border-right: 5px solid #3498db; padding: 12px 15px; background: #ecf0f1; font-size: 22px; }
    p { text-align: justify; line-height: 1.9; margin-bottom: 16px; font-size: 15px; }
    .intro, .conclusion { background-color: #ecf0f1; padding: 25px; border-radius: 8px; margin: 25px 0; border-right: 5px solid #3498db; }
</style>
</head>
<body>
<div class="header"><h1>{{ title }}</h1></div>
<div class="intro"><h2>📚 المقدمة</h2>{{ intro | safe }}</div>
{% for section in sections %}
<div><h2>{{ loop.index }}. {{ section.title }}</h2>{{ section.content | safe }}</div>
{% endfor %}
<div class="conclusion"><h2>🎯 الخاتمة</h2>{{ conc | safe }}</div>
</body>
</html>
"""
    },
    "modern": {
        "name": "🚀 عصري حديث",
        "html": """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
    @page { size: A4; margin: 2cm; }
    body { font-family: 'Arial', sans-serif; direction: rtl; text-align: right; line-height: 1.8; color: #1a1a2e; }
    .container { background: white; padding: 40px; }
    h1 { text-align: center; color: #667eea; font-size: 36px; margin-bottom: 30px; font-weight: bold; }
    h2 { color: #667eea; margin-top: 35px; padding: 15px 20px; background: linear-gradient(90deg, #f8f9fa 0%, white 100%); border-right: 6px solid #764ba2; border-radius: 0 10px 10px 0; font-size: 24px; }
    p { text-align: justify; line-height: 1.8; margin-bottom: 18px; font-size: 15px; color: #2d3748; }
    .intro, .conclusion { background: #f5f7fa; padding: 30px; border-radius: 15px; margin: 30px 0; }
</style>
</head>
<body>
<div class="container">
<h1>{{ title }}</h1>
<div class="intro"><h2>🌟 المقدمة</h2>{{ intro | safe }}</div>
{% for section in sections %}
<div><h2>{{ loop.index }}. {{ section.title }}</h2>{{ section.content | safe }}</div>
{% endfor %}
<div class="conclusion"><h2>✨ الخاتمة</h2>{{ conc | safe }}</div>
</div>
</body>
</html>
"""
    },
    "minimal": {
        "name": "⚪ بسيط أنيق",
        "html": """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
    @page { size: A4; margin: 3cm; }
    body { font-family: 'Arial', sans-serif; direction: rtl; text-align: right; line-height: 2; color: #333; max-width: 800px; margin: 0 auto; }
    h1 { text-align: center; font-size: 32px; font-weight: 300; letter-spacing: 2px; margin-bottom: 40px; padding-bottom: 20px; border-bottom: 1px solid #e0e0e0; }
    h2 { font-size: 20px; font-weight: 500; margin-top: 40px; margin-bottom: 20px; color: #555; }
    p { text-align: justify; line-height: 2; margin-bottom: 20px; font-size: 14px; color: #666; }
    .section { margin-bottom: 50px; }
</style>
</head>
<body>
<h1>{{ title }}</h1>
<div class="section"><h2>المقدمة</h2>{{ intro | safe }}</div>
{% for section in sections %}
<div class="section"><h2>{{ section.title }}</h2>{{ section.content | safe }}</div>
{% endfor %}
<div class="section"><h2>الخاتمة</h2>{{ conc | safe }}</div>
</body>
</html>
"""
    },
    "professional": {
        "name": "💼 احترافي رسمي",
        "html": """
<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<style>
    @page { size: A4; margin: 2.5cm; }
    body { font-family: 'Traditional Arabic', 'Times New Roman', serif; direction: rtl; text-align: right; line-height: 1.9; color: #1a202c; }
    .letterhead { border: 3px solid #2c5282; padding: 30px; margin-bottom: 40px; background: linear-gradient(to bottom, #f7fafc 0%, white 100%); }
    h1 { text-align: center; color: #2c5282; font-size: 30px; margin: 0; letter-spacing: 1px; }
    h2 { color: #2c5282; margin-top: 35px; padding: 12px 20px; background: #edf2f7; border-right: 6px solid #2c5282; font-size: 22px; letter-spacing: 0.5px; }
    p { text-align: justify; line-height: 1.9; margin-bottom: 18px; font-size: 15px; }
    .section { margin-bottom: 40px; }
</style>
</head>
<body>
<div class="letterhead"><h1>{{ title }}</h1></div>
<div class="section"><h2>المقدمة</h2>{{ intro | safe }}</div>
{% for section in sections %}
<div class="section"><h2>{{ loop.index }}. {{ section.title }}</h2>{{ section.content | safe }}</div>
{% endfor %}
<div class="section"><h2>الخاتمة</h2>{{ conc | safe }}</div>
</body>
</html>
"""
    }
}

WRITING_STYLES = {
    "academic": {
        "name": "🎓 أكاديمي متقدم",
        "prompt": "اكتب بأسلوب أكاديمي رسمي جداً مع استخدام مصطلحات علمية ولغة فصحى متقدمة. استخدم جمل معقدة ومفردات متخصصة."
    },
    "simple": {
        "name": "📖 مبسط سهل",
        "prompt": "اكتب بأسلوب مبسط وسهل الفهم مناسب لطلاب المدارس. استخدم جمل قصيرة وواضحة وأمثلة بسيطة."
    },
    "detailed": {
        "name": "📚 تفصيلي شامل",
        "prompt": "اكتب بأسلوب تفصيلي جداً مع شرح كل نقطة بعمق. أضف أمثلة وتفاصيل دقيقة وتحليلات متعمقة."
    },
    "creative": {
        "name": "✨ إبداعي ملهم",
        "prompt": "اكتب بأسلوب إبداعي جذاب مع استخدام تشبيهات واستعارات. اجعل المحتوى ممتعاً وملهماً."
    },
    "formal": {
        "name": "💼 رسمي احترافي",
        "prompt": "اكتب بأسلوب رسمي احترافي مناسب للأعمال والمؤسسات. استخدم لغة محترمة ودقيقة."
    }
}

LANGUAGES = {
    "ar": {
        "name": "🇸🇦 العربية",
        "prompt_instruction": "اكتب التقرير باللغة العربية الفصحى.",
        "intro_label": "المقدمة",
        "conclusion_label": "الخاتمة",
        "html_lang": "ar",
        "html_dir": "rtl",
        "html_align": "right",
        "font": "'Traditional Arabic', 'Arial', sans-serif",
    },
    "en": {
        "name": "🇬🇧 English",
        "prompt_instruction": "Write the report entirely in English.",
        "intro_label": "Introduction",
        "conclusion_label": "Conclusion",
        "html_lang": "en",
        "html_dir": "ltr",
        "html_align": "left",
        "font": "'Arial', sans-serif",
    }
}

TEMPLATE_DOCX_STYLES = {
    "classic":      {"title_color": RGBColor(0x2c, 0x3e, 0x50), "heading_color": RGBColor(0x34, 0x49, 0x5e), "heading_bg": "ECF0F1", "body_size": 12},
    "modern":       {"title_color": RGBColor(0x66, 0x7e, 0xea), "heading_color": RGBColor(0x66, 0x7e, 0xea), "heading_bg": "F8F9FA", "body_size": 12},
    "minimal":      {"title_color": RGBColor(0x33, 0x33, 0x33), "heading_color": RGBColor(0x55, 0x55, 0x55), "heading_bg": None,      "body_size": 11},
    "professional": {"title_color": RGBColor(0x2c, 0x52, 0x82), "heading_color": RGBColor(0x2c, 0x52, 0x82), "heading_bg": "EDF2F7", "body_size": 12},
}

def generate_report(topic, style="academic", template="classic", language="ar"):
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise Exception("API Key غير موجود")

        logger.info(f"📝 Generating: {topic} | Style: {style} | Template: {template} | Lang: {language}")

        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0.5,
            google_api_key=api_key,
            max_retries=3
        )

        parser = PydanticOutputParser(pydantic_object=AcademicReport)

        style_instruction = WRITING_STYLES[style]["prompt"]
        lang_instruction = LANGUAGES[language]["prompt_instruction"]

        prompt = PromptTemplate(
            input_variables=["topic"],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            template=f"""You are a professional academic writer. Write a detailed and comprehensive report about:

Topic: {{topic}}

Writing style: {style_instruction}

Language instruction: {lang_instruction}

The report must contain:
- A comprehensive introduction (150-200 words)
- 3-4 main sections (each section 200-300 words)
- A concise conclusion (100-150 words)

IMPORTANT: You MUST include the "conclusion" field in your JSON output. Do not omit it under any circumstances.

{{format_instructions}}"""
        )

        report = None
        for attempt in range(3):
            try:
                report = (prompt | llm | parser).invoke({"topic": topic})
                break
            except Exception as parse_err:
                if attempt == 2:
                    raise parse_err
                logger.warning(f"Parse attempt {attempt+1} failed, retrying...")

        logger.info("✅ Report generated")

        def clean(text):
            paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
            return "".join([f"<p>{p}</p>" for p in paragraphs])

        lang_cfg = LANGUAGES[language]
        base_html = TEMPLATES[template]["html"]

        html_content = base_html \
            .replace('lang="ar"', f'lang="{lang_cfg["html_lang"]}"') \
            .replace('dir="rtl"', f'dir="{lang_cfg["html_dir"]}"') \
            .replace('text-align: right;', f'text-align: {lang_cfg["html_align"]};') \
            .replace("'Traditional Arabic', 'Arial', sans-serif", lang_cfg["font"])

        html = Template(html_content).render(
            title=report.title,
            intro=clean(report.introduction),
            sections=[{'title': s.title, 'content': clean(s.content)} for s in report.sections],
            conc=clean(report.conclusion),
        )

        logger.info("📄 Converting to PDF...")
        pdf_bytes = HTML(string=html).write_pdf()

        logger.info("✅ PDF created")
        return pdf_bytes, report.title, report

    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        return None, str(e), None


def build_docx(report, language="ar", template="classic"):
    import re

    doc = Document()
    lang_cfg = LANGUAGES[language]
    is_rtl = lang_cfg["html_dir"] == "rtl"
    ts = TEMPLATE_DOCX_STYLES.get(template, TEMPLATE_DOCX_STYLES["classic"])

    for sec in doc.sections:
        sec.top_margin = Inches(1)
        sec.bottom_margin = Inches(1)
        sec.left_margin = Inches(1.2)
        sec.right_margin = Inches(1.2)

    def set_rtl(paragraph):
        pPr = paragraph._p.get_or_add_pPr()
        bidi = OxmlElement('w:bidi')
        pPr.insert(0, bidi)
        jc = OxmlElement('w:jc')
        jc.set(qn('w:val'), 'right')
        pPr.append(jc)

    def add_shading(paragraph, hex_color):
        pPr = paragraph._p.get_or_add_pPr()
        shd = OxmlElement('w:shd')
        shd.set(qn('w:fill'), hex_color)
        shd.set(qn('w:val'), 'clear')
        pPr.append(shd)

    def add_border_right(paragraph, hex_color):
        pPr = paragraph._p.get_or_add_pPr()
        pBdr = OxmlElement('w:pBdr')
        right = OxmlElement('w:right')
        right.set(qn('w:val'), 'single')
        right.set(qn('w:sz'), '24')
        right.set(qn('w:space'), '4')
        right.set(qn('w:color'), hex_color)
        pBdr.append(right)
        pPr.append(pBdr)

    def add_bottom_border(paragraph, hex_color):
        pPr = paragraph._p.get_or_add_pPr()
        pBdr = OxmlElement('w:pBdr')
        bottom = OxmlElement('w:bottom')
        bottom.set(qn('w:val'), 'single')
        bottom.set(qn('w:sz'), '12')
        bottom.set(qn('w:space'), '1')
        bottom.set(qn('w:color'), hex_color)
        pBdr.append(bottom)
        pPr.append(pBdr)

    def add_heading(text):
        h = doc.add_paragraph()
        if is_rtl:
            set_rtl(h)
        if ts["heading_bg"]:
            add_shading(h, ts["heading_bg"])
        if template in ("classic", "professional"):
            border_hex = "3498DB" if template == "classic" else "2C5282"
            add_border_right(h, border_hex)
        r = h.add_run(text)
        r.bold = True
        r.font.size = Pt(14)
        r.font.color.rgb = ts["heading_color"]

    def split_into_sentences(text):
        text = text.strip()
        if not text:
            return []
        # تقسيم النص إلى جمل بناءً على نقاط النهاية
        sentences = re.split(r'(?<=[.!?؟])\s+', text)
        # دمج الجمل في فقرات (كل 3-4 جمل فقرة)
        chunks = []
        current = []
        for s in sentences:
            s = s.strip()
            if s:
                current.append(s)
                if len(current) >= 3:
                    chunks.append(' '.join(current))
                    current = []
        if current:
            chunks.append(' '.join(current))
        return chunks if chunks else [text]

    def add_body(text):
        paragraphs = split_into_sentences(text)
        for para_text in paragraphs:
            if para_text.strip():
                p = doc.add_paragraph()
                p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
                if is_rtl:
                    set_rtl(p)
                run = p.add_run(para_text.strip())
                run.font.size = Pt(ts["body_size"])
                run.font.color.rgb = RGBColor(0x33, 0x33, 0x33)

    # العنوان الرئيسي
    title_para = doc.add_paragraph()
    title_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if is_rtl:
        set_rtl(title_para)
    if template in ("classic", "professional"):
        border_hex = "34495E" if template == "classic" else "2C5282"
        add_bottom_border(title_para, border_hex)
    r = title_para.add_run(report.title)
    r.bold = True
    r.font.size = Pt(22)
    r.font.color.rgb = ts["title_color"]

    doc.add_paragraph()

    add_heading(f"📚 {lang_cfg['intro_label']}")
    add_body(report.introduction)
    doc.add_paragraph()

    for i, sec in enumerate(report.sections, 1):
        add_heading(f"{i}. {sec.title}")
        add_body(sec.content)
        doc.add_paragraph()

    add_heading(f"🎯 {lang_cfg['conclusion_label']}")
    add_body(report.conclusion)

    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    return buf.read()


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    welcome = f"""
🎓 <b>مرحباً {user_name}!</b>

أهلاً بك في <b>بوت التقارير الأكاديمية الاحترافي</b> 📚

✨ <b>المميزات:</b>
- 5 أنماط كتابة مختلفة
- 4 قوالب تصميم احترافية
- تقارير بالعربية أو الإنجليزية
- إرسال بصيغة PDF أو Word

📝 <b>كيف تبدأ؟</b>
فقط أرسل لي موضوع التقرير وسأقوم بإنشاء تقرير احترافي

💡 <b>أمثلة للمواضيع:</b>
- الذكاء الاصطناعي وتطبيقاته
- التغير المناخي والحلول المستدامة
- الطاقة المتجددة في المستقبل
- الأمن السيبراني في العصر الرقمي

⏱️ <b>وقت الإنشاء: 30-60 ثانية</b>

🚀 <b>ابدأ الآن بإرسال موضوع تقريرك!</b>
"""
    await update.message.reply_text(welcome, parse_mode='HTML')
    logger.info(f"✅ User {user_id} ({user_name}) started the bot")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    user_id = update.effective_user.id

    if len(topic) < 5:
        await update.message.reply_text("❌ الموضوع قصير جداً! أرسل موضوعاً أطول من 5 أحرف.")
        return
    if len(topic) > 150:
        await update.message.reply_text("❌ الموضوع طويل جداً! حاول اختصاره لأقل من 150 حرف.")
        return

    user_sessions[user_id] = {"topic": topic}
    keyboard = [[InlineKeyboardButton(v["name"], callback_data=f"lang_{k}")] for k, v in LANGUAGES.items()]
    safe_topic = topic.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')

    await update.message.reply_text(
        f"📝 <b>تم استلام الموضوع:</b>\n<i>{safe_topic}</i>\n\n🌐 <b>اختر لغة التقرير:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    language = query.data.replace("lang_", "")

    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية. أرسل موضوعاً جديداً.")
        return

    user_sessions[user_id]["language"] = language
    keyboard = [[InlineKeyboardButton(v["name"], callback_data=f"style_{k}")] for k, v in WRITING_STYLES.items()]

    await query.edit_message_text(
        f"✅ <b>تم اختيار اللغة:</b> {LANGUAGES[language]['name']}\n\n🎨 <b>اختر نمط الكتابة المناسب:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    style = query.data.replace("style_", "")

    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية. أرسل موضوعاً جديداً.")
        return

    user_sessions[user_id]["style"] = style
    keyboard = [[InlineKeyboardButton(v["name"], callback_data=f"template_{k}")] for k, v in TEMPLATES.items()]

    await query.edit_message_text(
        f"✅ <b>تم اختيار:</b> {WRITING_STYLES[style]['name']}\n\n🎨 <b>الآن اختر تصميم التقرير:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    template = query.data.replace("template_", "")

    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية. أرسل موضوعاً جديداً.")
        return

    user_sessions[user_id]["template"] = template
    keyboard = [
        [InlineKeyboardButton("📄 PDF", callback_data="format_pdf")],
        [InlineKeyboardButton("📝 Word (DOCX)", callback_data="format_docx")],
    ]

    await query.edit_message_text(
        f"✅ <b>تم اختيار:</b> {TEMPLATES[template]['name']}\n\n📁 <b>اختر صيغة الملف:</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode='HTML'
    )


async def format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    file_format = query.data.replace("format_", "")

    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية. أرسل موضوعاً جديداً.")
        return

    session = user_sessions[user_id]
    topic = session["topic"]
    style = session["style"]
    template = session["template"]
    language = session.get("language", "ar")

    template_name = TEMPLATES[template]["name"]
    style_name = WRITING_STYLES[style]["name"]
    lang_name = LANGUAGES[language]["name"]
    format_name = "PDF" if file_format == "pdf" else "Word (DOCX)"

    safe_topic = topic.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')

    await query.edit_message_text(
        f"⏳ <b>جاري إنشاء التقرير...</b>\n\n📝 الموضوع: <i>{safe_topic}</i>\n🌐 اللغة: {lang_name}\n✍️ النمط: {style_name}\n🎨 القالب: {template_name}\n📁 الصيغة: {format_name}\n\n⏱️ يستغرق 30-60 ثانية...",
        parse_mode='HTML'
    )

    try:
        pdf_bytes, title, report_obj = generate_report(topic, style, template, language)

        if pdf_bytes:
            safe_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in title[:30])
            safe_title = title.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')

            caption = f"""
✅ <b>تم إنشاء التقرير بنجاح!</b>

📄 <b>العنوان:</b> {safe_title}
🌐 <b>اللغة:</b> {lang_name}
✍️ <b>النمط:</b> {style_name}
🎨 <b>القالب:</b> {template_name}
📁 <b>الصيغة:</b> {format_name}

🔄 <b>لإنشاء تقرير جديد، أرسل موضوعاً آخر!</b>
"""

            if file_format == "pdf":
                filename = f"{safe_name}.pdf"
                file_bytes = pdf_bytes
            else:
                file_bytes = build_docx(report_obj, language, template)
                filename = f"{safe_name}.docx"

            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=BytesIO(file_bytes),
                filename=filename,
                caption=caption,
                parse_mode='HTML'
            )

            await query.message.delete()
            logger.info(f"✅ {format_name} sent to user {user_id}")
            del user_sessions[user_id]
        else:
            error_msg = str(title).replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
            await query.edit_message_text(
                f"❌ <b>حدث خطأ</b>\n\n{error_msg[:300]}\n\n🔄 حاول مرة أخرى",
                parse_mode='HTML'
            )

    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        error_text = str(e)[:200].replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
        await query.edit_message_text(
            f"❌ <b>خطأ غير متوقع</b>\n\n<code>{error_text}</code>\n\n🔄 حاول مرة أخرى",
            parse_mode='HTML'
        )


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"❌ Update error: {context.error}", exc_info=context.error)
    try:
        if update and update.effective_message:
            await update.effective_message.reply_text("❌ حدث خطأ في معالجة طلبك. حاول مرة أخرى.")
    except:
        pass


if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask server started")

    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        logger.error("❌ TELEGRAM_TOKEN missing")
        exit(1)

    try:
        application = ApplicationBuilder().token(token).build()
        application.add_handler(CommandHandler('start', start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(CallbackQueryHandler(language_callback, pattern='^lang_'))
        application.add_handler(CallbackQueryHandler(style_callback, pattern='^style_'))
        application.add_handler(CallbackQueryHandler(template_callback, pattern='^template_'))
        application.add_handler(CallbackQueryHandler(format_callback, pattern='^format_'))
        application.add_error_handler(error_handler)

        logger.info("🤖 Bot Production Ready!")
        print("=" * 60)
        print("✅ Academic Reports Bot - Production Version 2.0")
        print("=" * 60)

        application.run_polling(allowed_updates=Update.ALL_TYPES)

    except Exception as e:
        logger.error(f"❌ Startup failed: {e}", exc_info=True)
        exit(1)

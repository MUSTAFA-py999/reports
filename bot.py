import os
import threading
import logging
import asyncio
from queue import Queue
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
from weasyprint import HTML, CSS
from docx import Document
from docx.shared import Pt, RGBColor, Inches
from docx.enum.text import WD_ALIGN_PARAGRAPH

# ==========================================
# إعداد Logging
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================
# Flask Server
# ==========================================
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "✅ Academic Reports Bot - Production Ready v3.0"

@flask_app.route('/health')
def health():
    return {"status": "healthy", "bot": "active", "version": "3.0"}, 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ==========================================
# Queue System
# ==========================================
request_queue = Queue(maxsize=50)

async def process_queue(context):
    """معالجة الطلبات من الطابور"""
    while True:
        try:
            if not request_queue.empty():
                task = request_queue.get()
                
                user_id = task['user_id']
                chat_id = task['chat_id']
                session = task['session']
                
                logger.info(f"🔄 Processing request for user {user_id}")
                
                await generate_and_send_report(
                    context=context,
                    chat_id=chat_id,
                    session=session,
                    user_id=user_id
                )
                
                request_queue.task_done()
                await asyncio.sleep(2)
            else:
                await asyncio.sleep(1)
        except Exception as e:
            logger.error(f"❌ Queue error: {e}", exc_info=True)
            await asyncio.sleep(2)

# ==========================================
# User Sessions
# ==========================================
user_sessions = {}

# ==========================================
# AI Models
# ==========================================
class Section(BaseModel):
    title: str = Field(description="عنوان القسم")
    content: str = Field(description="المحتوى")

class AcademicReport(BaseModel):
    title: str = Field(description="عنوان التقرير")
    introduction: str = Field(description="المقدمة")
    sections: List[Section] = Field(description="الأقسام")
    conclusion: str = Field(description="الخاتمة")

# ==========================================
# Languages
# ==========================================
LANGUAGES = {
    "ar": {
        "name": "🇸🇦 العربية",
        "direction": "rtl",
        "prompt_suffix": "اكتب بلغة عربية فصحى.",
        "intro_label": "المقدمة",
        "conclusion_label": "الخاتمة"
    },
    "en": {
        "name": "🇬🇧 English",
        "direction": "ltr",
        "prompt_suffix": "Write in professional English.",
        "intro_label": "Introduction",
        "conclusion_label": "Conclusion"
    }
}

# ==========================================
# Page Lengths
# ==========================================
PAGE_LENGTHS = {
    "short": {
        "name": "📄 قصير (2-3 صفحات)",
        "intro_words": "60-90",
        "sections": 2,
        "section_words": "150-200",
        "conclusion_words": "80-100"
    },
    "medium": {
        "name": "📑 متوسط (4-6 صفحات)",
        "intro_words": "60-90",
        "sections": 4,
        "section_words": "200-300",
        "conclusion_words": "100-150"
    },
    "long": {
        "name": "📚 طويل (7-10 صفحات)",
        "intro_words": "60-90",
        "sections": 4,
        "section_words": "300-400",
        "conclusion_words": "150-200"
    },
    "very_long": {
        "name": "📖 مفصل جداً (10-15 صفحة)",
        "intro_words": "60-90",
        "sections": 6,
        "section_words": "400-500",
        "conclusion_words": "200-250"
    }
}

# ==========================================
# Output Formats
# ==========================================
OUTPUT_FORMATS = {
    "pdf": {
        "name": "📕 PDF",
        "icon": "📕"
    },
    "docx": {
        "name": "📘 Word (DOCX)",
        "icon": "📘"
    }
}

# ==========================================
# Writing Styles
# ==========================================
WRITING_STYLES = {
    "academic": {
        "name": "🎓 أكاديمي متقدم",
        "prompt": "اكتب بأسلوب أكاديمي رسمي جداً مع استخدام مصطلحات علمية ولغة فصحى متقدمة."
    },
    "simple": {
        "name": "📖 مبسط سهل",
        "prompt": "اكتب بأسلوب مبسط وسهل الفهم مناسب لطلاب المدارس."
    },
    "detailed": {
        "name": "📚 تفصيلي شامل",
        "prompt": "اكتب بأسلوب تفصيلي جداً مع شرح كل نقطة بعمق وإضافة أمثلة."
    },
    "creative": {
        "name": "✨ إبداعي ملهم",
        "prompt": "اكتب بأسلوب إبداعي جذاب مع استخدام تشبيهات واستعارات."
    },
    "formal": {
        "name": "💼 رسمي احترافي",
        "prompt": "اكتب بأسلوب رسمي احترافي مناسب للأعمال والمؤسسات."
    }
}

# ==========================================
# HTML Templates - مُحسّنة ومختلفة
# ==========================================
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
    body {
        font-family: 'Traditional Arabic', 'Arial', sans-serif;
        direction: rtl;
        line-height: 1.9;
        color: #2c3e50;
    }
    .header {
        text-align: center;
        border-bottom: 4px solid #34495e;
        padding-bottom: 20px;
        margin-bottom: 40px;
    }
    h1 {
        color: #2c3e50;
        font-size: 32px;
        margin-bottom: 10px;
    }
    h2 {
        color: #34495e;
        margin-top: 30px;
        border-right: 5px solid #3498db;
        padding-right: 15px;
        padding: 12px 15px;
        background: #ecf0f1;
        font-size: 22px;
    }
    p {
        text-align: justify;
        line-height: 1.9;
        margin-bottom: 16px;
        font-size: 15px;
    }
    .intro, .conclusion {
        background-color: #ecf0f1;
        padding: 25px;
        border-radius: 8px;
        margin: 25px 0;
        border-right: 5px solid #3498db;
    }
</style>
</head>
<body>
<div class="header">
    <h1>{{ title }}</h1>
</div>

<div class="intro">
    <h2>{{ intro_label }}</h2>
    {{ intro | safe }}
</div>

{% for section in sections %}
<div>
    <h2>{{ loop.index }}. {{ section.title }}</h2>
    {{ section.content | safe }}
</div>
{% endfor %}

<div class="conclusion">
    <h2>{{ conc_label }}</h2>
    {{ conc | safe }}
</div>
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
    body {
        font-family: 'Arial', sans-serif;
        direction: rtl;
        line-height: 1.8;
        color: #1a1a2e;
        background: white;
    }
    h1 {
        text-align: center;
        color: #667eea;
        font-size: 36px;
        margin-bottom: 30px;
        font-weight: bold;
        text-shadow: 2px 2px 4px rgba(102, 126, 234, 0.2);
    }
    h2 {
        color: white;
        margin-top: 35px;
        padding: 15px 20px;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        border-radius: 10px;
        font-size: 24px;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    }
    p {
        text-align: justify;
        line-height: 1.8;
        margin-bottom: 18px;
        font-size: 15px;
        color: #2d3748;
    }
    .intro, .conclusion {
        background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
        padding: 30px;
        border-radius: 15px;
        margin: 30px 0;
        box-shadow: 0 5px 15px rgba(0,0,0,0.1);
    }
</style>
</head>
<body>
    <h1>{{ title }}</h1>
    
    <div class="intro">
        <h2>🌟 {{ intro_label }}</h2>
        {{ intro | safe }}
    </div>

    {% for section in sections %}
    <div>
        <h2>{{ loop.index }}. {{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="conclusion">
        <h2>✨ {{ conc_label }}</h2>
        {{ conc | safe }}
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
    body {
        font-family: 'Arial', sans-serif;
        direction: rtl;
        line-height: 2;
        color: #333;
    }
    h1 {
        text-align: center;
        font-size: 32px;
        font-weight: 300;
        letter-spacing: 2px;
        margin-bottom: 40px;
        padding-bottom: 20px;
        border-bottom: 1px solid #e0e0e0;
        color: #222;
    }
    h2 {
        font-size: 20px;
        font-weight: 500;
        margin-top: 40px;
        margin-bottom: 20px;
        color: #555;
    }
    p {
        text-align: justify;
        line-height: 2;
        margin-bottom: 20px;
        font-size: 14px;
        color: #666;
    }
    .section {
        margin-bottom: 50px;
    }
</style>
</head>
<body>
    <h1>{{ title }}</h1>
    
    <div class="section">
        <h2>{{ intro_label }}</h2>
        {{ intro | safe }}
    </div>

    {% for section in sections %}
    <div class="section">
        <h2>{{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="section">
        <h2>{{ conc_label }}</h2>
        {{ conc | safe }}
    </div>
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
    body {
        font-family: 'Traditional Arabic', 'Times New Roman', serif;
        direction: rtl;
        line-height: 1.9;
        color: #1a202c;
    }
    .letterhead {
        border: 3px double #2c5282;
        padding: 30px;
        margin-bottom: 40px;
        background: linear-gradient(to bottom, #f7fafc 0%, white 100%);
    }
    h1 {
        text-align: center;
        color: #2c5282;
        font-size: 30px;
        margin: 0;
        text-transform: uppercase;
        letter-spacing: 1px;
    }
    h2 {
        color: #2c5282;
        margin-top: 35px;
        padding: 12px 20px;
        background: #edf2f7;
        border-right: 6px solid #2c5282;
        font-size: 22px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }
    p {
        text-align: justify;
        line-height: 1.9;
        margin-bottom: 18px;
        font-size: 15px;
    }
    .section {
        margin-bottom: 40px;
    }
</style>
</head>
<body>
    <div class="letterhead">
        <h1>{{ title }}</h1>
    </div>

    <div class="section">
        <h2>{{ intro_label }}</h2>
        {{ intro | safe }}
    </div>

    {% for section in sections %}
    <div class="section">
        <h2>{{ loop.index }}. {{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="section">
        <h2>{{ conc_label }}</h2>
        {{ conc | safe }}
    </div>
</body>
</html>
"""
    }
}

# ==========================================
# Generate Report
# ==========================================
def generate_report_content(topic, style, language, page_length):
    """توليد محتوى التقرير"""
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise Exception("API Key غير موجود")
        
        logger.info(f"📝 Generating: {topic}")
        
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0.5,
            google_api_key=api_key,
            max_retries=3
        )
        
        parser = PydanticOutputParser(pydantic_object=AcademicReport)
        
        style_instruction = WRITING_STYLES[style]["prompt"]
        lang_config = LANGUAGES[language]
        page_config = PAGE_LENGTHS[page_length]
        
        prompt = PromptTemplate(
            input_variables=["topic"],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            template=f"""أنت كاتب أكاديمي محترف. اكتب تقريرًا مفصلاً:

الموضوع: {{topic}}

الأسلوب: {style_instruction}

المتطلبات:
- مقدمة: {page_config['intro_words']} كلمة
- {page_config['sections']} أقسام رئيسية
- كل قسم: {page_config['section_words']} كلمة
- خاتمة: {page_config['conclusion_words']} كلمة

{lang_config['prompt_suffix']}

{{format_instructions}}"""
        )
        
        report = (prompt | llm | parser).invoke({"topic": topic})
        logger.info("✅ Content generated")
        
        return report, None
        
    except Exception as e:
        logger.error(f"❌ Generation error: {e}", exc_info=True)
        return None, str(e)

def create_pdf(report, template, language):
    """إنشاء PDF"""
    try:
        lang_config = LANGUAGES[language]
        
        def clean(text):
            paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
            return "".join([f"<p>{p}</p>" for p in paragraphs])
        
        sections_html = ""
        for idx, section in enumerate(report.sections, 1):
            sections_html += f"""
<div>
    <h2>{idx}. {section.title}</h2>
    {clean(section.content)}
</div>
"""
        
        html_content = TEMPLATES[template]["html"]
        html = Template(html_content).render(
            title=report.title,
            intro_label=lang_config['intro_label'],
            intro=clean(report.introduction),
            sections=Markup(sections_html),
            conc_label=lang_config['conclusion_label'],
            conc=clean(report.conclusion)
        )
        
        logger.info("📄 Converting to PDF...")
        pdf_bytes = HTML(string=html).write_pdf()
        
        logger.info("✅ PDF created")
        return pdf_bytes
        
    except Exception as e:
        logger.error(f"❌ PDF error: {e}", exc_info=True)
        return None

# Import Markup for Jinja2
from markupsafe import Markup

def create_docx(report, language):
    """إنشاء DOCX"""
    try:
        lang_config = LANGUAGES[language]
        doc = Document()
        
        section = doc.sections[0]
        section.page_height = Inches(11.69)
        section.page_width = Inches(8.27)
        
        # العنوان
        title = doc.add_heading(report.title, 0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title.runs[0].font.size = Pt(24)
        title.runs[0].font.color.rgb = RGBColor(44, 62, 80)
        
        doc.add_paragraph()
        
        # المقدمة
        intro_heading = doc.add_heading(lang_config['intro_label'], 1)
        intro_heading.runs[0].font.color.rgb = RGBColor(52, 152, 219)
        
        for para in report.introduction.split('\n'):
            if para.strip():
                p = doc.add_paragraph(para.strip())
                p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        
        # الأقسام
        for idx, section in enumerate(report.sections, 1):
            doc.add_paragraph()
            section_heading = doc.add_heading(f"{idx}. {section.title}", 1)
            section_heading.runs[0].font.color.rgb = RGBColor(52, 152, 219)
            
            for para in section.content.split('\n'):
                if para.strip():
                    p = doc.add_paragraph(para.strip())
                    p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        
        # الخاتمة
        doc.add_paragraph()
        conc_heading = doc.add_heading(lang_config['conclusion_label'], 1)
        conc_heading.runs[0].font.color.rgb = RGBColor(52, 152, 219)
        
        for para in report.conclusion.split('\n'):
            if para.strip():
                p = doc.add_paragraph(para.strip())
                p.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        
        docx_buffer = BytesIO()
        doc.save(docx_buffer)
        docx_buffer.seek(0)
        
        logger.info("✅ DOCX created")
        return docx_buffer.getvalue()
        
    except Exception as e:
        logger.error(f"❌ DOCX error: {e}", exc_info=True)
        return None

async def generate_and_send_report(context, chat_id, session, user_id):
    """توليد وإرسال التقرير"""
    try:
        topic = session["topic"]
        style = session["style"]
        template = session["template"]
        language = session["language"]
        page_length = session["page_length"]
        output_format = session["format"]
        
        # توليد المحتوى
        report, error = generate_report_content(topic, style, language, page_length)
        
        if not report:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"❌ <b>خطأ في التوليد</b>\n\n{error[:300]}",
                parse_mode='HTML'
            )
            return False
        
        # إنشاء الملف
        if output_format == "pdf":
            file_bytes = create_pdf(report, template, language)
            extension = "pdf"
            icon = "📕"
        else:
            file_bytes = create_docx(report, language)
            extension = "docx"
            icon = "📘"
        
        if not file_bytes:
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ <b>خطأ في إنشاء الملف</b>",
                parse_mode='HTML'
            )
            return False
        
        safe_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in report.title[:30])
        filename = f"{safe_name}.{extension}"
        
        lang_config = LANGUAGES[language]
        
        caption = f"""
✅ <b>تم بنجاح!</b>

{icon} <b>العنوان:</b> {report.title}
🌐 <b>اللغة:</b> {lang_config['name']}
📄 <b>الطول:</b> {PAGE_LENGTHS[page_length]['name']}

🔄 <b>أرسل موضوعاً جديداً للمتابعة!</b>
"""
        
        await context.bot.send_document(
            chat_id=chat_id,
            document=BytesIO(file_bytes),
            filename=filename,
            caption=caption,
            parse_mode='HTML'
        )
        
        logger.info(f"✅ Sent to user {user_id}")
        return True
        
    except Exception as e:
        logger.error(f"❌ Send error: {e}", exc_info=True)
        return False

# ==========================================
# Telegram Handlers
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.effective_user.first_name
    
    welcome = f"""
🎓 <b>مرحباً {user_name}!</b>

أهلاً بك في <b>بوت التقارير الأكاديمية الاحترافي</b> 📚

✨ <b>المميزات:</b>
- 5 أنماط كتابة مختلفة
- 4 قوالب تصميم احترافية
- لغتين (عربي وإنجليزي)
- 4 أطوال للتقرير (2-15 صفحة)
- تصدير PDF أو Word

📝 <b>ابدأ الآن:</b>
أرسل موضوع التقرير

💡 <b>أمثلة:</b>
- الذكاء الاصطناعي
- Renewable Energy
- الأمن السيبراني

⏱️ <b>الوقت: 30-60 ثانية</b>
"""
    
    await update.message.reply_text(welcome, parse_mode='HTML')

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = update.message.text.strip()
    user_id = update.effective_user.id
    
    if len(topic) < 5:
        await update.message.reply_text("❌ الموضوع قصير جداً!")
        return
    
    if len(topic) > 150:
        await update.message.reply_text("❌ الموضوع طويل جداً!")
        return
    
    user_sessions[user_id] = {"topic": topic}
    
    keyboard = []
    for key, value in LANGUAGES.items():
        keyboard.append([InlineKeyboardButton(value["name"], callback_data=f"lang_{key}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        f"📝 <b>الموضوع:</b> <i>{topic}</i>\n\n🌐 <b>اختر اللغة:</b>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def language_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    language = query.data.replace("lang_", "")
    
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    
    user_sessions[user_id]["language"] = language
    
    keyboard = []
    for key, value in PAGE_LENGTHS.items():
        keyboard.append([InlineKeyboardButton(value["name"], callback_data=f"length_{key}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ <b>اللغة:</b> {LANGUAGES[language]['name']}\n\n📏 <b>اختر الطول:</b>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def length_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    page_length = query.data.replace("length_", "")
    
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    
    user_sessions[user_id]["page_length"] = page_length
    
    keyboard = []
    for key, value in WRITING_STYLES.items():
        keyboard.append([InlineKeyboardButton(value["name"], callback_data=f"style_{key}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ <b>الطول:</b> {PAGE_LENGTHS[page_length]['name']}\n\n✍️ <b>اختر النمط:</b>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    style = query.data.replace("style_", "")
    
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    
    user_sessions[user_id]["style"] = style
    
    keyboard = []
    for key, value in TEMPLATES.items():
        keyboard.append([InlineKeyboardButton(value["name"], callback_data=f"template_{key}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ <b>النمط:</b> {WRITING_STYLES[style]['name']}\n\n🎨 <b>اختر القالب:</b>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def template_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    template = query.data.replace("template_", "")
    
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    
    user_sessions[user_id]["template"] = template
    
    keyboard = []
    for key, value in OUTPUT_FORMATS.items():
        keyboard.append([InlineKeyboardButton(value["name"], callback_data=f"format_{key}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await query.edit_message_text(
        f"✅ <b>القالب:</b> {TEMPLATES[template]['name']}\n\n📎 <b>اختر الصيغة:</b>",
        reply_markup=reply_markup,
        parse_mode='HTML'
    )

async def format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    output_format = query.data.replace("format_", "")
    
    if user_id not in user_sessions:
        await query.edit_message_text("❌ الجلسة منتهية.")
        return
    
    session = user_sessions[user_id]
    session["format"] = output_format
    
    queue_size = request_queue.qsize()
    queue_msg = f"\n\n⏳ <b>{queue_size} طلب في الانتظار</b>" if queue_size > 0 else ""
    
    await query.edit_message_text(
        f"✅ <b>تم!</b>\n\n🔄 <b>جاري الإضافة للطابور...</b>{queue_msg}",
        parse_mode='HTML'
    )
    
    try:
        request_queue.put({
            "user_id": user_id,
            "chat_id": query.message.chat_id,
            "session": session.copy()
        }, block=False)
        
        logger.info(f"📥 Queued for user {user_id}")
        
        await query.message.reply_text(
            "✅ <b>تم!</b>\n\n⏱️ سيصلك التقرير خلال دقائق...",
            parse_mode='HTML'
        )
        
        del user_sessions[user_id]
        
    except:
        await query.message.reply_text(
            "❌ <b>الطابور ممتلئ!</b>\n\nحاول بعد قليل.",
            parse_mode='HTML'
        )

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"❌ Error: {context.error}", exc_info=context.error)

# ==========================================
# Main
# ==========================================
if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🌐 Flask started")
    
    token = os.getenv("TELEGRAM_TOKEN")
    
    if not token:
        logger.error("❌ TOKEN missing")
        exit(1)
    
    try:
        application = ApplicationBuilder().token(token).build()
        
        application.add_handler(CommandHandler('start', start))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
        application.add_handler(CallbackQueryHandler(language_callback, pattern='^lang_'))
        application.add_handler(CallbackQueryHandler(length_callback, pattern='^length_'))
        application.add_handler(CallbackQueryHandler(style_callback, pattern='^style_'))
        application.add_handler(CallbackQueryHandler(template_callback, pattern='^template_'))
        application.add_handler(CallbackQueryHandler(format_callback, pattern='^format_'))
        application.add_error_handler(error_handler)
        
        logger.info("🤖 Bot v3.0 Ready!")
        print("=" * 60)
        print("✅ Bot v3.0 Production")
        print("=" * 60)
        
        async def run_bot():
            async with application:
                await application.initialize()
                await application.start()
                
                asyncio.create_task(process_queue(application))
                
                await application.updater.start_polling(allowed_updates=Update.ALL_TYPES)
                await asyncio.Event().wait()
        
        asyncio.run(run_bot())
        
    except Exception as e:
        logger.error(f"❌ Failed: {e}", exc_info=True)
        exit(1)

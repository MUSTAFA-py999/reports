```python
import os
import threading
import logging
import asyncio
import datetime
from queue import Queue
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, CallbackQueryHandler, filters
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.prompts import PromptTemplate
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, Field
from jinja2 import Template, Markup
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
    return "✅ Academic Reports Bot - Production Ready v3.1 (Fixed Templates & PDF)"

@flask_app.route('/health')
def health():
    return {"status": "healthy", "bot": "active", "version": "3.1"}, 200

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
# HTML Templates - مُحسّنة ومختلفة (مع استخدام متغيرات موحدة)
# ==========================================
TEMPLATES = {
    "classic": {
        "name": "🎓 كلاسيكي أكاديمي",
        "description": "تصميم تقليدي احترافي",
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
        text-align: right;
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
    .subtitle {
        color: #7f8c8d;
        font-size: 14px;
        margin-top: 10px;
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
    .footer {
        text-align: center;
        margin-top: 60px;
        padding-top: 25px;
        border-top: 3px solid #bdc3c7;
        color: #7f8c8d;
        font-size: 12px;
    }
</style>
</head>
<body>
<div class="header">
    <h1>{{ title }}</h1>
    <div class="subtitle">{{ date }} | تقرير أكاديمي</div>
</div>

<div class="intro">
    <h2>📚 المقدمة</h2>
    {{ introduction | safe }}
</div>

{% for section in sections %}
<div>
    <h2>{{ loop.index }}. {{ section.title }}</h2>
    {{ section.content | safe }}
</div>
{% endfor %}

<div class="conclusion">
    <h2>🎯 الخاتمة</h2>
    {{ conclusion | safe }}
</div>

<div class="footer">
    <p>تم الإنشاء بواسطة Academic Reports Bot</p>
    <p>{{ date }}</p>
</div>
</body>
</html>
"""
    },
    
    "modern": {
        "name": "🚀 عصري حديث",
        "description": "تصميم عصري بألوان جذابة",
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
        text-align: right;
        line-height: 1.8;
        color: #1a1a2e;
    }
    .container {
        background: white;
        padding: 40px;
    }
    h1 {
        text-align: center;
        color: #667eea;
        font-size: 36px;
        margin-bottom: 15px;
        font-weight: bold;
    }
    .date-badge {
        text-align: center;
        background: #667eea;
        color: white;
        padding: 8px 20px;
        border-radius: 20px;
        display: inline-block;
        font-size: 13px;
        margin-bottom: 30px;
    }
    h2 {
        color: #667eea;
        margin-top: 35px;
        padding: 15px 20px;
        background: linear-gradient(90deg, #f8f9fa 0%, white 100%);
        border-right: 6px solid #764ba2;
        border-radius: 0 10px 10px 0;
        font-size: 24px;
    }
    p {
        text-align: justify;
        line-height: 1.8;
        margin-bottom: 18px;
        font-size: 15px;
        color: #2d3748;
    }
    .intro, .conclusion {
        background: #f5f7fa;
        padding: 30px;
        border-radius: 15px;
        margin: 30px 0;
    }
    .footer {
        text-align: center;
        margin-top: 50px;
        padding: 20px;
        background: #f8f9fa;
        border-radius: 10px;
        color: #718096;
    }
</style>
</head>
<body>
<div class="container">
    <h1>{{ title }}</h1>
    <div style="text-align: center;">
        <span class="date-badge">📅 {{ date }}</span>
    </div>

    <div class="intro">
        <h2>🌟 المقدمة</h2>
        {{ introduction | safe }}
    </div>

    {% for section in sections %}
    <div>
        <h2>{{ loop.index }}. {{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="conclusion">
        <h2>✨ الخاتمة</h2>
        {{ conclusion | safe }}
    </div>

    <div class="footer">
        <p><strong>Academic Reports Bot</strong></p>
        <p>{{ date }}</p>
    </div>
</div>
</body>
</html>
"""
    },
    
    "minimal": {
        "name": "⚪ بسيط أنيق",
        "description": "تصميم نظيف ومرتب",
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
        text-align: right;
        line-height: 2;
        color: #333;
        max-width: 800px;
        margin: 0 auto;
    }
    h1 {
        text-align: center;
        font-size: 32px;
        font-weight: 300;
        letter-spacing: 2px;
        margin-bottom: 40px;
        padding-bottom: 20px;
        border-bottom: 1px solid #e0e0e0;
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
    .footer {
        text-align: center;
        margin-top: 80px;
        padding-top: 30px;
        border-top: 1px solid #e0e0e0;
        font-size: 11px;
        color: #999;
    }
</style>
</head>
<body>
    <h1>{{ title }}</h1>
    
    <div class="section">
        <h2>المقدمة</h2>
        {{ introduction | safe }}
    </div>

    {% for section in sections %}
    <div class="section">
        <h2>{{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="section">
        <h2>الخاتمة</h2>
        {{ conclusion | safe }}
    </div>

    <div class="footer">
        <p>{{ date }}</p>
    </div>
</body>
</html>
"""
    },
    
    "colorful": {
        "name": "🎨 ملون إبداعي",
        "description": "تصميم ملون ومميز",
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
        text-align: right;
        line-height: 1.8;
        color: #2d3748;
    }
    .header {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 50%, #f093fb 100%);
        padding: 40px;
        text-align: center;
        border-radius: 15px;
        margin-bottom: 40px;
    }
    h1 {
        color: white;
        font-size: 34px;
        margin: 0;
        text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
    }
    .date {
        color: white;
        margin-top: 15px;
        font-size: 14px;
    }
    h2 {
        font-size: 24px;
        margin-top: 35px;
        padding: 15px 20px;
        border-radius: 10px;
        color: white;
        font-weight: bold;
    }
    h2:nth-of-type(1) { background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%); }
    h2:nth-of-type(2) { background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%); }
    h2:nth-of-type(3) { background: linear-gradient(135deg, #43e97b 0%, #38f9d7 100%); }
    h2:nth-of-type(4) { background: linear-gradient(135deg, #fa709a 0%, #fee140 100%); }
    h2:nth-of-type(5) { background: linear-gradient(135deg, #30cfd0 0%, #330867 100%); }
    h2:nth-of-type(6) { background: linear-gradient(135deg, #a8edea 0%, #fed6e3 100%); }
    p {
        text-align: justify;
        line-height: 1.8;
        margin-bottom: 18px;
        font-size: 15px;
    }
    .section {
        background: #f8f9fa;
        padding: 25px;
        border-radius: 12px;
        margin: 25px 0;
    }
    .footer {
        text-align: center;
        margin-top: 50px;
        padding: 25px;
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        border-radius: 10px;
    }
</style>
</head>
<body>
    <div class="header">
        <h1>{{ title }}</h1>
        <div class="date">📅 {{ date }}</div>
    </div>

    <div class="section">
        <h2>📚 المقدمة</h2>
        {{ introduction | safe }}
    </div>

    {% for section in sections %}
    <div class="section">
        <h2>{{ loop.index }}. {{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="section">
        <h2>🎯 الخاتمة</h2>
        {{ conclusion | safe }}
    </div>

    <div class="footer">
        <p><strong>Academic Reports Bot</strong></p>
        <p>{{ date }}</p>
    </div>
</body>
</html>
"""
    },
    
    "professional": {
        "name": "💼 احترافي رسمي",
        "description": "تصميم رسمي للأعمال",
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
        text-align: right;
        line-height: 1.9;
        color: #1a202c;
    }
    .letterhead {
        border: 3px solid #2c5282;
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
    .doc-info {
        text-align: center;
        margin-top: 20px;
        padding: 15px;
        background: #edf2f7;
        border-radius: 5px;
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
    .footer {
        text-align: center;
        margin-top: 50px;
        padding: 20px;
        border-top: 3px solid #2c5282;
        color: #4a5568;
        font-size: 12px;
    }
</style>
</head>
<body>
    <div class="letterhead">
        <h1>{{ title }}</h1>
        <div class="doc-info">
            <strong>تاريخ الإصدار:</strong> {{ date }}<br>
            <strong>نوع الوثيقة:</strong> تقرير أكاديمي
        </div>
    </div>

    <div class="section">
        <h2>المقدمة</h2>
        {{ introduction | safe }}
    </div>

    {% for section in sections %}
    <div class="section">
        <h2>{{ loop.index }}. {{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="section">
        <h2>الخاتمة</h2>
        {{ conclusion | safe }}
    </div>

    <div class="footer">
        <p><strong>Academic Reports Bot</strong></p>
        <p>هذه وثيقة رسمية تم إنشاؤها إلكترونياً</p>
        <p>{{ date }}</p>
    </div>
</body>
</html>
"""
    }
}

# ==========================================
# دالة مساعدة لتنظيف النص وتحويله لفقرات HTML
# ==========================================
def clean_html_paragraphs(text):
    paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
    return "".join([f"<p>{p}</p>" for p in paragraphs])

# ==========================================
# Generate Report Content
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

# ==========================================
# Create PDF
# ==========================================
def create_pdf(report, template_name, language):
    """إنشاء PDF من التقرير"""
    try:
        # تنظيف النصوص وتحويلها إلى HTML
        intro_html = Markup(clean_html_paragraphs(report.introduction))
        conclusion_html = Markup(clean_html_paragraphs(report.conclusion))
        
        sections_html = ""
        for idx, section in enumerate(report.sections, 1):
            section_content = clean_html_paragraphs(section.content)
            sections_html += f"""
<div class="section">
    <h2>{idx}. {section.title}</h2>
    {section_content}
</div>
"""
        sections_html = Markup(sections_html)
        
        # تحديد التاريخ
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        
        # تحميل القالب
        html_template = Template(TEMPLATES[template_name]["html"])
        
        html_content = html_template.render(
            title=report.title,
            introduction=intro_html,
            sections=sections_html,
            conclusion=conclusion_html,
            date=today
        )
        
        # إنشاء PDF
        pdf = HTML(string=html_content).write_pdf()
        return pdf, None
        
    except Exception as e:
        logger.error(f"❌ PDF creation error: {e}", exc_info=True)
        return None, str(e)

# ==========================================
# Create DOCX
# ==========================================
def create_docx(report, language):
    """إنشاء ملف Word (DOCX) من التقرير"""
    try:
        doc = Document()
        
        # إعداد الاتجاه (للعربية من اليمين لليسار)
        if language == "ar":
            # تعيين اللغة العربية
            doc.styles['Normal'].paragraph_format.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        
        # العنوان
        title = doc.add_heading(report.title, 0)
        if language == "ar":
            title.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        
        # التاريخ
        date_paragraph = doc.add_paragraph(f"التاريخ: {datetime.datetime.now().strftime('%Y-%m-%d')}")
        if language == "ar":
            date_paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        
        doc.add_paragraph()  # فراغ
        
        # المقدمة
        doc.add_heading(LANGUAGES[language]['intro_label'], level=1)
        intro_para = doc.add_paragraph(report.introduction)
        if language == "ar":
            intro_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        
        # الأقسام
        for section in report.sections:
            doc.add_heading(section.title, level=2)
            section_para = doc.add_paragraph(section.content)
            if language == "ar":
                section_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        
        # الخاتمة
        doc.add_heading(LANGUAGES[language]['conclusion_label'], level=1)
        conc_para = doc.add_paragraph(report.conclusion)
        if language == "ar":
            conc_para.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        
        # حفظ في الذاكرة
        file_stream = BytesIO()
        doc.save(file_stream)
        file_stream.seek(0)
        return file_stream, None
        
    except Exception as e:
        logger.error(f"❌ DOCX creation error: {e}", exc_info=True)
        return None, str(e)

# ==========================================
# توليد وإرسال التقرير (دالة المساعد)
# ==========================================
async def generate_and_send_report(context, chat_id, session, user_id):
    """توليد التقرير حسب الجلسة وإرساله"""
    try:
        # إرسال رسالة انتظار
        await context.bot.send_message(chat_id=chat_id, text="⏳ جاري إنشاء تقريرك الشامل، قد يستغرق ذلك دقيقة...")
        
        # استخراج الإعدادات من الجلسة
        topic = session['topic']
        style = session['style']
        language = session['language']
        page_length = session['page_length']
        output_format = session['output_format']
        template = session.get('template', 'classic')  # قالب افتراضي
        
        # توليد المحتوى
        report, error = generate_report_content(topic, style, language, page_length)
        if error:
            await context.bot.send_message(chat_id=chat_id, text=f"❌ فشل في توليد المحتوى: {error}")
            return
        
        # إنشاء الملف حسب الصيغة المطلوبة
        if output_format == "pdf":
            pdf_bytes, error = create_pdf(report, template, language)
            if error:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ فشل في إنشاء PDF: {error}")
                return
            file = BytesIO(pdf_bytes)
            file.name = f"report_{user_id}.pdf"
            await context.bot.send_document(chat_id=chat_id, document=file, caption=f"✅ تم إنشاء تقريرك بصيغة PDF\nالموضوع: {topic}")
            
        elif output_format == "docx":
            docx_stream, error = create_docx(report, language)
            if error:
                await context.bot.send_message(chat_id=chat_id, text=f"❌ فشل في إنشاء DOCX: {error}")
                return
            docx_stream.name = f"report_{user_id}.docx"
            await context.bot.send_document(chat_id=chat_id, document=docx_stream, caption=f"✅ تم إنشاء تقريرك بصيغة Word\nالموضوع: {topic}")
        
        # تنظيف الجلسة بعد الإرسال
        if user_id in user_sessions:
            del user_sessions[user_id]
            
    except Exception as e:
        logger.error(f"❌ Error in generate_and_send_report: {e}", exc_info=True)
        await context.bot.send_message(chat_id=chat_id, text="❌ حدث خطأ غير متوقع أثناء إنشاء التقرير.")

# ==========================================
# أوامر وواجهات البوت
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_sessions[user_id] = {}
    
    keyboard = [
        [InlineKeyboardButton("📝 كتابة تقرير جديد", callback_data="new_report")],
        [InlineKeyboardButton("🌐 تغيير اللغة", callback_data="change_language")],
        [InlineKeyboardButton("ℹ️ مساعدة", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "👋 مرحباً بك في بوت التقارير الأكاديمية!\n"
        "يمكنك إنشاء تقارير احترافية بسهولة.\n"
        "اختر من القائمة:",
        reply_markup=reply_markup
    )

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = update.effective_user.id
    data = query.data
    
    if data == "new_report":
        user_sessions[user_id] = {}
        await query.edit_message_text("📝 أرسل الموضوع الذي تريد كتابة التقرير عنه:")
        context.user_data['awaiting_topic'] = True
        
    elif data == "change_language":
        # عرض خيارات اللغة
        keyboard = []
        for lang_code, lang_info in LANGUAGES.items():
            keyboard.append([InlineKeyboardButton(lang_info['name'], callback_data=f"set_lang_{lang_code}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("🌐 اختر اللغة:", reply_markup=reply_markup)
        
    elif data.startswith("set_lang_"):
        lang_code = data.replace("set_lang_", "")
        if user_id not in user_sessions:
            user_sessions[user_id] = {}
        user_sessions[user_id]['language'] = lang_code
        await query.edit_message_text(f"✅ تم تعيين اللغة: {LANGUAGES[lang_code]['name']}\n\nالآن أرسل الموضوع:")
        context.user_data['awaiting_topic'] = True
        
    elif data == "help":
        help_text = (
            "ℹ️ مساعدة:\n"
            "• لبدء تقرير جديد: اضغط '📝 كتابة تقرير جديد'\n"
            "• سيُطلب منك إدخال الموضوع ثم اختيار:\n"
            "   - اللغة\n"
            "   - أسلوب الكتابة\n"
            "   - طول التقرير\n"
            "   - قالب التصميم\n"
            "   - صيغة الملف (PDF أو DOCX)\n"
            "• بعد الاختيار، سيتم إنشاء التقرير وإرساله لك."
        )
        await query.edit_message_text(help_text)
        
    elif data.startswith("style_"):
        style = data.replace("style_", "")
        if user_id not in user_sessions:
            user_sessions[user_id] = {}
        user_sessions[user_id]['style'] = style
        # بعد اختيار الأسلوب ننتقل لاختيار الطول
        keyboard = []
        for length_key, length_info in PAGE_LENGTHS.items():
            keyboard.append([InlineKeyboardButton(length_info['name'], callback_data=f"length_{length_key}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("📏 اختر طول التقرير:", reply_markup=reply_markup)
        
    elif data.startswith("length_"):
        length = data.replace("length_", "")
        if user_id not in user_sessions:
            user_sessions[user_id] = {}
        user_sessions[user_id]['page_length'] = length
        # بعد الطول ننتقل لاختيار القالب
        keyboard = []
        for template_key, template_info in TEMPLATES.items():
            keyboard.append([InlineKeyboardButton(template_info['name'], callback_data=f"template_{template_key}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("🎨 اختر التصميم المناسب:", reply_markup=reply_markup)
        
    elif data.startswith("template_"):
        template = data.replace("template_", "")
        if user_id not in user_sessions:
            user_sessions[user_id] = {}
        user_sessions[user_id]['template'] = template
        # بعد القالب ننتقل لاختيار الصيغة
        keyboard = []
        for fmt_key, fmt_info in OUTPUT_FORMATS.items():
            keyboard.append([InlineKeyboardButton(fmt_info['name'], callback_data=f"format_{fmt_key}")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text("📁 اختر صيغة الملف النهائي:", reply_markup=reply_markup)
        
    elif data.startswith("format_"):
        fmt = data.replace("format_", "")
        if user_id not in user_sessions:
            user_sessions[user_id] = {}
        user_sessions[user_id]['output_format'] = fmt
        
        # التحقق من اكتمال جميع الخيارات
        session = user_sessions.get(user_id, {})
        required = ['topic', 'language', 'style', 'page_length', 'template', 'output_format']
        if all(k in session for k in required):
            # إضافة الطلب إلى الطابور
            request_queue.put({
                'user_id': user_id,
                'chat_id': update.effective_chat.id,
                'session': session.copy()
            })
            await query.edit_message_text("✅ تم استلام طلبك، سيتم إنشاء التقرير وإرساله لك خلال لحظات...")
        else:
            await query.edit_message_text("⚠️ حدث خطأ في الجلسة، الرجاء البدء من جديد بـ /start")
            if user_id in user_sessions:
                del user_sessions[user_id]

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text
    
    if context.user_data.get('awaiting_topic'):
        # استقبال الموضوع
        if user_id not in user_sessions:
            user_sessions[user_id] = {}
        user_sessions[user_id]['topic'] = text
        context.user_data['awaiting_topic'] = False
        
        # إذا لم تكن اللغة محددة بعد، نطلبها
        if 'language' not in user_sessions[user_id]:
            keyboard = []
            for lang_code, lang_info in LANGUAGES.items():
                keyboard.append([InlineKeyboardButton(lang_info['name'], callback_data=f"set_lang_{lang_code}")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("🌐 اختر اللغة:", reply_markup=reply_markup)
        else:
            # ننتقل مباشرة لاختيار الأسلوب
            keyboard = []
            for style_key, style_info in WRITING_STYLES.items():
                keyboard.append([InlineKeyboardButton(style_info['name'], callback_data=f"style_{style_key}")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text("✍️ اختر أسلوب الكتابة:", reply_markup=reply_markup)
    else:
        await update.message.reply_text("🤖 استخدم الأزرار للتفاعل مع البوت، أو أرسل /start")

# ==========================================
# تشغيل البوت
# ==========================================
def main():
    # التحقق من وجود API Key
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        logger.error("❌ GOOGLE_API_KEY غير موجودة في المتغيرات البيئية")
        return
    
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not bot_token:
        logger.error("❌ TELEGRAM_BOT_TOKEN غير موجودة في المتغيرات البيئية")
        return
    
    # تشغيل Flask في خيط منفصل
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("🚀 Flask server started on background thread")
    
    # إعداد التطبيق
    application = ApplicationBuilder().token(bot_token).build()
    
    # إضافة المعالجات
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    
    # تشغيل معالج الطابور في الخلفية
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.create_task(process_queue(application.bot))
    
    logger.info("🤖 Bot is starting...")
    application.run_polling()

if __name__ == "__main__":
    main()
```

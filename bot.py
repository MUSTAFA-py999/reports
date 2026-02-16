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
from datetime import datetime

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
    return "✅ Academic Reports Bot - Production Ready!"

@flask_app.route('/health')
def health():
    return {"status": "healthy", "bot": "active", "version": "2.0"}, 200

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

# ==========================================
# User Session Storage
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
# HTML Templates
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
    {{ intro | safe }}
</div>

{% for section in sections %}
<div>
    <h2>{{ loop.index }}. {{ section.title }}</h2>
    {{ section.content | safe }}
</div>
{% endfor %}

<div class="conclusion">
    <h2>🎯 الخاتمة</h2>
    {{ conc | safe }}
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
        {{ intro | safe }}
    </div>

    {% for section in sections %}
    <div>
        <h2>{{ loop.index }}. {{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="conclusion">
        <h2>✨ الخاتمة</h2>
        {{ conc | safe }}
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
        {{ intro | safe }}
    </div>

    {% for section in sections %}
    <div class="section">
        <h2>{{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="section">
        <h2>الخاتمة</h2>
        {{ conc | safe }}
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
        {{ intro | safe }}
    </div>

    {% for section in sections %}
    <div class="section">
        <h2>{{ loop.index }}. {{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="section">
        <h2>🎯 الخاتمة</h2>
        {{ conc | safe }}
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
        {{ intro | safe }}
    </div>

    {% for section in sections %}
    <div class="section">
        <h2>{{ loop.index }}. {{ section.title }}</h2>
        {{ section.content | safe }}
    </div>
    {% endfor %}

    <div class="section">
        <h2>الخاتمة</h2>
        {{ conc | safe }}
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
# Writing Styles
# ==========================================
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

# ==========================================
# Generate Report Function
# ==========================================
def generate_report(topic, style="academic", template="classic"):
    try:
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise Exception("API Key غير موجود")
        
        logger.info(f"📝 Generating: {topic} | Style: {style} | Template: {template}")
        
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash",
            temperature=0.5,
            google_api_key=api_key,
            max_retries=3
        )
        
        parser = PydanticOutputParser(pydantic_object=AcademicReport)
        
        style_instruction = WRITING_STYLES[style]["prompt"]
        
        prompt = PromptTemplate(
            input_variables=["topic"],
            partial_variables={"format_instructions": parser.get_format_instructions()},
            template=f"""أنت كاتب أكاديمي محترف. اكتب تقريرًا مفصلاً وشاملاً عن:

الموضوع: {{topic}}

أسلوب الكتابة: {style_instruction}

يجب أن يحتوي التقرير على:
- مقدمة شاملة (150-200 كلمة)
- 3-4 أقسام رئيسية (كل قسم 200-300 كلمة)
- خاتمة موجزة (100-150 كلمة)

{{format_instructions}}"""
        )
        
        report = (prompt | llm | parser).invoke({"topic": topic})
        logger.info("✅ Report generated")
        
        def clean(text):
            paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
            return "".join([f"<p>{p}</p>" for p in paragraphs])
        
        current_date = datetime.now().strftime("%Y/%m/%d")
        
        html = Template(TEMPLATES[template]["html"]).render(
            title=report.title,
            intro=clean(report.introduction),
            sections=[{'title': s.title, 'content': clean(s.content)} for s in report.sections],
            conc=clean(report.conclusion),
            date=current_date
        )
        
        logger.info("📄 Converting to PDF...")
        pdf_bytes = HTML(string=html).write_pdf()
        
        logger.info("✅ PDF created")
        return pdf_bytes, report.title
        
    except Exception as e:
        logger.error(f"❌ Error: {e}", exc_info=True)
        return None, str(e)

# ==========================================
# Telegram Handlers
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    user_name = update.effective_user.first_name
    
    welcome = f"""
🎓 <b>مرحباً {user_name}!</b>

أهلاً بك في <b>بوت التقارير الأكاديمية الاحترافي</b> 📚

✨ <b>المميزات:</b>
- 5 أنماط كتابة مختلفة
- 5 قوالب تصميم احترافية
- تقارير مخصصة حسب احتياجاتك
- جودة عالية وسرعة فائقة

📝 <b>كيف تبدأ؟</b>
فقط أرسل لي موضوع التقرير وسأقوم بإنشاء تقرير احترافي بصيغة PDF

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
    
    # حفظ الموضوع في الجلسة
    user_sessions[user_id] = {"topic": topic}
    
    # إنشاء قائمة اختيار نمط الكتابة
    keyboard = []
    for key, value in WRITING_STYLES.items():
        keyboard.append([InlineKeyboardButton(value["name"], callback_data=f"style_{key}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    # استخدام HTML escape للنص
    safe_topic = topic.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
    
    await update.message.reply_text(
        f"📝 <b>تم استلام الموضوع:</b>\n<i>{safe_topic}</i>\n\n🎨 <b>اختر نمط الكتابة المناسب:</b>",
        reply_markup=reply_markup,
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
    
    # إنشاء قائمة اختيار القالب
    keyboard = []
    for key, value in TEMPLATES.items():
        keyboard.append([InlineKeyboardButton(f"{value['name']}", callback_data=f"template_{key}")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    style_name = WRITING_STYLES[style]["name"]
    await query.edit_message_text(
        f"✅ <b>تم اختيار:</b> {style_name}\n\n🎨 <b>الآن اختر تصميم التقرير:</b>",
        reply_markup=reply_markup,
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
    
    session = user_sessions[user_id]
    topic = session["topic"]
    style = session["style"]
    
    template_name = TEMPLATES[template]["name"]
    style_name = WRITING_STYLES[style]["name"]
    
    # استخدام HTML escape
    safe_topic = topic.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
    
    await query.edit_message_text(
        f"⏳ <b>جاري إنشاء التقرير...</b>\n\n📝 الموضوع: <i>{safe_topic}</i>\n✍️ النمط: {style_name}\n🎨 القالب: {template_name}\n\n⏱️ يستغرق 30-60 ثانية...",
        parse_mode='HTML'
    )
    
    try:
        pdf_bytes, title = generate_report(topic, style, template)
        
        if pdf_bytes:
            safe_name = "".join(c if c.isalnum() or c in (' ', '_', '-') else '_' for c in title[:30])
            filename = f"{safe_name}.pdf"
            
            # استخدام HTML escape للعنوان
            safe_title = title.replace('<', '&lt;').replace('>', '&gt;').replace('&', '&amp;')
            
            caption = f"""
✅ <b>تم إنشاء التقرير بنجاح!</b>

📄 <b>العنوان:</b> {safe_title}
✍️ <b>النمط:</b> {style_name}
🎨 <b>القالب:</b> {template_name}

🔄 <b>لإنشاء تقرير جديد، أرسل موضوعاً آخر!</b>
"""
            
            await context.bot.send_document(
                chat_id=query.message.chat_id,
                document=BytesIO(pdf_bytes),
                filename=filename,
                caption=caption,
                parse_mode='HTML'
            )
            
            await query.message.delete()
            logger.info(f"✅ PDF sent to user {user_id}")
            
            # مسح الجلسة
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
            await update.effective_message.reply_text(
                "❌ حدث خطأ في معالجة طلبك. حاول مرة أخرى."
            )
    except:
        pass

# ==========================================
# Main
# ==========================================
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
        application.add_handler(CallbackQueryHandler(style_callback, pattern='^style_'))
        application.add_handler(CallbackQueryHandler(template_callback, pattern='^template_'))
        application.add_error_handler(error_handler)
        
        logger.info("🤖 Bot Production Ready!")
        print("=" * 60)
        print("✅ Academic Reports Bot - Production Version 2.0")
        print("=" * 60)
        
        application.run_polling(allowed_updates=Update.ALL_TYPES)
        
    except Exception as e:
        logger.error(f"❌ Startup failed: {e}", exc_info=True)
        exit(1)


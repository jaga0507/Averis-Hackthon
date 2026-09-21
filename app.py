import io
import re
import streamlit as st
from groq import Groq
from pypdf import PdfReader
import docx
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

# ----------------- Configuration & Hardcoded API Key -----------------
# Paste your Groq API key below to hardcode it, or leave it blank to enter via the UI
HARDCODED_GROQ_API_KEY = API_KEY

st.set_page_config(
    page_title="Titan BL-Verify Assistant",
    page_icon="🚢",
    layout="wide"
)

# ----------------- Helper Functions -----------------
def extract_text_from_file(uploaded_file) -> str:
    """Extracts raw text from TXT, PDF, or DOCX files."""
    if uploaded_file is None:
        return ""
    
    file_name = uploaded_file.name.lower()
    
    if file_name.endswith(".txt"):
        return uploaded_file.read().decode("utf-8", errors="ignore")
    
    elif file_name.endswith(".pdf"):
        reader = PdfReader(uploaded_file)
        text = ""
        for page in reader.pages:
            content = page.extract_text()
            if content:
                text += content + "\n"
        return text
    
    elif file_name.endswith(".docx"):
        doc = docx.Document(io.BytesIO(uploaded_file.read()))
        return "\n".join([para.text for para in doc.paragraphs if para.text])
    
    return ""

def generate_discrepancy_pdf(report_text: str, filename_si: str, filename_bl: str) -> io.BytesIO:
    """Compiles the audit text into a downloadable PDF report."""
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=letter,
        leftMargin=36,
        rightMargin=36,
        topMargin=36,
        bottomMargin=36
    )
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#0F172A'),
        spaceAfter=6
    )
    meta_style = ParagraphStyle(
        'MetaText',
        parent=styles['Normal'],
        fontSize=9,
        leading=12,
        textColor=colors.HexColor('#475569')
    )
    normal_style = styles['Normal']

    story.append(Paragraph("<b>SHIPPING DOCUMENT DISCREPANCY AUDIT REPORT</b>", title_style))
    story.append(Paragraph(f"<b>Reference SI:</b> {filename_si} &nbsp;|&nbsp; <b>Draft BL:</b> {filename_bl}", meta_style))
    story.append(Paragraph("<b>Audited By:</b> Titan BL-Verify Engine", meta_style))
    story.append(Spacer(1, 14))

    for line in report_text.split("\n"):
        cleaned = line.strip()
        if not cleaned:
            story.append(Spacer(1, 4))
            continue
        
        # Format headers
        if cleaned.startswith("#"):
            h_text = re.sub(r"^#+\s*", "", cleaned)
            story.append(Paragraph(f"<b>{h_text}</b>", styles['Heading3']))
            story.append(Spacer(1, 4))
        else:
            # Escape HTML/XML entities
            safe = cleaned.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            safe = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", safe)
            story.append(Paragraph(safe, normal_style))
            story.append(Spacer(1, 3))

    doc.build(story)
    buffer.seek(0)
    return buffer

SYSTEM_AUDIT_PROMPT = """
You are TitanBL, an automated bilingual (English & Mandarin) shipping document auditor.
Your role is to verify Shipping Instructions (SI) against draft Bills of Lading (BL), and converse with operators.

Rules for document verification:
1. Ground Truth: The Shipping Instruction (SI) is the intended reference/ground truth.
2. Verify all 7 critical fields:
   - Shipper
   - Consignee
   - Notify Party
   - Port of Loading (POL)
   - Port of Discharge (POD)
   - Container Count
   - Gross Weight (kg)
3. Normalization: Treat bilingual translations (e.g., 'Shenzhen' and '深圳') and label variants ('Port of Loading' vs 'Load Port') as identical matches. Standardize weights to kg.
4. Output Format:
   - Show the 7 extracted fields from both SI and BL.
   - If discrepancies are found, display them side-by-side (SI Value vs Draft BL Value) in a Markdown table with clear explanations.
   - If all 7 fields match, state clearly: 'No mismatch detected.'
"""

# ----------------- Sidebar Configuration -----------------
with st.sidebar:
    st.title("⚙️ Configuration")
    
    if HARDCODED_GROQ_API_KEY.strip():
        api_key = HARDCODED_GROQ_API_KEY.strip()
        st.success("API Key loaded from script.")
    else:
        api_key = st.text_input("Enter Groq API Key:", type="password")
    
    model_choice = st.selectbox(
        "Groq Model:",
        options=["qwen/qwen3.8-27b", "llama-3.1-8b-instant", "llama-3.3-70b-versatile"],
        index=0
    )

    st.markdown("---")
    st.subheader("📄 Document Comparison")
    uploaded_si = st.file_uploader("1. Shipping Instruction (SI)", type=["txt", "pdf", "docx"])
    uploaded_bl = st.file_uploader("2. Draft Bill of Lading (BL)", type=["txt", "pdf", "docx"])
    
    analyze_btn = st.button("🚀 Analyze Discrepancy", use_container_width=True)
    
    if st.button("Clear Chat", use_container_width=True):
        st.session_state.messages = []
        st.session_state.last_report = None
        st.session_state.report_filenames = None
        st.rerun()

# ----------------- Session State & Client Setup -----------------
client = Groq(api_key=api_key) if api_key else None

if "messages" not in st.session_state:
    st.session_state.messages = [
        {"role": "assistant", "content": "Welcome to **Titan BL-Verify**. You can chat in English or Mandarin (中文), or upload an SI and BL file from the sidebar to check for discrepancies."}
    ]

if "last_report" not in st.session_state:
    st.session_state.last_report = None
    st.session_state.report_filenames = None

# Render Chat History
for msg in st.session_state.messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])

# ----------------- Run Document Verification -----------------
if analyze_btn:
    if not client:
        st.error("Missing Groq API Key. Set `HARDCODED_GROQ_API_KEY` in `app.py` or enter it in the sidebar.")
    elif not uploaded_si or not uploaded_bl:
        st.warning("Please upload both the SI and BL documents to perform comparison.")
    else:
        with st.spinner("Extracting documents and auditing discrepancies..."):
            si_text = extract_text_from_file(uploaded_si)
            bl_text = extract_text_from_file(uploaded_bl)
            
            analysis_prompt = f"""
Audit the following two documents for discrepancies according to your system instructions.

--- SHIPPING INSTRUCTION (SI Reference) ---
{si_text}

--- DRAFT BILL OF LADING (BL) ---
{bl_text}

Provide:
1. Extracted target fields for SI and Draft BL.
2. Side-by-side mismatch comparison table (if any).
3. Clear final verdict. If there are no mismatches across all 7 fields, explicitly state 'No mismatch detected'.
"""
            user_msg = f"Auditing documents: **{uploaded_si.name}** and **{uploaded_bl.name}**"
            st.session_state.messages.append({"role": "user", "content": user_msg})
            with st.chat_message("user"):
                st.markdown(user_msg)

            with st.chat_message("assistant"):
                message_placeholder = st.empty()
                full_response = ""
                
                stream = client.chat.completions.create(
                    model=model_choice,
                    messages=[
                        {"role": "system", "content": SYSTEM_AUDIT_PROMPT},
                        {"role": "user", "content": analysis_prompt}
                    ],
                    stream=True,
                    temperature=0.1
                )
                
                for chunk in stream:
                    delta = chunk.choices[0].delta.content or ""
                    full_response += delta
                    message_placeholder.markdown(full_response + "▌")
                
                message_placeholder.markdown(full_response)

            st.session_state.messages.append({"role": "assistant", "content": full_response})
            st.session_state.last_report = full_response
            st.session_state.report_filenames = (uploaded_si.name, uploaded_bl.name)

# ----------------- Persistent PDF Download Button -----------------
if st.session_state.last_report and st.session_state.report_filenames:
    si_name, bl_name = st.session_state.report_filenames
    pdf_bytes = generate_discrepancy_pdf(st.session_state.last_report, si_name, bl_name)
    
    st.download_button(
        label="📥 Download Audit Discrepancy Report (PDF)",
        data=pdf_bytes,
        file_name=f"Audit_Report_{bl_name}.pdf",
        mime="application/pdf",
        use_container_width=True
    )

# ----------------- User Text Interaction -----------------
user_input = st.chat_input("Ask a question, request changes, or query document status (EN/中文)...")

if user_input:
    if not client:
        st.error("Missing Groq API Key. Please provide an API key to continue.")
    else:
        st.session_state.messages.append({"role": "user", "content": user_input})
        with st.chat_message("user"):
            st.markdown(user_input)

        with st.chat_message("assistant"):
            message_placeholder = st.empty()
            full_response = ""

            messages_payload = [{"role": "system", "content": SYSTEM_AUDIT_PROMPT}]
            for m in st.session_state.messages:
                messages_payload.append({"role": m["role"], "content": m["content"]})

            stream = client.chat.completions.create(
                model=model_choice,
                messages=messages_payload,
                stream=True,
                temperature=0.3
            )

            for chunk in stream:
                delta = chunk.choices[0].delta.content or ""
                full_response += delta
                message_placeholder.markdown(full_response + "▌")

            message_placeholder.markdown(full_response)

        st.session_state.messages.append({"role": "assistant", "content": full_response})

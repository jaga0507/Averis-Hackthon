from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle

def create_shipping_instruction(filename="Shipping_Instruction_SI.pdf"):
    doc = SimpleDocTemplate(filename, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#1E3A8A'),
        spaceAfter=12
    )
    normal_style = styles['Normal']

    story.append(Paragraph("<b>SHIPPING INSTRUCTION (SI) - REFERENCE</b>", title_style))
    story.append(Paragraph("<b>Booking Ref:</b> BKG-2026-98124 | <b>Date:</b> 2026-09-20", normal_style))
    story.append(Spacer(1, 14))

    data = [
        [Paragraph("<b>Field Name</b>", normal_style), Paragraph("<b>Declared SI Information (Ground Truth)</b>", normal_style)],
        ["Shipper / Exporter", "Apex Global Manufacturing Ltd.\n12 Industrial Way, Shenzhen, China"],
        ["Consignee", "Pacific Freight Logistics Inc.\n88 Harbor Boulevard, Long Beach, CA, USA"],
        ["Notify Party", "Same as Consignee"],
        ["Port of Loading (POL)", "Shenzhen (Yantian), China"],
        ["Port of Discharge (POD)", "Long Beach Port, USA"],
        ["Container Count", "3 x 40ft High Cube Containers"],
        ["Total Gross Weight", "22,000.00 kg"],
        ["Cargo Description", "Automated Electronics & Industrial Controllers\nHS Code: 8537.10"]
    ]

    t = Table(data, colWidths=[160, 380])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (1, 0), colors.HexColor('#E2E8F0')),
        ('TEXTCOLOR', (0, 0), (1, 0), colors.HexColor('#0F172A')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#94A3B8')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(t)
    doc.build(story)
    print(f"Generated: {filename}")

def create_draft_bill_of_lading(filename="Draft_Bill_of_Lading_BL.pdf"):
    doc = SimpleDocTemplate(filename, pagesize=letter, leftMargin=36, rightMargin=36, topMargin=36, bottomMargin=36)
    styles = getSampleStyleSheet()
    story = []

    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontSize=18,
        leading=22,
        textColor=colors.HexColor('#B91C1C'),
        spaceAfter=12
    )
    normal_style = styles['Normal']

    story.append(Paragraph("<b>DRAFT BILL OF LADING (BL) - FOR REVIEW</b>", title_style))
    story.append(Paragraph("<b>Draft BL Number:</b> OOCL-HKG-774190 | <b>Status:</b> DRAFT / UNFINALIZED", normal_style))
    story.append(Spacer(1, 14))

    # Introduced discrepancies:
    # 1. Port of Discharge: Oakland instead of Long Beach (MISMATCH)
    # 2. Container Count: 4 containers instead of 3 (MISMATCH)
    # 3. Gross Weight: 26,500 kg instead of 22,000 kg (MISMATCH)
    # 4. Shipper / Port aliases: Uses semantic variation 'Load Port' and bilingual text to test AI normalization
    data = [
        [Paragraph("<b>Bill of Lading Field</b>", normal_style), Paragraph("<b>Draft BL Details</b>", normal_style)],
        ["Shipper", "Apex Global Manufacturing Ltd. (深圳顶峰制造有限公司)\n12 Industrial Way, Shenzhen, China"],
        ["Consignee", "Pacific Freight Logistics Inc.\n88 Harbor Boulevard, Long Beach, CA, USA"],
        ["Notify Party", "Same as Consignee"],
        ["Load Port", "Shenzhen (Yantian), China"],
        ["Port of Discharge", "Oakland, CA, USA"], # Discrepancy (SI says Long Beach)
        ["Number of Containers", "4 Containers (4 x 40HQ)"], # Discrepancy (SI says 3)
        ["Gross Weight", "26,500.00 kg"], # Discrepancy (SI says 22,000.00 kg)
        ["Description of Goods", "Automated Electronics & Industrial Controllers\nSaid to Contain: 1,400 Cartons"]
    ]

    t = Table(data, colWidths=[160, 380])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (1, 0), colors.HexColor('#FEE2E2')),
        ('TEXTCOLOR', (0, 0), (1, 0), colors.HexColor('#7F1D1D')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#FCA5A5')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('PADDING', (0, 0), (-1, -1), 8),
    ]))
    story.append(t)
    doc.build(story)
    print(f"Generated: {filename}")

if __name__ == "__main__":
    create_shipping_instruction()
    create_draft_bill_of_lading()
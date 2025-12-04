"""
श्रीLalita PDF Generator - Streamlit Web App
Complete working application for generating customer PDFs from POS data
"""

import streamlit as st
import pandas as pd
import yaml
import re
from io import BytesIO
from datetime import datetime
import zipfile
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
from reportlab.lib.units import inch

# Page configuration
st.set_page_config(
    page_title="श्रीLalita PDF Generator",
    page_icon="🥛",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Load configuration
@st.cache_data
def load_config():
    """Load configuration from YAML file"""
    try:
        with open('config.yaml', 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)
    except FileNotFoundError:
        # Default config if file doesn't exist
        return {
            'columns': {
                'receipt_id': 'ReceiptId',
                'date': 'Date',
                'customer_name': 'CustomerName',
                'customer_number': 'CustomerNumber',
                'entry_type': 'EntryType',
                'entry_name': 'EntryName',
                'payment_mode': 'PaymentMode'
            },
            'scheme': {
                'product': 'Raw Whole Milk',
                'price_1l_original': 60,
                'price_1l_discounted': 55,
                'price_500ml_original': 30,
                'price_500ml_discounted': 27.5
            }
        }

def process_excel_file(uploaded_file):
    """
    Read and process the POS Excel file
    Apply carry-forward logic to fill blank cells
    """
    try:
        # Read the receiptsWithItems sheet
        df = pd.read_excel(uploaded_file, sheet_name='receiptsWithItems')
        
        # Apply carry-forward for important columns using ffill()
        fill_columns = ['ReceiptId', 'Date', 'Cashier', 'CustomerName', 
                       'CustomerNumber', 'PaymentMode']
        
        for col in fill_columns:
            if col in df.columns:
                df[col] = df[col].ffill()  # Use ffill() instead of fillna(method='ffill')
        
        # Remove completely blank rows
        df = df.dropna(how='all')
        
        return df
        
    except Exception as e:
        st.error(f"Error reading Excel file: {str(e)}")
        return None

def get_unique_customers(df, payment_mode=None):
    """Extract unique customers from processed data, optionally filtered by payment mode"""
    if df is None or df.empty:
        return []
    
    # Filter rows with customer information
    customer_df = df[df['CustomerName'].notna() & df['CustomerNumber'].notna()].copy()
    
    # If payment mode specified (and not "All"), filter by it first
    if payment_mode and payment_mode != "All" and 'PaymentMode' in customer_df.columns:
        customer_df = customer_df[customer_df['PaymentMode'] == payment_mode]
    
    # Clean phone numbers BEFORE deduplication
    def clean_phone_number(val):
        if pd.notna(val):
            try:
                return str(int(float(val)))
            except (ValueError, TypeError, OverflowError):
                return str(val).strip()
        return ""
    
    customer_df['CustomerNumberClean'] = customer_df['CustomerNumber'].apply(clean_phone_number)
    customer_df['CustomerNameClean'] = customer_df['CustomerName'].astype(str).str.strip()
    
    # Get unique combinations based on CLEANED values
    unique_customers = customer_df[['CustomerNameClean', 'CustomerNumberClean']].drop_duplicates()
    
    # Sort by name
    unique_customers = unique_customers.sort_values('CustomerNameClean')
    
    # Convert to list of dicts
    customers = []
    for _, row in unique_customers.iterrows():
        customers.append({
            'name': row['CustomerNameClean'],
            'number': row['CustomerNumberClean']
        })
    
    return customers

def filter_customer_transactions(df, customer_number, start_date, end_date, payment_mode=None):
    """Filter transactions for a specific customer and date range"""
    
    # Convert dates
    start_dt = pd.to_datetime(start_date)
    end_dt = pd.to_datetime(end_date) + pd.Timedelta(hours=23, minutes=59, seconds=59)
    
    # Clean customer number (remove non-digits) - handle scientific notation
    try:
        clean_number = str(int(float(str(customer_number))))
    except (ValueError, TypeError, OverflowError):
        clean_number = ''.join(filter(str.isdigit, str(customer_number)))
    
    # Filter by customer - also handle scientific notation in dataframe
    def clean_phone(val):
        try:
            return str(int(float(str(val))))
        except:
            return ''.join(filter(str.isdigit, str(val)))
    
    customer_data = df[df['CustomerNumber'].apply(clean_phone) == clean_number]
    
    # Filter by date
    customer_data['DateParsed'] = pd.to_datetime(customer_data['Date'], errors='coerce')
    customer_data = customer_data[
        (customer_data['DateParsed'] >= start_dt) & 
        (customer_data['DateParsed'] <= end_dt)
    ]
    
    # Filter by payment mode if specified (skip if "All")
    if payment_mode and payment_mode != "All" and 'PaymentMode' in customer_data.columns:
        rows_before = len(customer_data)
        customer_data = customer_data[customer_data['PaymentMode'] == payment_mode]
        rows_after = len(customer_data)
        # Debug: This helps identify if filtering is working
        if rows_before > 0 and rows_after == rows_before:
            # Payment mode filtering didn't remove any rows - might be an issue
            pass
    
    # Filter by entry type (only Items and Discounts)
    customer_data = customer_data[customer_data['EntryType'].isin(['Item', 'Discount'])]
    
    return customer_data

def parse_entry_name(entry_name):
    """
    Parse entry name to extract product, quantity, and rate
    Format: "Product Name (Qty X Rate)"
    Example: "Raw Whole Milk (1 X 60)" -> ("Raw Whole Milk", 1, 60)
    """
    import re
    
    if pd.isna(entry_name):
        return None, None, None
    
    # Extract product name (everything before the opening parenthesis)
    product_match = re.match(r'^(.*?)\s*\(', entry_name)
    product = product_match.group(1).strip() if product_match else ""
    
    # Extract quantity and rate from "(Qty X Rate)"
    qty_rate_match = re.search(r'\(([0-9.]+)\s*X\s*([0-9.]+)\)', entry_name)
    
    if qty_rate_match:
        quantity = float(qty_rate_match.group(1))
        rate = float(qty_rate_match.group(2))
        return product, quantity, rate
    
    return product, None, None

def apply_scheme_discount(product, rate, apply_scheme, config):
    """Apply scheme discount if applicable"""
    if not apply_scheme:
        return rate
    
    scheme = config['scheme']
    
    if product == scheme['product']:
        if rate == scheme['price_1l_original']:
            return scheme['price_1l_discounted']
        elif rate == scheme['price_500ml_original']:
            return scheme['price_500ml_discounted']
    
    return rate

def generate_pdf(customer_name, customer_number, payment_mode, start_date, end_date, 
                transactions, apply_scheme, config):
    """
    Generate PDF for a customer
    Returns PDF as bytes
    """
    buffer = BytesIO()
    
    # Create PDF
    doc = SimpleDocTemplate(buffer, pagesize=A4)
    elements = []
    styles = getSampleStyleSheet()
    
    # Title
    title = Paragraph(f"<b>🧾 Customer Receipt</b>", styles['Title'])
    elements.append(title)
    elements.append(Spacer(1, 0.2*inch))
    
    # Customer info
    info_style = styles['Normal']
    elements.append(Paragraph(f"<b>Customer:</b> {customer_name}", info_style))
    elements.append(Paragraph(f"<b>Phone:</b> {customer_number}", info_style))
    elements.append(Paragraph(f"<b>Payment Mode:</b> {payment_mode}", info_style))
    elements.append(Paragraph(
        f"<b>Date Range:</b> {start_date.strftime('%d-%m-%Y')} to {end_date.strftime('%d-%m-%Y')}", 
        info_style
    ))
    
    if apply_scheme:
        scheme_text = Paragraph(
            f"<b>💰 Scheme Applied:</b> {config['scheme']['product']} @ ₹{config['scheme']['price_1l_discounted']}/L",
            info_style
        )
        elements.append(scheme_text)
    
    elements.append(Spacer(1, 0.3*inch))
    
    # Transactions table
    table_data = [['Date', 'Product', 'Qty', 'Rate', 'Amount']]
    total_amount = 0
    
    for _, trans in transactions.iterrows():
        date_str = trans['DateParsed'].strftime('%d-%m-%Y')
        entry_type = trans['EntryType']
        
        if entry_type == 'Discount':
            # Handle discount
            discount_match = re.search(r'\((\d+)\)', str(trans['EntryName']))
            if discount_match:
                discount_amount = float(discount_match.group(1))
                table_data.append([
                    date_str,
                    '💸 Discount',
                    '',
                    '',
                    f'-₹{discount_amount:.2f}'
                ])
                total_amount -= discount_amount
        else:
            # Handle item
            product, quantity, rate = parse_entry_name(trans['EntryName'])
            
            if product and quantity and rate:
                # Apply scheme discount
                rate = apply_scheme_discount(product, rate, apply_scheme, config)
                amount = quantity * rate
                
                table_data.append([
                    date_str,
                    product,
                    str(quantity),
                    f'₹{rate}',
                    f'₹{amount:.2f}'
                ])
                total_amount += amount
    
    # Create table
    table = Table(table_data, colWidths=[1.5*inch, 2.5*inch, 0.8*inch, 1*inch, 1.2*inch])
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 12),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black)
    ]))
    
    elements.append(table)
    elements.append(Spacer(1, 0.3*inch))
    
    # Total
    total_text = Paragraph(f"<b>💰 Total Amount: ₹{total_amount:.2f}</b>", styles['Heading2'])
    elements.append(total_text)
    
    elements.append(Spacer(1, 0.2*inch))
    
    # Thank you note
    thank_you = Paragraph(
        "🙏 श्रीLalita के शुद्ध 🥛 और पौष्टिक 🌿 दूध में आपके विश्वास के लिए धन्यवाद।",
        info_style
    )
    elements.append(thank_you)
    
    # Build PDF
    doc.build(elements)
    
    # Get PDF bytes
    pdf_bytes = buffer.getvalue()
    buffer.close()
    
    return pdf_bytes

# Main app
def main():
    st.title("🥛 श्रीLalita PDF Generator")
    st.markdown("---")
    
    # Load config
    config = load_config()
    
    # Sidebar
    with st.sidebar:
        st.header("ℹ️ About")
        st.info(
            "Upload your POS Excel file, select customers, "
            "and generate professional PDF receipts instantly!"
        )
        
        st.header("📋 Instructions")
        st.markdown("""
        1. Upload Excel file
        2. Set date range
        3. Select payment mode
        4. Choose customers
        5. Click Generate!
        """)
    
    # File upload
    st.header("📤 Upload POS Excel File")
    uploaded_file = st.file_uploader(
        "Choose your receipts Excel file",
        type=['xlsx'],
        help="Upload the Excel file exported from your POS system"
    )
    
    if uploaded_file is not None:
        # Process file
        with st.spinner("Processing Excel file..."):
            df = process_excel_file(uploaded_file)
        
        if df is not None and not df.empty:
            st.success(f"✅ File loaded: {len(df)} rows processed")
            
            # Debug section (expandable)
            with st.expander("🔍 Debug: Check Payment Modes in Your Data"):
                st.write("**Unique Payment Modes:**")
                st.write(df['PaymentMode'].value_counts())
                
                st.write("\n**Sample of how PaymentMode is stored:**")
                sample_df = df[df['EntryType'] == 'Item'].head(20)
                st.dataframe(sample_df[['CustomerName', 'EntryName', 'PaymentMode']])
            
            # Date range
            st.header("📅 Select Date Range")
            col1, col2 = st.columns(2)
            
            with col1:
                start_date = st.date_input(
                    "Start Date",
                    value=pd.to_datetime('today') - pd.Timedelta(days=30)
                )
            
            with col2:
                end_date = st.date_input(
                    "End Date",
                    value=pd.to_datetime('today')
                )
            
            # Payment mode
            st.header("💳 Payment Mode")
            payment_mode = st.selectbox(
                "Select payment mode",
                ["All", "Credit", "Cash", "UPI / BHIM", "Card"],
                index=0,
                help="Select 'All' to include all payment modes"
            )
            
            # Get customers - filtered by payment mode
            customers = get_unique_customers(df, payment_mode)
            
            if customers:
                st.header(f"👥 Select Customers ({len(customers)} found)")
                
                # Show info about filtering
                if payment_mode != "All":
                    st.info(f"ℹ️ Showing only customers with **{payment_mode}** transactions")
                
                # Select all/none buttons
                col1, col2 = st.columns(2)
                
                # Initialize session state for select all
                if 'select_all_state' not in st.session_state:
                    st.session_state.select_all_state = False
                
                # Button handlers
                if col1.button("✅ Select All"):
                    st.session_state.select_all_state = True
                    for idx in range(len(customers)):
                        st.session_state[f"customer_{idx}"] = True
                    st.rerun()
                
                if col2.button("❌ Deselect All"):
                    st.session_state.select_all_state = False
                    for idx in range(len(customers)):
                        st.session_state[f"customer_{idx}"] = False
                    st.rerun()
                
                # Customer selection
                selected_customers = []
                
                for idx, customer in enumerate(customers):
                    with st.container():
                        col1, col2 = st.columns([4, 1])
                        
                        # Customer checkbox
                        is_selected = col1.checkbox(
                            f"{customer['name']} ({customer['number']})",
                            key=f"customer_{idx}"
                        )
                        
                        if is_selected:
                            # Scheme checkbox
                            apply_scheme = col2.checkbox(
                                "💰 Scheme",
                                key=f"scheme_{idx}",
                                help="Apply discount for Raw Whole Milk"
                            )
                            
                            selected_customers.append({
                                **customer,
                                'scheme': apply_scheme
                            })
                
                st.markdown("---")
                
                # Generate button
                if selected_customers:
                    st.success(f"✅ {len(selected_customers)} customers selected")
                    
                    # Preview transactions (optional)
                    with st.expander("🔍 Preview transactions before generating PDFs"):
                        preview_customer = st.selectbox(
                            "Select customer to preview:",
                            [f"{c['name']} ({c['number']})" for c in selected_customers],
                            key="preview_selector"
                        )
                        
                        if preview_customer:
                            # Get customer details
                            customer_idx = [f"{c['name']} ({c['number']})" for c in selected_customers].index(preview_customer)
                            customer = selected_customers[customer_idx]
                            
                            # Filter transactions
                            preview_trans = filter_customer_transactions(
                                df,
                                customer['number'],
                                start_date,
                                end_date,
                                payment_mode
                            )
                            
                            st.write(f"**Transactions for {customer['name']}**")
                            st.write(f"**Filters:** Payment Mode = `{payment_mode}`, Date Range = `{start_date}` to `{end_date}`")
                            st.write(f"**Total items found:** {len(preview_trans)}")
                            
                            if not preview_trans.empty:
                                st.dataframe(
                                    preview_trans[['Date', 'EntryType', 'EntryName', 'PaymentMode']],
                                    use_container_width=True
                                )
                            else:
                                st.warning("⚠️ No transactions found for this customer with selected filters")
                    
                    if st.button("🎯 Generate PDFs", type="primary", use_container_width=True):
                        # Progress tracking
                        progress_bar = st.progress(0)
                        status_text = st.empty()
                        
                        # Store PDFs
                        generated_pdfs = []
                        
                        # Generate PDFs
                        for i, customer in enumerate(selected_customers):
                            status_text.text(
                                f"Generating PDF for {customer['name']} "
                                f"({i+1}/{len(selected_customers)})"
                            )
                            
                            # Filter transactions
                            transactions = filter_customer_transactions(
                                df,
                                customer['number'],
                                start_date,
                                end_date,
                                payment_mode
                            )
                            
                            if not transactions.empty:
                                # Generate PDF
                                pdf_bytes = generate_pdf(
                                    customer['name'],
                                    customer['number'],
                                    payment_mode,
                                    start_date,
                                    end_date,
                                    transactions,
                                    customer['scheme'],
                                    config
                                )
                                
                                generated_pdfs.append({
                                    'name': customer['name'],
                                    'filename': f"{customer['name']} - {start_date.strftime('%d-%m-%Y')} to {end_date.strftime('%d-%m-%Y')}.pdf",
                                    'bytes': pdf_bytes
                                })
                            
                            # Update progress
                            progress_bar.progress((i + 1) / len(selected_customers))
                        
                        status_text.empty()
                        progress_bar.empty()
                        
                        # Display results
                        st.header("📦 Generated PDFs")
                        
                        if generated_pdfs:
                            st.success(f"✅ Successfully generated {len(generated_pdfs)} PDFs!")
                            
                            # Individual download buttons
                            for idx, pdf in enumerate(generated_pdfs):
                                st.download_button(
                                    label=f"📄 Download: {pdf['name']}",
                                    data=pdf['bytes'],
                                    file_name=pdf['filename'],
                                    mime="application/pdf",
                                    key=f"download_pdf_{idx}"  # Unique key for each button
                                )
                            
                            # Create ZIP of all PDFs
                            if len(generated_pdfs) > 1:
                                zip_buffer = BytesIO()
                                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                                    for pdf in generated_pdfs:
                                        zip_file.writestr(pdf['filename'], pdf['bytes'])
                                
                                zip_buffer.seek(0)
                                
                                st.download_button(
                                    label="📦 Download All as ZIP",
                                    data=zip_buffer.getvalue(),
                                    file_name=f"receipts_{start_date.strftime('%d%m%Y')}_to_{end_date.strftime('%d%m%Y')}.zip",
                                    mime="application/zip",
                                    type="primary"
                                )
                        else:
                            st.warning("⚠️ No transactions found for selected customers in the date range")
                
                else:
                    st.info("👆 Please select at least one customer to generate PDFs")
            
            else:
                st.error("❌ No customers found in the Excel file")
        
        else:
            st.error("❌ Failed to process Excel file. Please check the format.")
    
    else:
        st.info("👆 Please upload an Excel file to get started")
    
    # Footer
    st.markdown("---")
    st.markdown(
        "<div style='text-align: center; color: gray;'>"
        "Made with ❤️ for श्रीLalita Dairy | Powered by Streamlit"
        "</div>",
        unsafe_allow_html=True
    )

if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
import os
import re
import uuid
import logging
import magic  # برای بررسی نوع فایل
from collections import defaultdict
from typing import List, Dict, Tuple, Optional, Set
import pandas as pd
from flask import Flask, render_template, request, send_file
from werkzeug.utils import secure_filename
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException, ElementNotVisibleException
from io import BytesIO
from email_validator import validate_email, EmailNotValidError

# --- Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_FOLDER = os.path.join(BASE_DIR, 'instance')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')
ALLOWED_EXTENSIONS: Set[str] = {'.xlsx'}
SELENIUM_TIMEOUT = 25
MAX_FILE_SIZE = 10 * 1024 * 1024

# --- Stop Words (English, Persian, Arabic Lists) ---
STOP_WORDS_ENGLISH: Set[str] = {"the", "a", "an", "of", "and", "to", "in", "for", "on", "with", "at", "by", "from", "as", "is", "it"}
STOP_WORDS_PERSIAN: Set[str] = {"و", "از", "به", "در", "با", "برای", "که", "این", "آن", "اینکه", "تا", "بر", "می", "باشد", "ندارد", "همچنین", "چه", "چرا", "کجا", "چگونه"}
STOP_WORDS_ARABIC: Set[str] = {"و", "من", "على", "في", "إلى", "أن", "كان", "ل", "مع", "عن", "منذ", "هذا", "ذلك", "لكن", "الذي", "إلى", "ما"}

# --- Folder Setup ---
for folder in [UPLOAD_FOLDER, RESULTS_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# --- Flask App ---
app = Flask(__name__, instance_path=INSTANCE_FOLDER)
app.config.update(
    UPLOAD_FOLDER=UPLOAD_FOLDER,
    RESULTS_FOLDER=RESULTS_FOLDER,
    MAX_CONTENT_LENGTH=MAX_FILE_SIZE,
    SECRET_KEY=os.environ.get('FLASK_SECRET_KEY', 'a_default_dev_secret_key')  # Use environment variable in production
)

# --- Logging ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(process)d - %(thread)d - %(message)s'
)
logging.getLogger('selenium').setLevel(logging.WARNING)

# --- Helper Functions ---

def allowed_file(filename: str) -> bool:
    """Checks if the file extension is allowed."""
    return '.' in filename and os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def is_valid_excel(filepath):
    """Validates if the file is an actual Excel file."""
    mime = magic.Magic(mime=True)
    mime_type = mime.from_file(filepath)
    return mime_type == 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'

def validate_url(url: str) -> str:
    """Validates the URL and ensures it starts with http:// or https://"""
    if not (url.startswith('http://') or url.startswith('https://')):
        url = 'https://' + url
    return url

def validate_email_address(email: str) -> bool:
    """Validates the email format."""
    try:
        validate_email(email)
        return True
    except EmailNotValidError as e:
        return False

def fetch_page_text(url: str) -> str:
    """Fetches and cleans text content from a URL using Selenium."""
    driver = None
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument("user-agent=Mozilla/5.0 ...")  # همانطور که قبل‌تر ذکر شد
        driver = uc.Chrome(options=chrome_options)
        driver.set_page_load_timeout(SELENIUM_TIMEOUT)
        driver.get(url)

        WebDriverWait(driver, SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        html = driver.page_source
        if not html:
            return ""

        text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip().lower()

        return text

    except (TimeoutException, WebDriverException, NoSuchElementException, ElementNotVisibleException) as e:
        logging.error(f"Error fetching {url}: {e}")
        return ""
    finally:
        if driver:
            driver.quit()

def get_phrases_from_file(filepath: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """Reads phrases (one per row) from the first column of an Excel file."""
    try:
        df = pd.read_excel(filepath, header=None, engine='openpyxl')
        if df.empty or df.shape[1] == 0:
            return None, "فایل اکسل خالی است یا ستون اول وجود ندارد."
        phrases = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
        valid_phrases = [p for p in phrases if p]
        return valid_phrases, None
    except Exception as e:
        return None, f"خطا در خواندن فایل اکسل: {e}"

def generate_excel_report(results: List[Dict]) -> str:
    """Generates an Excel report from results and saves it."""
    report_data = []
    for res in results:
        found_terms_str = '; '.join([f"{term}({score})" for term, score in res['found_terms'].items()]) if res['found_terms'] else "N/A"
        important_terms_str = ', '.join(res['important_terms']) if res['important_terms'] else "N/A"
        first_preview = next(iter(res['previews'].values()), "")
        report_data.append({
            'Original Phrase': res['original_phrase'],
            'Important Terms Searched': important_terms_str,
            'Found Terms (Count)': found_terms_str,
            'Total Score': res['total_score'],
            'Any Term Found': 'Yes' if res['found_any'] else 'No',
            'Analysis Notes': res['analysis_notes'] if res['analysis_notes'] else "-",
            'URL Checked': res['url'],
            'Sample Preview': first_preview
        })
    df = pd.DataFrame(report_data)
    output_name = f"analysis_{uuid.uuid4().hex}.xlsx"
    output_path = os.path.join(app.config['RESULTS_FOLDER'], output_name)
    df.to_excel(output_path, index=False, engine='openpyxl')
    return output_name

def analyze_phrases_for_keywords(phrases: List[str], page_text: str, url: str) -> List[Dict]:
    """Analyzes each phrase, breaks it into words, and searches each word in the page content."""
    results = []
    for phrase in phrases:
        words = phrase.split()  # Breaking the phrase into individual words
        found_terms = {}
        important_terms = []  # This can be enhanced by adding semantic analysis
        total_score = 0
        previews = {}

        for word in words:
            word = word.lower()
            if word in page_text:
                found_terms[word] = page_text.count(word)  # Count occurrences of the word in the text
                total_score += found_terms[word]  # Add to total score (this can be enhanced)
                previews[word] = page_text.lower().find(word)  # Save the first position as preview

        if found_terms:
            results.append({
                'original_phrase': phrase,
                'found_terms': found_terms,
                'important_terms': important_terms,
                'total_score': total_score,
                'found_any': True if found_terms else False,
                'analysis_notes': 'N/A',
                'url': url,
                'previews': previews
            })
        else:
            results.append({
                'original_phrase': phrase,
                'found_terms': {},
                'important_terms': important_terms,
                'total_score': 0,
                'found_any': False,
                'analysis_notes': 'No terms found',
                'url': url,
                'previews': {}
            })

    return results

# --- Flask Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    """Handles file upload, website input, processing, and displaying results."""
    results: List[Dict] = []
    error: Optional[str] = None
    download_filename: Optional[str] = None
    email: str = request.form.get('email', '').strip()
    website: str = request.form.get('website', '').strip()

    if request.method == 'POST':
        if not email: 
            error = "لطفا ایمیل را وارد کنید."
        elif not website: 
            error = "لطفا آدرس وب‌سایت را وارد کنید."
        elif not validate_email_address(email):
            error = "فرمت ایمیل وارد شده صحیح نیست."
        file = request.files.get('file')
        if not file: 
            error = "لطفا فایل اکسل حاوی عبارات کلیدی را انتخاب کنید."

        if website:
            website = validate_url(website)

        if not error:
            filepath = None
            try:
                filename = secure_filename(file.filename)
                unique_upload_name = f"{uuid.uuid4().hex}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_upload_name)
                file.save(filepath)
                if not is_valid_excel(filepath):
                    raise ValueError("فایل انتخابی اکسل معتبر نیست.")
                
                original_phrases, phrases_error = get_phrases_from_file(filepath)
                if phrases_error:
                    raise ValueError(f"خطا در پردازش فایل عبارات: {phrases_error}")
                
                page_text = fetch_page_text(website)
                results = analyze_phrases_for_keywords(original_phrases, page_text, website)
                if results:
                    download_filename = generate_excel_report(results)

            except Exception as e:
                error = str(e)
            finally:
                if filepath and os.path.exists(filepath):
                    os.remove(filepath)

    return render_template("index.html", results=results, error=error, download_filename=download_filename, email=email, website=website)

@app.route('/download/<filename>')
def download(filename: str):
    """Provides the generated analysis Excel file for download."""
    safe_name = secure_filename(filename)
    path = os.path.join(app.config['RESULTS_FOLDER'], safe_name)
    if not os.path.isfile(path):
        return "File not found", 404
    return send_file(path, as_attachment=True)

# --- Main Execution Guard ---
if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000)

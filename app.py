# -*- coding: utf-8 -*-
import os
import re
import uuid
import logging
from collections import Counter, defaultdict
from typing import List, Dict, Tuple, Optional, Set
import time

# Third-party imports
import pandas as pd
from flask import Flask, render_template, request, send_file, abort
from werkzeug.utils import secure_filename
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException
import undetected_chromedriver as uc
from contextlib import contextmanager
from nltk.corpus import stopwords
import nltk

# Download required NLTK data (run once or ensure in setup)
nltk.download('stopwords', quiet=True)

# --- Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_FOLDER = os.path.join(BASE_DIR, 'instance')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')
ALLOWED_EXTENSIONS: Set[str] = {'.xlsx'}
SELENIUM_TIMEOUT = 30  # Increased for reliability
MAX_FILE_SIZE = 10 * 1024 * 1024

# --- Stop Words (English + Persian) ---
STOP_WORDS: Set[str] = set(stopwords.words('english')) | {
    'و', 'در', 'به', 'از', 'این', 'که', 'است', 'با', 'برای', 'روی', 'تا', 'را',  # Persian stop words
    'com', 'www', 'http', 'https'
}

# --- Folder Setup ---
for folder in [UPLOAD_FOLDER, RESULTS_FOLDER]:
    os.makedirs(folder, exist_ok=True)

# --- Flask App ---
app = Flask(__name__, instance_path=INSTANCE_FOLDER)
app.config.update(
    UPLOAD_FOLDER=UPLOAD_FOLDER,
    RESULTS_FOLDER=RESULTS_FOLDER,
    MAX_CONTENT_LENGTH=MAX_FILE_SIZE,
    SECRET_KEY=os.environ.get('FLASK_SECRET_KEY') or str(uuid.uuid4())  # Secure default
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
    return '.' in filename and os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

@contextmanager
def get_driver():
    """Context manager for Selenium driver to ensure cleanup."""
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    chrome_options.add_argument('--disable-gpu')
    chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    driver = uc.Chrome(options=chrome_options)
    driver.set_page_load_timeout(SELENIUM_TIMEOUT)
    try:
        yield driver
    finally:
        driver.quit()
        logging.debug("Selenium driver closed.")

def fetch_page_text(url: str, retries: int = 2) -> str:
    """Fetches page text with retries."""
    for attempt in range(retries):
        try:
            with get_driver() as driver:
                logging.info(f"Fetching URL: {url} (Attempt {attempt + 1}/{retries})")
                driver.get(url)
                WebDriverWait(driver, SELENIUM_TIMEOUT).until(
                    EC.presence_of_element_located((By.TAG_NAME, "body"))
                )
                html = driver.page_source
                if not html:
                    logging.warning(f"No HTML content from {url}")
                    return ""
                text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.IGNORECASE | re.DOTALL)
                text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
                text = re.sub(r'<[^>]+>', ' ', text)
                text = re.sub(r'\s+', ' ', text).strip().lower()
                logging.info(f"Fetched text from {url}. Length: {len(text)}")
                return text
        except TimeoutException:
            logging.error(f"Timeout fetching {url}")
            if attempt == retries - 1:
                return ""
            time.sleep(2)
        except WebDriverException as e:
            logging.error(f"WebDriver error for {url}: {e}")
            return ""

def get_phrases_from_file(filepath: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """Reads phrases from Excel with specific error handling."""
    try:
        df = pd.read_excel(filepath, header=None, engine='openpyxl')
        if df.empty or df.shape[1] == 0:
            return None, "فایل اکسل خالی است یا ستون اول وجود ندارد."
        phrases = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
        valid_phrases = [p for p in phrases if p]
        if not valid_phrases:
            return None, "هیچ عبارت معتبری در ستون اول یافت نشد."
        logging.info(f"Extracted {len(valid_phrases)} phrases from {filepath}")
        return valid_phrases, None
    except (FileNotFoundError, pd.errors.EmptyDataError) as e:
        logging.error(f"File error {filepath}: {e}")
        return None, f"فایل اکسل یافت نشد یا خالی است."
    except pd.errors.ParserError as e:
        logging.error(f"Parse error {filepath}: {e}")
        return None, "فرمت فایل اکسل نامعتبر است."

def extract_important_words(phrase: str, stop_words: Set[str]) -> List[str]:
    """Extracts important words with basic cleaning."""
    if not phrase:
        return []
    cleaned_phrase = re.sub(r'[^\w\s-]', '', phrase.lower()).strip()
    words = re.split(r'\s+', cleaned_phrase)
    return sorted(list({
        word for word in words
        if word and word not in stop_words and len(word) > 1
    }))

def analyze_phrases_for_keywords(original_phrases: List[str], text: str, url: str) -> List[Dict]:
    """Analyzes phrases with pre-compiled regex for efficiency."""
    analysis_results = []
    if not text:
        for phrase in original_phrases:
            analysis_results.append({
                'original_phrase': phrase,
                'important_terms': [],
                'found_terms': {},
                'total_score': 0,
                'found_any': False,
                'url': url,
                'previews': {},
                'analysis_notes': "متن صفحه خالی بود"
            })
        return analysis_results

    for phrase in original_phrases:
        important_terms = extract_important_words(phrase, STOP_WORDS)
        found_terms_scores = defaultdict(int)
        found_terms_previews = {}
        total_phrase_score = 0
        found_any_term = False
        analysis_notes = ""

        if not important_terms:
            analysis_notes = "هیچ کلمه مهمی استخراج نشد"
        else:
            patterns = {
                term: re.compile(r'\b' + re.escape(term) + r'(?:s|es|ed|ing)?\b', re.IGNORECASE)
                for term in important_terms
            }
            for term, pattern in patterns.items():
                matches = list(pattern.finditer(text))
                count = len(matches)
                if count > 0:
                    found_any_term = True
                    found_terms_scores[term] = count
                    total_phrase_score += count
                    m = matches[0]
                    start = max(0, m.start() - 50)
                    end = min(len(text), m.end() + 50)
                    found_terms_previews[term] = f"...{text[start:end]}..."

        analysis_results.append({
            'original_phrase': phrase,
            'important_terms': important_terms,
            'found_terms': dict(found_terms_scores),
            'total_score': total_phrase_score,
            'found_any': found_any_term,
            'url': url,
            'previews': found_terms_previews,
            'analysis_notes': analysis_notes.strip()
        })
    return analysis_results

# --- Flask Routes ---

@app.route('/', methods=['GET', 'POST'])
def index():
    results: List[Dict] = []
    error: Optional[str] = None
    download_filename: Optional[str] = None
    email = request.form.get('email', '').strip()
    website = request.form.get('website', '').strip()

    if request.method == 'POST':
        # Validate inputs
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            error = "فرمت ایمیل نامعتبر است."
        elif not website:
            error = "لطفا آدرس وب‌سایت را وارد کنید."
        file = request.files.get('file')
        if not file or not file.filename or not allowed_file(file.filename):
            if not error:
                error = "لطفا یک فایل اکسل معتبر (.xlsx) انتخاب کنید."

        if website and not website.startswith(('http://', 'https://')):
            website = 'https://' + website  # Default to HTTPS

        if not error:
            filepath = None
            try:
                filename = secure_filename(file.filename)
                unique_upload_name = f"{uuid.uuid4().hex}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_upload_name)
                file.save(filepath)
                logging.info(f"File uploaded: {unique_upload_name}")

                original_phrases, phrases_error = get_phrases_from_file(filepath)
                if phrases_error:
                    raise ValueError(phrases_error)

                page_text = fetch_page_text(website)
                results = analyze_phrases_for_keywords(original_phrases, page_text, website)

                if results:
                    report_data = [
                        {
                            'Original Phrase': r['original_phrase'],
                            'Important Terms Searched': ', '.join(r['important_terms']) or 'N/A',
                            'Found Terms (Count)': '; '.join(f"{t}({s})" for t, s in r['found_terms'].items()) or 'N/A',
                            'Total Score': r['total_score'],
                            'Any Term Found': 'بله' if r['found_any'] else 'خیر',
                            'Analysis Notes': r['analysis_notes'] or '-',
                            'URL Checked': r['url'],
                            'Sample Preview': ' | '.join(r['previews'].values()) or '-'
                        }
                        for r in results
                    ]
                    df = pd.DataFrame(report_data)
                    output_name = f"analysis_{uuid.uuid4().hex}.xlsx"
                    output_path = os.path.join(app.config['RESULTS_FOLDER'], output_name)
                    df.to_excel(output_path, index=False, engine='openpyxl')
                    download_filename = output_name
                    logging.info(f"Report generated: {output_name}")

            except ValueError as ve:
                error = str(ve)
            except Exception as e:
                logging.exception(f"Processing error: {e}")
                error = f"خطا در پردازش: {str(e)}"
            finally:
                if filepath and os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                        logging.info(f"Cleaned up: {filepath}")
                    except OSError as e:
                        logging.error(f"Cleanup error: {e}")

    return render_template("index.html",
                          results=results,
                          error=error,
                          download_filename=download_filename,
                          email=email,
                          website=website)

@app.route('/download/<filename>')
def download(filename: str):
    safe_name = secure_filename(filename)
    path = os.path.realpath(os.path.join(app.config['RESULTS_FOLDER'], safe_name))
    if not path.startswith(os.path.realpath(app.config['RESULTS_FOLDER'])) or not os.path.isfile(path):
        logging.warning(f"Invalid download attempt: {filename}")
        abort(404, description="فایل یافت نشد.")
    return send_file(path, as_attachment=True)

if __name__ == '__main__':
    print(f"Starting Flask app at http://0.0.0.0:5000")
    app.run(debug=False, host='0.0.0.0', port=5000)

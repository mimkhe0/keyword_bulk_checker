# -*- coding: utf-8 -*-
import os
import re
import uuid
import logging
from collections import Counter, defaultdict # defaultdict is useful here
from typing import List, Dict, Tuple, Optional, Set

import pandas as pd
from flask import Flask, render_template, request, send_file
from werkzeug.utils import secure_filename
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException

# --- Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_FOLDER = os.path.join(BASE_DIR, 'instance')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')
ALLOWED_EXTENSIONS: Set[str] = {'.xlsx'}
SELENIUM_TIMEOUT = 25
MAX_FILE_SIZE = 10 * 1024 * 1024

# --- Stop Words (Simple English List) ---
# You can expand this list or use libraries like NLTK/spaCy for more comprehensive lists
STOP_WORDS: Set[str] = {
    'a', 'about', 'above', 'after', 'again', 'against', 'all', 'am', 'an', 'and', 'any', 'are', "aren't", 'as', 'at',
    'be', 'because', 'been', 'before', 'being', 'below', 'between', 'both', 'but', 'by',
    'can', "can't", 'cannot', 'could', "couldn't", 'did', "didn't", 'do', 'does', "doesn't", 'doing', "don't", 'down', 'during',
    'each', 'few', 'for', 'from', 'further', 'had', "hadn't", 'has', "hasn't", 'have', "haven't", 'having', 'he', "he'd", "he'll", "he's", 'her', 'here', "here's", 'hers', 'herself', 'him', 'himself', 'his', 'how', "how's",
    'i', "i'd", "i'll", "i'm", "i've", 'if', 'in', 'into', 'is', "isn't", 'it', "it's", 'its', 'itself',
    "let's", 'me', 'more', 'most', "mustn't", 'my', 'myself', 'no', 'nor', 'not', 'of', 'off', 'on', 'once', 'only', 'or', 'other', 'ought', 'our', 'ours', 'ourselves', 'out', 'over', 'own',
    'same', "shan't", 'she', "she'd", "she'll", "she's", 'should', "shouldn't", 'so', 'some', 'such',
    'than', 'that', "that's", 'the', 'their', 'theirs', 'them', 'themselves', 'then', 'there', "there's", 'these', 'they', "they'd", "they'll", "they're", "they've", 'this', 'those', 'through', 'to', 'too', 'under', 'until', 'up', 'very',
    'was', "wasn't", 'we', "we'd", "we'll", "we're", "we've", 'were', "weren't", 'what', "what's", 'when', "when's", 'where', "where's", 'which', 'while', 'who', "who's", 'whom', 'why', "why's", 'with', "won't", 'would', "wouldn't",
    'you', "you'd", "you'll", "you're", "you've", 'your', 'yours', 'yourself', 'yourselves',
    # Domain specific stop words could be added if needed
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
    SECRET_KEY=os.environ.get('FLASK_SECRET_KEY', 'a_default_dev_secret_key')
)

# --- Logging ---
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(process)d - %(thread)d - %(message)s'
)
logging.getLogger('selenium').setLevel(logging.WARNING) # Quieten Selenium logs

# --- Helper Functions ---

def allowed_file(filename: str) -> bool:
    """Checks if the file extension is allowed."""
    return '.' in filename and os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def fetch_page_text(url: str) -> str:
    """Fetches and cleans text content from a URL using Selenium."""
    driver = None
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless')
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36")

        driver = uc.Chrome(options=chrome_options)
        driver.set_page_load_timeout(SELENIUM_TIMEOUT)
        logging.info(f"Fetching URL: {url}")
        driver.get(url)

        WebDriverWait(driver, SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )
        html = driver.page_source
        if not html:
            logging.warning(f"No HTML content from {url}")
            return ""

        # Clean HTML: Remove scripts, styles, then tags. Normalize whitespace. Lowercase.
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip().lower()

        logging.info(f"Successfully fetched and cleaned text from {url}. Length: {len(text)}")
        # Removed the debug file writing here
        return text

    except TimeoutException:
        logging.error(f"[Selenium Timeout] Timed out loading URL: {url}")
        return ""
    except WebDriverException as e:
        logging.error(f"[Selenium WebDriverException] Error fetching {url}: {e}")
        return ""
    except Exception as e:
        logging.error(f"[Selenium Unexpected Error] Error fetching {url}: {type(e).__name__} - {e}")
        return ""
    finally:
        if driver:
            try:
                driver.quit()
            except Exception as e:
                logging.error(f"Error quitting Selenium driver for {url}: {e}")


def get_phrases_from_file(filepath: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """Reads phrases (one per row) from the first column of an Excel file."""
    try:
        df = pd.read_excel(filepath, header=None)
        if df.empty or df.shape[1] == 0:
            return None, "فایل اکسل خالی است یا ستون اول وجود ندارد."

        phrases = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
        # Keep original phrases, cleaning/filtering happens later
        valid_phrases = [p for p in phrases if p] # Ensure no empty strings

        if not valid_phrases:
            return None, "هیچ عبارت معتبری در ستون اول فایل اکسل یافت نشد."

        logging.info(f"Extracted {len(valid_phrases)} phrases from {filepath}")
        return valid_phrases, None
    except FileNotFoundError:
        logging.error(f"Keyword file not found at: {filepath}")
        return None, "فایل اکسل یافت نشد."
    except Exception as e:
        logging.error(f"Error reading phrases from Excel file {filepath}: {e}")
        return None, f"خطا در خواندن فایل اکسل: {e}"

def extract_important_words(phrase: str, stop_words: Set[str]) -> List[str]:
    """
    Extracts important words from a phrase by removing stop words and punctuation.

    Args:
        phrase: The input phrase.
        stop_words: A set of words to ignore.

    Returns:
        A list of unique, important words in lowercase.
    """
    if not phrase:
        return []
    # Remove punctuation (except maybe hyphens within words if needed) and convert to lowercase
    cleaned_phrase = re.sub(r'[^\w\s-]', '', phrase.lower()).strip()
    # Split into words
    words = re.split(r'\s+', cleaned_phrase)
    # Filter out stop words and short words (e.g., less than 2 chars)
    important_words = [word for word in words if word and word not in stop_words and len(word) > 1]
    # Return unique important words
    return sorted(list(set(important_words)))


def analyze_phrases_for_keywords(original_phrases: List[str], text: str, url: str) -> List[Dict]:
    """
    Analyzes each original phrase to find its important constituent keywords within the text.

    Args:
        original_phrases: List of phrases read from the Excel file.
        text: The webpage text content (lowercase).
        url: The URL where the text was found.

    Returns:
        A list of dictionaries, each representing an original phrase and its analysis.
    """
    analysis_results = []
    if not text:
        logging.warning("Cannot analyze phrases: Page text is empty.")
        # Optionally return results indicating failure for all phrases
        for phrase in original_phrases:
             analysis_results.append({
                'original_phrase': phrase,
                'important_terms': [],
                'found_terms': {}, # Dictionary to hold {term: score}
                'total_score': 0,
                'found_any': False,
                'url': url,
                'analysis_notes': "Page text was empty"
            })
        return analysis_results


    for phrase in original_phrases:
        important_terms = extract_important_words(phrase, STOP_WORDS)
        found_terms_scores = defaultdict(int) # Use defaultdict for easier counting
        found_terms_previews = {}
        total_score = 0
        found_any_term = False

        if not important_terms:
            logging.debug(f"No important terms extracted from phrase: '{phrase}'")
            analysis_notes = "No important terms extracted"
        else:
             analysis_notes = ""
             for term in important_terms:
                term_score = 0
                try:
                    # Regex to find whole words, potentially with simple suffixes (s, es, ed, ing)
                    escaped_term = re.escape(term)
                    # Optional: Adjust pattern if suffix matching is too broad or too narrow
                    pattern = r'\b' + escaped_term + r'(?:s|es|ed|ing)?\b'
                    matches = list(re.finditer(pattern, text, re.IGNORECASE))
                    term_score = len(matches)

                    if term_score > 0:
                        found_any_term = True
                        found_terms_scores[term] = term_score
                        total_score += term_score
                        # Get preview for the first match of this term
                        if term not in found_terms_previews:
                             m = matches[0]
                             preview_text = text[max(0, m.start()-50) : min(len(text), m.end()+50)]
                             found_terms_previews[term] = f"...{preview_text}..."

                except Exception as e:
                    logging.error(f"Error checking term '{term}' from phrase '{phrase}': {e}")
                    analysis_notes += f" Error checking '{term}';" # Add note about term error

        # Structure the result for this phrase
        analysis_results.append({
            'original_phrase': phrase,
            'important_terms': important_terms, # List the terms we looked for
            'found_terms': dict(found_terms_scores), # Convert defaultdict back to dict for output {term: score}
            'total_score': total_score, # Sum of scores of all found important terms
            'found_any': found_any_term, # True if at least one important term was found
            'url': url, # URL where checked
            'previews': found_terms_previews, # Dictionary of {term: preview_snippet}
            'analysis_notes': analysis_notes.strip() # Any errors or notes during analysis
        })
        logging.debug(f"Analyzed phrase '{phrase}'. Found terms: {dict(found_terms_scores)}")

    logging.info(f"Completed analysis for {len(original_phrases)} phrases on URL {url}.")
    return analysis_results


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
        # 1. Validate Input
        if not email:
            error = "لطفا ایمیل را وارد کنید."
        elif not website:
            error = "لطفا آدرس وب‌سایت را وارد کنید."

        file = request.files.get('file')
        if not file:
            if not error: error = "لطفا فایل اکسل حاوی عبارات کلیدی را انتخاب کنید."
        elif not file.filename:
             if not error: error = "فایل انتخاب شده نام ندارد."
        elif not allowed_file(file.filename):
            if not error: error = "فرمت فایل مجاز نیست. لطفا فایل .xlsx آپلود کنید."

        # Add URL prefix if missing
        if website and not website.startswith(('http://', 'https://')):
            website = 'http://' + website
            logging.info(f"Prepended http:// to website URL: {website}")

        # 2. Process if input is valid so far
        if not error:
            try:
                filename = secure_filename(file.filename)
                unique_upload_name = f"{uuid.uuid4().hex}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_upload_name)
                file.save(filepath)
                logging.info(f"File '{filename}' uploaded as '{unique_upload_name}' by {email}")

                # 3. Extract Original Phrases
                original_phrases, phrases_error = get_phrases_from_file(filepath)
                if phrases_error:
                    raise ValueError(f"خطا در پردازش فایل عبارات: {phrases_error}")
                if not original_phrases:
                     raise ValueError("هیچ عبارت معتبری در فایل یافت نشد.")

                # 4. Fetch Website Content
                logging.info(f"Fetching content for {website} requested by {email}")
                page_text = fetch_page_text(website)
                # If page_text is empty, analyze_phrases_for_keywords will handle it

                # 5. Analyze Phrases for Keywords
                logging.info(f"Analyzing {len(original_phrases)} phrases against {website}")
                # Call the new analysis function
                results = analyze_phrases_for_keywords(original_phrases, page_text, website)

                # 6. Generate Report (structure needs adjustment in Excel)
                if not results:
                     error = "نتیجه‌ای برای تحلیل وجود ندارد."
                else:
                    # Flatten the results slightly for better Excel representation
                    report_data = []
                    for res in results:
                        report_data.append({
                            'Original Phrase': res['original_phrase'],
                            'Important Terms Searched': ', '.join(res['important_terms']),
                            'Found Terms (Count)': '; '.join([f"{term}({score})" for term, score in res['found_terms'].items()]),
                            'Total Score': res['total_score'],
                            'Any Term Found': res['found_any'],
                            'Analysis Notes': res['analysis_notes'],
                            'URL': res['url'],
                            # Previews might make the Excel file large/complex, maybe omit or add selectively
                            #'First Preview Sample': next(iter(res['previews'].values()), "") if res['previews'] else ""
                        })

                    df = pd.DataFrame(report_data)
                    output_name = f"analysis_{uuid.uuid4().hex}.xlsx"
                    output_path = os.path.join(app.config['RESULTS_FOLDER'], output_name)
                    df.to_excel(output_path, index=False, engine='openpyxl')
                    download_filename = output_name
                    logging.info(f"Analysis report generated: '{output_name}' for {website} requested by {email}")

            except ValueError as ve:
                 logging.warning(f"Processing failed for {email} / {website}: {ve}")
                 error = str(ve)
            except Exception as e:
                logging.exception(f"Unexpected error during processing request from {email} for {website}: {e}")
                error = "خطا در پردازش اطلاعات. لطفا ورودی‌ها را بررسی کرده و دوباره تلاش کنید."
            # finally: # Optional cleanup of uploaded file
                 # if 'filepath' in locals() and os.path.exists(filepath): os.remove(filepath)

    # Render template needs update to display new results structure
    return render_template("index.html",
                           results=results, # Pass the raw results list to template
                           error=error,
                           download_filename=download_filename,
                           email=email,
                           website=website)


@app.route('/download/<filename>')
def download(filename: str):
    """Provides the generated analysis Excel file for download."""
    try:
        safe_name = secure_filename(filename)
        if os.path.sep in safe_name or '..' in safe_name:
            return "Invalid filename", 400

        path = os.path.join(app.config['RESULTS_FOLDER'], safe_name)
        if not os.path.isfile(path): # Use isfile for better check
            logging.warning(f"Download request for non-existent file: {safe_name}")
            return "فایل یافت نشد", 404

        logging.info(f"Download request for file: {safe_name}")
        return send_file(path, as_attachment=True)

    except Exception as e:
        logging.exception(f"Error during file download for {filename}: {e}")
        return "Internal server error", 500

# --- Main Execution ---
if __name__ == '__main__':
    print(f"✅ Flask app running. Upload folder: {UPLOAD_FOLDER}, Results folder: {RESULTS_FOLDER}")
    print(f"   Access at http://0.0.0.0:5000")
    # Use Waitress or Gunicorn for production deployment
    # from waitress import serve
    # serve(app, host='0.0.0.0', port=5000)
    app.run(debug=False, host='0.0.0.0', port=5000) # Keep debug=False for production-like testing

# -*- coding: utf-8 -*-
import os
import re
import uuid
import logging
from collections import Counter, defaultdict # defaultdict is useful here
from typing import List, Dict, Tuple, Optional, Set

# Third-party imports
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
    SECRET_KEY=os.environ.get('FLASK_SECRET_KEY', 'a_default_dev_secret_key') # Use environment variable in production
)

# --- Logging ---
# Consider adding file rotation for production logs
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
        # Set a common user agent
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/98.0.4758.102 Safari/537.36")

        # Ensure undetected_chromedriver path is correctly handled if needed
        driver = uc.Chrome(options=chrome_options)
        driver.set_page_load_timeout(SELENIUM_TIMEOUT)
        logging.info(f"Fetching URL: {url}")
        driver.get(url)

        # Wait for body tag to ensure basic page load
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
        return text

    except TimeoutException:
        logging.error(f"[Selenium Timeout] Timed out loading URL: {url}")
        return ""
    except WebDriverException as e:
        # Log more specific WebDriver errors if possible
        logging.error(f"[Selenium WebDriverException] Error fetching {url}: {e}")
        return ""
    except Exception as e:
        logging.error(f"[Selenium Unexpected Error] Error fetching {url}: {type(e).__name__} - {e}")
        return ""
    finally:
        if driver:
            try:
                driver.quit()
                logging.debug(f"Selenium driver quit successfully for {url}")
            except Exception as e:
                logging.error(f"Error quitting Selenium driver for {url}: {e}")


def get_phrases_from_file(filepath: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """Reads phrases (one per row) from the first column of an Excel file."""
    try:
        # Specify engine for broader compatibility
        df = pd.read_excel(filepath, header=None, engine='openpyxl')
        if df.empty or df.shape[1] == 0:
            return None, "فایل اکسل خالی است یا ستون اول وجود ندارد."

        # Read first column, drop empty rows, convert to string, strip whitespace
        phrases = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
        valid_phrases = [p for p in phrases if p] # Ensure no empty strings remain

        if not valid_phrases:
            return None, "هیچ عبارت معتبری در ستون اول فایل اکسل یافت نشد."

        logging.info(f"Extracted {len(valid_phrases)} phrases from {filepath}")
        return valid_phrases, None
    except FileNotFoundError:
        logging.error(f"Keyword file not found at: {filepath}")
        return None, "فایل اکسل یافت نشد."
    except Exception as e:
        # Log the actual error for debugging
        logging.error(f"Error reading phrases from Excel file {filepath}: {e}")
        # Provide a slightly more informative message to the user
        return None, f"خطا در خواندن فایل اکسل. فرمت فایل ممکن است نامعتبر باشد یا فایل آسیب دیده باشد. ({type(e).__name__})"

def extract_important_words(phrase: str, stop_words: Set[str]) -> List[str]:
    """
    Extracts important words from a phrase by removing stop words and punctuation.

    Args:
        phrase: The input phrase.
        stop_words: A set of words to ignore.

    Returns:
        A list of unique, important words in lowercase, sorted alphabetically.
    """
    if not phrase:
        return []
    # Remove punctuation (allow hyphens within words) and convert to lowercase
    cleaned_phrase = re.sub(r'[^\w\s-]', '', phrase.lower()).strip()
    # Split into words based on whitespace
    words = re.split(r'\s+', cleaned_phrase)
    # Filter out stop words and very short words (e.g., less than 2 chars)
    important_words = [
        word for word in words
        if word and word not in stop_words and len(word) > 1
    ]
    # Return unique important words, sorted for consistency
    return sorted(list(set(important_words)))

def analyze_phrases_for_keywords(original_phrases: List[str], text: str, url: str) -> List[Dict]:
    """
    Analyzes each original phrase to find its important constituent keywords within the text,
    now attempting basic singular form check for plural terms ending in 's'.

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
        # Return results indicating failure for all phrases if text is empty
        for phrase in original_phrases:
             analysis_results.append({
                'original_phrase': phrase,
                'important_terms': [], # No terms could be searched
                'found_terms': {},
                'total_score': 0,
                'found_any': False,
                'url': url,
                'previews': {},
                'analysis_notes': "Page text was empty"
            })
        return analysis_results

    # Perform analysis for each phrase
    for phrase in original_phrases:
        important_terms = extract_important_words(phrase, STOP_WORDS)
        # Use defaultdict for easier accumulation of scores for each original term
        found_terms_scores = defaultdict(int)
        found_terms_previews = {} # Store preview for the first match of either form (key is original term)
        total_phrase_score = 0
        found_any_term_for_phrase = False
        analysis_notes = ""

        if not important_terms:
            logging.debug(f"No important terms extracted from phrase: '{phrase}'")
            analysis_notes = "No important terms extracted"
        else:
            # Check each important term extracted from the phrase
            for term in important_terms:
                term_found_count = 0 # Count for this specific term (considering plural/singular variants)
                try:
                    # --- Search for the term itself (plural or original form) ---
                    escaped_term = re.escape(term)
                    # Pattern: whole word, case-insensitive, optional simple suffixes
                    pattern_main = r'\b' + escaped_term + r'(?:s|es|ed|ing)?\b'
                    matches_main = list(re.finditer(pattern_main, text, re.IGNORECASE))
                    term_found_count += len(matches_main)

                    # Store the first preview found for this term (from main match if available)
                    if len(matches_main) > 0 and term not in found_terms_previews:
                        m = matches_main[0]
                        start_idx = max(0, m.start() - 50)
                        end_idx = min(len(text), m.end() + 50)
                        preview_text = text[start_idx:end_idx]
                        # Highlight the exact matched span in preview
                        # preview_highlight = preview_text.replace(m.group(0), f"**{m.group(0)}**", 1)
                        found_terms_previews[term] = f"...{preview_text}..." # Associate preview with the original term

                    # --- Additionally, search for basic singular form if term looks plural ---
                    singular_term = None
                    # Simple check: ends in 's' but not 'ss' (like 'glass') and is longer than 2 chars
                    if term.endswith('s') and not term.endswith('ss') and len(term) > 2:
                        singular_term = term[:-1] # Basic singularization (e.g., 'cats' -> 'cat')
                        # Further refinement possible for 'es', 'ies' if needed, but keep it simple first

                    # Search for singular form only if it's different from the original term
                    if singular_term and singular_term != term:
                        escaped_singular = re.escape(singular_term)
                        pattern_singular = r'\b' + escaped_singular + r'(?:s|es|ed|ing)?\b'
                        matches_singular = list(re.finditer(pattern_singular, text, re.IGNORECASE))
                        term_found_count += len(matches_singular) # Add count from singular matches

                        # Add preview from singular match if no preview exists yet for this term
                        if len(matches_singular) > 0 and term not in found_terms_previews:
                            m_sing = matches_singular[0]
                            start_idx_sing = max(0, m_sing.start() - 50)
                            end_idx_sing = min(len(text), m_sing.end() + 50)
                            preview_text_sing = text[start_idx_sing:end_idx_sing]
                            # preview_highlight_sing = preview_text_sing.replace(m_sing.group(0), f"**{m_sing.group(0)}**", 1)
                            found_terms_previews[term] = f"...{preview_text_sing}..."

                    # --- Update overall results if this term (or its singular variant) was found ---
                    if term_found_count > 0:
                        found_any_term_for_phrase = True
                        # Store total count under the original extracted term
                        found_terms_scores[term] = term_found_count
                        # Accumulate total score for the entire phrase
                        total_phrase_score += term_found_count

                except Exception as e:
                    logging.error(f"Error checking term '{term}' (or singular) from phrase '{phrase}': {e}")
                    analysis_notes += f" Error checking '{term}';" # Append note about the error

        # --- Structure the final result for this original phrase ---
        analysis_results.append({
            'original_phrase': phrase,
            'important_terms': important_terms, # List the terms derived from the phrase
            'found_terms': dict(found_terms_scores), # Dict: {original_term: total_count_found}
            'total_score': total_phrase_score, # Sum of counts for all found terms derived from this phrase
            'found_any': found_any_term_for_phrase, # True if any derived term was found
            'url': url, # URL where the check was performed
            'previews': found_terms_previews, # Dict: {original_term: first_preview_snippet}
            'analysis_notes': analysis_notes.strip() # Any errors or notes during this phrase's analysis
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
    # Get form data safely, providing defaults
    email: str = request.form.get('email', '').strip()
    website: str = request.form.get('website', '').strip()

    if request.method == 'POST':
        # 1. Validate Input Fields
        if not email: # Basic check, can add email format validation
            error = "لطفا ایمیل را وارد کنید."
        elif not website:
            error = "لطفا آدرس وب‌سایت را وارد کنید."

        # 2. Validate File Upload
        file = request.files.get('file')
        if not file:
            # Set error only if no previous error exists
            if not error: error = "لطفا فایل اکسل حاوی عبارات کلیدی را انتخاب کنید."
        elif not file.filename:
             if not error: error = "فایل انتخاب شده نام ندارد."
        elif not allowed_file(file.filename):
            if not error: error = f"فرمت فایل مجاز نیست. فقط فایل‌های {', '.join(ALLOWED_EXTENSIONS)} مجاز هستند."

        # 3. Add URL scheme if missing (simple check)
        if website and not website.startswith(('http://', 'https://')):
            # Default to http, consider https for robustness if needed
            website = 'http://' + website
            logging.info(f"Prepended http:// to website URL: {website}")

        # 4. Proceed with Processing if all inputs seem valid so far
        if not error:
            filepath = None # Initialize filepath to ensure it exists in finally block if needed
            try:
                # Sanitize filename and create a unique name for storage
                filename = secure_filename(file.filename)
                unique_upload_name = f"{uuid.uuid4().hex}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_upload_name)
                file.save(filepath)
                logging.info(f"File '{filename}' uploaded as '{unique_upload_name}' by {email}")

                # 5. Extract Original Phrases from Excel
                original_phrases, phrases_error = get_phrases_from_file(filepath)
                if phrases_error:
                    # Raise ValueError to be caught below, providing specific error
                    raise ValueError(f"خطا در پردازش فایل عبارات: {phrases_error}")
                if not original_phrases: # Should be caught by phrases_error, but double-check
                     raise ValueError("هیچ عبارت معتبری در فایل اکسل یافت نشد.")

                # 6. Fetch Website Content
                logging.info(f"Fetching content for {website} requested by {email}")
                page_text = fetch_page_text(website)
                # analysis function handles empty page_text, no need for explicit error here unless fetch failed fundamentally

                # 7. Analyze Phrases against Website Text
                logging.info(f"Analyzing {len(original_phrases)} phrases against {website}")
                results = analyze_phrases_for_keywords(original_phrases, page_text, website)

                # 8. Generate Excel Report if results exist
                if not results:
                     error = "پردازش انجام شد، اما نتیجه‌ای برای گزارش وجود ندارد." # More informative message
                else:
                    # Prepare data for DataFrame, flattening complex fields
                    report_data = []
                    for res in results:
                        # Create readable strings for list/dict fields
                        found_terms_str = '; '.join([f"{term}({score})" for term, score in res['found_terms'].items()]) if res['found_terms'] else "N/A"
                        important_terms_str = ', '.join(res['important_terms']) if res['important_terms'] else "N/A"
                        # Selectively include a preview (e.g., first one found)
                        first_preview = next(iter(res['previews'].values()), "") if res['previews'] else ""

                        report_data.append({
                            'Original Phrase': res['original_phrase'],
                            'Important Terms Searched': important_terms_str,
                            'Found Terms (Count)': found_terms_str,
                            'Total Score': res['total_score'],
                            'Any Term Found': 'Yes' if res['found_any'] else 'No', # More readable boolean
                            'Analysis Notes': res['analysis_notes'] if res['analysis_notes'] else "-", # Use '-' for empty notes
                            'URL Checked': res['url'],
                            'Sample Preview': first_preview # Add one preview sample to report
                        })

                    df = pd.DataFrame(report_data)
                    # Create a unique name for the results file
                    output_name = f"analysis_{uuid.uuid4().hex}.xlsx"
                    output_path = os.path.join(app.config['RESULTS_FOLDER'], output_name)
                    df.to_excel(output_path, index=False, engine='openpyxl') # Ensure engine is specified
                    download_filename = output_name # Pass filename for download link generation
                    logging.info(f"Analysis report generated: '{output_name}' for {website} requested by {email}")
                    # Optional: Send email notification here using the 'email' variable

            except ValueError as ve: # Catch specific processing errors (like file reading/parsing)
                 logging.warning(f"Processing failed for {email} / {website}: {ve}")
                 error = str(ve) # Show the specific error message from the exception
            except Exception as e:
                # Catch unexpected errors during processing
                logging.exception(f"Unexpected error during processing request from {email} for {website}: {e}")
                error = "خطا در پردازش اطلاعات. لطفاً ورودی‌ها را بررسی کرده و دوباره تلاش کنید یا با پشتیبانی تماس بگیرید."
            # finally:
                # Optional: Clean up the uploaded file after processing
                # if filepath and os.path.exists(filepath):
                #    try:
                #        os.remove(filepath)
                #        logging.info(f"Cleaned up upload file: {filepath}")
                #    except OSError as oe:
                #        logging.error(f"Error cleaning up upload file {filepath}: {oe}")
                # pass


    # Render the template, passing necessary data
    # The template (index.html) needs to be designed to display the 'results' structure appropriately.
    return render_template("index.html",
                           results=results, # Pass the raw analysis results list to the template
                           error=error,
                           download_filename=download_filename,
                           email=email, # Retain email in form field
                           website=website) # Retain website in form field


@app.route('/download/<filename>')
def download(filename: str):
    """Provides the generated analysis Excel file for download."""
    try:
        # Secure the filename again before joining path
        safe_name = secure_filename(filename)
        # Basic directory traversal check
        if os.path.sep in safe_name or '..' in safe_name:
            logging.warning(f"Potential directory traversal attempt in download: {filename}")
            return "Invalid filename", 400

        # Construct the full path to the file within the designated results folder
        path = os.path.join(app.config['RESULTS_FOLDER'], safe_name)

        # Check if the file exists and is actually a file (not a directory)
        if not os.path.isfile(path):
            logging.warning(f"Download request for non-existent or non-file path: {path}")
            # Use Flask's abort function for standard HTTP errors
            from flask import abort
            abort(404, description="File not found.") # Send a standard 404 error

        logging.info(f"Download request for file: {safe_name}")
        # Send the file to the user, prompting download dialog
        return send_file(path, as_attachment=True)

    except Exception as e:
        # Log any unexpected error during the download process
        logging.exception(f"Error during file download for {filename}: {e}")
        from flask import abort
        abort(500, description="Internal server error during download.") # Send a standard 500 error


# --- Main Execution Guard ---
if __name__ == '__main__':
    # Print some helpful info when running directly
    print(f"--- Keyword Analysis Flask App ---")
    print(f"Instance Path: {app.instance_path}")
    print(f"Upload Folder: {app.config['UPLOAD_FOLDER']}")
    print(f"Results Folder: {app.config['RESULTS_FOLDER']}")
    print(f"Log File: {LOG_FILE}")
    print(f"---")
    print(f"✅ Flask development server starting...")
    print(f"   Access the application at http://0.0.0.0:5000 or http://127.0.0.1:5000")
    print(f"---")
    # Run the Flask development server
    # NOTE: For production, use a proper WSGI server like Gunicorn or Waitress.
    # Example: waitress-serve --host 0.0.0.0 --port 5000 app:app
    app.run(debug=False, host='0.0.0.0', port=5000) # Keep debug=False usually

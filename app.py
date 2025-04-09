# -*- coding: utf-8 -*-
import os
import re
import uuid
import logging
import magic  # برای بررسی نوع فایل
# from collections import defaultdict # دیگر نیازی نیست
from typing import List, Dict, Tuple, Optional, Set, Union # Union اضافه شد
import pandas as pd
from flask import Flask, render_template, request, send_file
from werkzeug.utils import secure_filename
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import TimeoutException, WebDriverException, NoSuchElementException, ElementNotVisibleException
# from io import BytesIO # دیگر نیازی نیست
from email_validator import validate_email, EmailNotValidError

# --- Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_FOLDER = os.path.join(BASE_DIR, 'instance')
UPLOAD_FOLDER = os.path.join(INSTANCE_FOLDER, 'uploads')
RESULTS_FOLDER = os.path.join(INSTANCE_FOLDER, 'results')
LOG_FILE = os.path.join(INSTANCE_FOLDER, 'app.log')
ALLOWED_EXTENSIONS: Set[str] = {'.xlsx'}
SELENIUM_TIMEOUT = 30 # کمی افزایش یافت
MAX_FILE_SIZE = 10 * 1024 * 1024
SNIPPET_CONTEXT_LENGTH = 50 # تعداد کاراکتر قبل و بعد از عبارت برای پیش‌نمایش

# --- Stop Words (Combined for simplicity, consider language detection for more complex scenarios) ---
# You might want to refine this based on the expected language of the phrases/content
STOP_WORDS_ENGLISH: Set[str] = {
    "the", "a", "an", "of", "and", "to", "in", "for", "on", "with", "at", "by",
    "from", "as", "is", "it", "this", "that", "was", "were", "be", "being",
    "been", "have", "has", "had", "do", "does", "did", "will", "would", "shall",
    "should", "can", "could", "may", "might", "must", "about", "above", "after",
    "again", "against", "all", "am", "any", "are", "aren't", "because", "before",
    "below", "between", "both", "but", "cannot", "couldn't", "didn't", "doesn't",
    "doing", "don't", "down", "during", "each", "few", "further", "hadn't",
    "hasn't", "haven't", "having", "he", "he'd", "he'll", "he's", "her", "here",
    "here's", "hers", "herself", "him", "himself", "his", "how", "how's", "i",
    "i'd", "i'll", "i'm", "i've", "if", "into", "isn't", "it's", "its", "itself",
    "let's", "me", "more", "most", "mustn't", "my", "myself", "no", "nor", "not",
    "now", "off", "once", "only", "or", "other", "ought", "our", "ours", "ourselves",
    "out", "over", "own", "same", "shan't", "she", "she'd", "she'll", "she's",
    "shouldn't", "so", "some", "such", "than", "that's", "their", "theirs",
    "them", "themselves", "then", "there", "there's", "these", "they", "they'd",
    "they'll", "they're", "they've", "those", "through", "too", "under", "until",
    "up", "very", "wasn't", "we", "we'd", "we'll", "we're", "we've", "weren't",
    "what", "what's", "when", "when's", "where", "where's", "which", "while",
    "who", "who's", "whom", "why", "why's", "won't", "wouldn't", "you", "you'd",
    "you'll", "you're", "you've", "your", "yours", "yourself", "yourselves"
}
STOP_WORDS_PERSIAN: Set[str] = {
    "و", "در", "به", "از", "که", "می", "این", "است", "را", "با", "های", "برای",
    "آن", "تا", "شد", "وی", "یکی", "بود", "کرد", "نیز", "هم", "ما", "یا",
    "شده", "باید", "هر", "او", "خود", "پیش", "بر", "گفت", "پس", "کردن", "اگر",
    "همه", "نه", "دیگر", "حتی", "بین", "آنها", "ولی", "برخی", "طور", "شما",
    "همین", "داد", "داشته_باشد", "داشت", "خواهد_شد", "توان", "اما", "چیزی",
    "مانند", "کسی", "جای", "بی", "بعد", "اینکه", "قبل", "کنیم", "نمی", "باشد",
    "دارد", "همچنین", "چه", "چرا", "کجا", "چگونه", "دهد", "کند", "کنید", "کنند",
    "کردند", "یک", "دو" # اعداد کوچک هم می‌توانند اضافه شوند
}
STOP_WORDS_ARABIC: Set[str] = {
    "و", "في", "من", "على", "إلى", "لا", "أن", "هو", "كل", "مع", "هذا", "كان",
    "ما", "لم", "عن", "قد", "هي", "أو", "ثم", "حتى", "له", "ذلك", "أي", "قال",
    "يكون", "ب", "إن", "هم", "به", "ف", "التي", "الذي", "كما", "لن", "عند", "كانت",
    "بعض", "أكثر", "عليه", "هذه", "إلا", "غير", "شيء", "تم", "هناك", "مثل", "كانوا",
    "وهو", "وهي", "أيضا", "نحو", "كيف", "متى", "أين", "لماذا"
}
ALL_STOP_WORDS: Set[str] = STOP_WORDS_ENGLISH.union(STOP_WORDS_PERSIAN).union(STOP_WORDS_ARABIC)


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
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(process)d - %(thread)d - %(message)s'
)
logging.getLogger('selenium').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING) # Reduce noise from underlying libraries

# --- Helper Functions ---

def allowed_file(filename: str) -> bool:
    """Checks if the file extension is allowed."""
    return '.' in filename and os.path.splitext(filename)[1].lower() in ALLOWED_EXTENSIONS

def is_valid_excel(filepath: str) -> bool:
    """Validates if the file is an actual Excel file using MIME type."""
    try:
        mime = magic.Magic(mime=True)
        mime_type = mime.from_file(filepath)
        return mime_type in [
            'application/vnd.ms-excel', # .xls
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' # .xlsx
        ]
    except Exception as e:
        logging.error(f"Magic library error checking file {filepath}: {e}")
        # Fallback or assume invalid if magic fails
        return os.path.splitext(filepath)[1].lower() in ALLOWED_EXTENSIONS

def validate_url(url: str) -> str:
    """Validates the URL and ensures it starts with http:// or https://"""
    url = url.strip()
    if not re.match(r'^(http|https)://', url):
        # Basic check if it looks like domain.tld, prepend https
        if '.' in url.split('/')[-1]: # Avoid prepending to things like 'localhost:5000' without scheme
             return 'https://' + url
        else:
            # Could be an invalid URL format, maybe raise error or handle differently
             # For now, just return as is or maybe prepend https anyway
             return 'https://' + url # Or handle error better
    return url


def validate_email_address(email: str) -> bool:
    """Validates the email format."""
    try:
        validate_email(email, check_deliverability=False) # Deliverability check requires DNS and can be slow/fail
        return True
    except EmailNotValidError as e:
        logging.warning(f"Invalid email format: {email} - {e}")
        return False

def fetch_page_text(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Fetches and cleans text content from a URL using Selenium.
    Returns (page_text, error_message)
    """
    driver = None
    logging.info(f"Attempting to fetch URL: {url}")
    try:
        chrome_options = Options()
        chrome_options.add_argument('--headless=new') # Updated headless mode
        chrome_options.add_argument('--no-sandbox')
        chrome_options.add_argument('--disable-dev-shm-usage')
        chrome_options.add_argument('--disable-gpu')
        # Use a common, realistic user agent
        chrome_options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36") # Example modern UA
        chrome_options.add_argument('--blink-settings=imagesEnabled=false') # Disable images for faster loading
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"]) # Try to hide automation flags
        chrome_options.add_experimental_option('useAutomationExtension', False)

        driver = uc.Chrome(options=chrome_options, version_main=123) # Specify version if needed

        driver.set_page_load_timeout(SELENIUM_TIMEOUT)
        driver.set_script_timeout(SELENIUM_TIMEOUT) # Also timeout for javascript execution

        driver.get(url)

        # Wait for body or a main content element if known
        WebDriverWait(driver, SELENIUM_TIMEOUT).until(
            EC.presence_of_element_located((By.TAG_NAME, "body"))
        )

        # Execute script to get text content, potentially more robust than page_source parsing
        # page_text = driver.execute_script("return document.body.innerText") # Option 1: innerText
        html = driver.page_source # Option 2: Use page_source and clean

        if not html:
            logging.warning(f"No HTML content retrieved from {url}")
            return None, "محتوایی از صفحه دریافت نشد."

        # --- Text Cleaning (Improved) ---
        # Remove scripts and styles first
        text = re.sub(r'<script[^>]*>.*?</script>', ' ', html, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r'<style[^>]*>.*?</style>', ' ', text, flags=re.IGNORECASE | re.DOTALL)
        # Remove HTML comments
        text = re.sub(r'', ' ', text, flags=re.DOTALL)
        # Remove all remaining HTML tags
        text = re.sub(r'<[^>]+>', ' ', text)
        # Replace multiple whitespace chars (including newlines, tabs) with a single space
        text = re.sub(r'\s+', ' ', text).strip()
        # Convert to lowercase for case-insensitive matching
        text = text.lower()

        logging.info(f"Successfully fetched and cleaned text from {url}. Length: {len(text)}")
        return text, None

    except TimeoutException as e:
        logging.error(f"Timeout error fetching {url}: {e}")
        return None, f"خطای وقفه زمانی (Timeout) هنگام بارگذاری صفحه: {url}"
    except WebDriverException as e:
        logging.error(f"WebDriver error fetching {url}: {e}")
        # Check for common specific errors
        if "net::ERR_NAME_NOT_RESOLVED" in str(e) or "dns error" in str(e).lower():
             return None, f"آدرس وب‌سایت نامعتبر یا در دسترس نیست: {url}"
        if "unable to connect" in str(e).lower():
             return None, f"امکان برقراری ارتباط با وب‌سایت وجود ندارد: {url}"
        return None, f"خطای مرورگر (WebDriver) هنگام دسترسی به صفحه: {e}"
    except (NoSuchElementException, ElementNotVisibleException) as e:
         logging.error(f"Element error on {url}: {e}")
         return None, f"خطا در یافتن المان‌های ضروری صفحه: {e}"
    except Exception as e:
        logging.error(f"Unexpected error fetching {url}: {e}", exc_info=True)
        return None, f"خطای پیش‌بینی نشده در دریافت اطلاعات صفحه: {e}"
    finally:
        if driver:
            try:
                driver.quit()
                logging.info(f"Selenium driver quit for {url}")
            except Exception as e:
                logging.error(f"Error quitting driver for {url}: {e}")

def get_phrases_from_file(filepath: str) -> Tuple[Optional[List[str]], Optional[str]]:
    """Reads phrases (one per row) from the first column of an Excel file."""
    try:
        df = pd.read_excel(filepath, header=None, engine='openpyxl')
        if df.empty or df.shape[1] == 0:
            return None, "فایل اکسل خالی است یا ستون اول وجود ندارد."
        # Select first column, convert to string, strip whitespace, drop NaNs/empty strings
        phrases = df.iloc[:, 0].dropna().astype(str).str.strip().tolist()
        # Filter out any potentially remaining empty strings after stripping
        valid_phrases = [p for p in phrases if p]
        if not valid_phrases:
             return None, "هیچ عبارت معتبری در ستون اول فایل اکسل یافت نشد."
        logging.info(f"Read {len(valid_phrases)} phrases from {filepath}")
        return valid_phrases, None
    except FileNotFoundError:
        logging.error(f"Excel file not found at path: {filepath}")
        return None, f"فایل اکسل یافت نشد: {filepath}"
    except pd.errors.EmptyDataError:
        logging.warning(f"Excel file is empty: {filepath}")
        return None, "فایل اکسل خالی است."
    except Exception as e:
        logging.error(f"Error reading Excel file {filepath}: {e}", exc_info=True)
        return None, f"خطا در خواندن یا پردازش فایل اکسل: {e}"

def get_context_snippet(text: str, phrase: str, context_len: int = SNIPPET_CONTEXT_LENGTH) -> Optional[str]:
    """Extracts a snippet of text around the first occurrence of the phrase."""
    try:
        # Use regex for robust finding (handles potential regex chars in phrase if escaped)
        # Find all non-overlapping matches
        matches = list(re.finditer(re.escape(phrase), text, re.IGNORECASE))
        if not matches:
            return None

        match = matches[0] # Use the first match
        start, end = match.span()

        # Calculate snippet boundaries, ensuring they are within text limits
        snippet_start = max(0, start - context_len)
        snippet_end = min(len(text), end + context_len)

        # Add ellipsis if truncated
        prefix = "..." if snippet_start > 0 else ""
        suffix = "..." if snippet_end < len(text) else ""

        # Extract the snippet
        snippet = text[snippet_start:snippet_end]

        # Optional: Highlight the phrase within the snippet (using simple markers)
        # Be careful with case-insensitivity if highlighting
        # highlighted_snippet = snippet.replace(phrase, f"**{phrase}**") # Simple markdown-like bolding

        return f"{prefix}{snippet}{suffix}"

    except Exception as e:
        logging.error(f"Error generating snippet for phrase '{phrase}': {e}", exc_info=True)
        return "[خطا در ایجاد پیش‌نمایش]"


def analyze_phrases_in_text(
    phrases: List[str],
    page_text: str,
    url: str
) -> List[Dict[str, Union[str, int, bool, Dict, List[str], None]]]:
    """
    Analyzes each phrase, searches for the *entire phrase* in the page content,
    calculates score based on phrase count, identifies important terms (non-stop words),
    and generates context snippets.
    """
    results = []
    if not page_text: # Handle case where page text couldn't be fetched
        logging.warning(f"Page text is empty for URL {url}. Skipping analysis.")
        for phrase in phrases:
             results.append({
                'original_phrase': phrase,
                'phrase_to_find': phrase.lower().strip(), # Store the processed phrase
                'found_phrase_count': 0,
                'important_terms': [], # Still calculate important terms even if page is empty
                'total_score': 0,
                'found_phrase': False,
                'analysis_notes': "محتوای صفحه وب قابل دریافت یا خالی بود.",
                'url': url,
                'context_snippet': None
            })
        return results

    logging.info(f"Analyzing {len(phrases)} phrases against text of length {len(page_text)} from {url}")

    for phrase in phrases:
        if not phrase or not phrase.strip():
            logging.warning("Skipping empty phrase.")
            continue

        original_phrase = phrase # Keep original for reporting
        phrase_lower = phrase.strip().lower() # Process for searching

        # 1. Identify Important Terms (non-stop words) from the original phrase
        words_in_phrase = re.findall(r'\b\w+\b', phrase_lower) # Basic word tokenization
        important_terms = [word for word in words_in_phrase if word not in ALL_STOP_WORDS]

        # 2. Search for the exact phrase (case-insensitive)
        # Use regex finditer for potentially better performance on large texts and getting locations
        try:
            # Escape phrase for regex search, as it might contain special characters
            escaped_phrase = re.escape(phrase_lower)
            matches = list(re.finditer(escaped_phrase, page_text, re.IGNORECASE))
            phrase_count = len(matches)
        except re.error as e:
             logging.error(f"Regex error searching for phrase '{phrase_lower}': {e}")
             phrase_count = 0 # Treat as not found if regex fails
             matches = [] # Ensure matches is empty list


        # 3. Calculate Score (simple count of the phrase)
        total_score = phrase_count

        # 4. Get Context Snippet (if found)
        context_snippet = None
        if phrase_count > 0 and matches:
            # Pass the actual found text span's content to snippet generator if needed
            # For simplicity, we use page_text and phrase_lower
            context_snippet = get_context_snippet(page_text, phrase_lower, SNIPPET_CONTEXT_LENGTH)


        # 5. Compile Results
        found_phrase = phrase_count > 0
        analysis_notes = None if found_phrase else "عبارت مورد نظر در متن صفحه یافت نشد."

        results.append({
            'original_phrase': original_phrase,
            'phrase_to_find': phrase_lower, # The actual searched term
            'found_phrase_count': phrase_count,
            'important_terms': important_terms,
            'total_score': total_score,
            'found_phrase': found_phrase,
            'analysis_notes': analysis_notes,
            'url': url,
            'context_snippet': context_snippet
        })

    logging.info(f"Analysis complete for {url}. Found matches for {sum(1 for r in results if r['found_phrase'])} out of {len(phrases)} phrases.")
    return results

def generate_excel_report(results: List[Dict], url_checked: str) -> str:
    """Generates an Excel report from results and saves it."""
    if not results:
        logging.warning("No results to generate report.")
        # Or raise an error, or return an indication of no report
        return None # Indicate no report generated

    report_data = []
    for res in results:
        important_terms_str = ', '.join(res.get('important_terms', [])) if res.get('important_terms') else "-"
        analysis_notes = res.get('analysis_notes', "-") if res.get('analysis_notes') else "-" # Ensure '-' if None/empty

        report_data.append({
            'Original Phrase': res.get('original_phrase', 'N/A'),
            'Phrase Searched (Lowercase)': res.get('phrase_to_find', 'N/A'),
            'Important Terms (Non-StopWords)': important_terms_str,
            'Times Found': res.get('found_phrase_count', 0),
            'Score (Phrase Count)': res.get('total_score', 0),
            'Phrase Found?': 'Yes' if res.get('found_phrase', False) else 'No',
            'Analysis Notes': analysis_notes,
            'Context Snippet': res.get('context_snippet', 'N/A'),
            # URL is the same for all rows in this structure, added below or could be per row
        })

    df = pd.DataFrame(report_data)

    # Add URL checked as a separate piece of info, maybe in filename or a header row if needed
    # For now, just use it in the filename or log it.
    # If you want it in the Excel file, you might add it as a metadata property or a first row.

    output_name = f"analysis_report_{uuid.uuid4().hex}.xlsx"
    output_path = os.path.join(app.config['RESULTS_FOLDER'], output_name)

    try:
        # Using openpyxl engine is generally recommended for .xlsx
        df.to_excel(output_path, index=False, engine='openpyxl')
        logging.info(f"Generated Excel report: {output_path} for URL: {url_checked}")
        return output_name
    except Exception as e:
        logging.error(f"Failed to generate Excel report {output_path}: {e}", exc_info=True)
        # Depending on requirements, you might want to raise this error
        # or return None to indicate failure.
        return None # Indicate failure


# --- Flask Routes ---
@app.route('/', methods=['GET', 'POST'])
def index():
    """Handles file upload, website input, processing, and displaying results."""
    results_summary: Optional[List[Dict]] = None # Store summary for display
    error: Optional[str] = None
    download_filename: Optional[str] = None
    processing_done: bool = False
    # Get form data, provide defaults for GET request
    email: str = request.form.get('email', '').strip()
    website: str = request.form.get('website', '').strip()

    if request.method == 'POST':
        logging.info(f"POST request received. Email: {'Provided' if email else 'Missing'}, Website: {'Provided' if website else 'Missing'}")
        # 1. Validate Inputs
        if 'file' not in request.files or not request.files['file'].filename:
            error = "لطفا فایل اکسل حاوی عبارات کلیدی را انتخاب کنید."
            logging.warning("File not provided in POST request.")
        elif not email:
            error = "لطفا ایمیل خود را وارد کنید."
            logging.warning("Email not provided in POST request.")
        elif not validate_email_address(email):
             error = "فرمت ایمیل وارد شده صحیح نیست."
             # Logged in validate_email_address
        elif not website:
            error = "لطفا آدرس وب‌سایت را وارد کنید."
            logging.warning("Website not provided in POST request.")
        else:
            file = request.files['file']
            if not allowed_file(file.filename):
                error = "فرمت فایل مجاز نیست. لطفا فایل اکسل با پسوند .xlsx آپلود کنید."
                logging.warning(f"Disallowed file extension: {file.filename}")

        if not error:
             # Proceed only if initial validations pass
            website = validate_url(website) # Ensure URL has scheme
            logging.info(f"Validated inputs. Processing website: {website}")
            filepath = None
            unique_upload_name = None
            try:
                # 2. Save and Validate Uploaded File
                filename = secure_filename(file.filename)
                unique_upload_name = f"{uuid.uuid4().hex}_{filename}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], unique_upload_name)
                file.save(filepath)
                logging.info(f"File saved temporarily to {filepath}")

                if not is_valid_excel(filepath):
                    # is_valid_excel logs errors internally if magic fails
                    raise ValueError("فایل آپلود شده یک فایل اکسل معتبر نیست.")

                # 3. Read Phrases from Excel
                phrases, phrases_error = get_phrases_from_file(filepath)
                if phrases_error:
                    # get_phrases_from_file logs errors
                    raise ValueError(f"{phrases_error}") # Pass specific error message up

                # 4. Fetch Website Content
                logging.info(f"Fetching text content for {website}...")
                page_text, fetch_error = fetch_page_text(website)
                if fetch_error:
                    # fetch_page_text logs errors
                    raise ConnectionError(f"{fetch_error}") # Use a more specific exception type if possible

                # 5. Analyze Phrases
                logging.info(f"Analyzing {len(phrases)} phrases...")
                analysis_results = analyze_phrases_in_text(phrases, page_text, website)

                # 6. Generate Report
                if analysis_results:
                    logging.info("Generating Excel report...")
                    download_filename = generate_excel_report(analysis_results, website)
                    if not download_filename:
                         # generate_excel_report logs errors
                         raise RuntimeError("خطا در تولید فایل گزارش اکسل.")
                    results_summary = analysis_results # Prepare for display
                else:
                     # This case might happen if page_text was empty or other issues in analysis
                     error = "تحلیلی انجام نشد یا نتیجه‌ای حاصل نشد." # Or provide more specific feedback
                     logging.warning("Analysis returned no results.")

                processing_done = True # Mark as done for template logic
                logging.info(f"Processing finished successfully for {website}.")

            except (ValueError, ConnectionError, RuntimeError) as e:
                error = str(e) # Use the specific error message raised
                logging.error(f"Processing error: {error}", exc_info=False) # Log error without full traceback for known types
            except Exception as e:
                error = f"یک خطای پیش‌بینی نشده رخ داد: {e}"
                logging.error("Unexpected error during processing:", exc_info=True) # Log full traceback for unknown errors
            finally:
                # 7. Cleanup Uploaded File
                if filepath and os.path.exists(filepath):
                    try:
                        os.remove(filepath)
                        logging.info(f"Cleaned up uploaded file: {filepath}")
                    except OSError as e:
                        logging.error(f"Error removing uploaded file {filepath}: {e}")

    # Render template with results or errors
    return render_template(
        "index.html",
        results=results_summary, # Pass analysis results for display
        error=error,
        download_filename=download_filename,
        processing_done=processing_done, # Indicate if processing step was reached
        # Pass back form values to repopulate fields
        email=email,
        website=website
    )


@app.route('/download/<filename>')
def download(filename: str):
    """Provides the generated analysis Excel file for download."""
    # Sanitize filename again before joining path
    safe_name = secure_filename(filename)
    if not safe_name or safe_name != filename:
         # If secure_filename modifies the name significantly or returns empty, likely invalid input
         logging.warning(f"Download attempt with potentially unsafe filename: {filename}")
         return "نام فایل نامعتبر است.", 400

    path = os.path.join(app.config['RESULTS_FOLDER'], safe_name)
    logging.info(f"Download request for: {path}")

    if not os.path.isfile(path):
        logging.error(f"Download failed: File not found at {path}")
        return "فایل مورد نظر یافت نشد.", 404

    try:
        # send_file handles Content-Disposition, MIME type etc.
        # `download_name` ensures the user sees the original generated name
        return send_file(path, download_name=safe_name, as_attachment=True)
    except Exception as e:
         logging.error(f"Error sending file {path} for download: {e}", exc_info=True)
         return "خطا در ارسال فایل.", 500


# --- Main Execution Guard ---
if __name__ == '__main__':
    # Consider using Waitress or Gunicorn for production instead of Flask dev server
    # Example using Waitress (install with pip install waitress):
    # from waitress import serve
    # serve(app, host='0.0.0.0', port=5000)

    # For development:
    app.run(debug=False, host='0.0.0.0', port=5000) # Keep debug=False unless actively developing

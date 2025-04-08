import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC

options = Options()
options.add_argument('--headless')
options.add_argument('--no-sandbox')
options.add_argument('--disable-dev-shm-usage')
options.add_argument("user-agent=Mozilla/5.0")

driver = uc.Chrome(options=options)
driver.set_page_load_timeout(20)

try:
    url = "https://best-istikhara.com/en/"
    driver.get(url)
    WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "body")))
    html = driver.page_source
    print("✅ HTML content fetched successfully.")
    print(html[:500])  # فقط اولین ۵۰۰ کاراکتر برای تست

except Exception as e:
    print("❌ Error occurred:", e)

finally:
    driver.quit()

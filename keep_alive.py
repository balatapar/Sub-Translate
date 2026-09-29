import time
from playwright.sync_api import sync_playwright

URL = "https://sub-fookin-translate.streamlit.app"

def wake_up_app():
    with sync_playwright() as p:
        # اجرا کردن مرورگر کرومیوم در حالت بدون سر
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        
        print(f"Opening app: {URL}")
        page.goto(URL, timeout=60000)
        time.sleep(5) # صبر برای لود شدن کامل
        
        # بررسی اینکه آیا دکمه بیدار کردن روی صفحه هست یا نه
        wake_button = page.locator("button:has-text('Yes, get this app back up!')")
        
        if wake_button.is_visible():
            print("App is sleeping! Clicking wake-up button...")
            wake_button.click()
            time.sleep(15) # صبر برای بالا آمدن کامل اپ
            print("Wake-up request sent successfully.")
        else:
            print("App is already awake!")
            
        browser.close()

if __name__ == "__main__":
    wake_up_app()

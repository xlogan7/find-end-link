from DrissionPage import ChromiumPage, ChromiumOptions
import time
import os

def click_turnstile_checkbox(url="https://2captcha.com/demo/cloudflare-turnstile"):
    # ========== Browser Setup ==========
    co = ChromiumOptions()

    # Common paths for Chrome, Edge, and Brave on Windows
    browser_paths = [
        # Google Chrome
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Users\{}\AppData\Local\Google\Chrome\Application\chrome.exe".format(os.getlogin()),

        # Microsoft Edge
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Users\{}\AppData\Local\Microsoft\Edge\Application\msedge.exe".format(os.getlogin()),

        # Brave Browser
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"C:\Users\{}\AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe".format(os.getlogin()),
    ]

    browser_found = False
    for path in browser_paths:
        if os.path.exists(path):
            co.set_browser_path(path)
            print(f"Using browser: {path}")
            browser_found = True
            break

    if not browser_found:
        print("Neither Chrome, Edge, nor Brave found in common locations!")
        print("Please check the path manually.")
        return

    # Stealth options
    co.set_argument("--disable-blink-features=AutomationControlled")
    co.set_argument("--no-sandbox")
    co.set_argument("--disable-infobars")
    co.set_argument("--disable-dev-shm-usage")
    # co.headless()  # keep False

    try:
        driver = ChromiumPage(co)
    except Exception as e:
        print("Failed to start browser:", e)
        return

    print(f"\n{'='*60}")
    print(f"Opening: {url}")
    print(f"{'='*60}\n")

    driver.get(url)
    time.sleep(4)

    print(f"Page Title : {driver.title}")
    print(f"Current URL: {driver.url}")
    print("-" * 50)

    # ========== Method 1: Click container ==========
    print("\n[Method 1] Trying to click .cf-turnstile container...")
    try:
        widget = driver.ele("css:.cf-turnstile", timeout=5)
        if widget:
            widget.click()
            print("  [OK] Clicked .cf-turnstile")
            time.sleep(2)
    except Exception as e:
        print(f"  [X] Failed: {e}")

    # ========== Method 2: Iframe + Checkbox ==========
    print("\n[Method 2] Looking for Turnstile iframe...")
    try:
        iframe = None
        for sel in [
            "css:iframe[src*='challenges.cloudflare.com']",
            "css:iframe[src*='turnstile']",
            "css:iframe[title*='Cloudflare']",
            "css:iframe[title*='Widget containing']",
            "tag:iframe",
        ]:
            try:
                iframe = driver.ele(sel, timeout=2)
                if iframe:
                    print(f"  [OK] Found iframe -> {sel}")
                    break
            except:
                continue

        if iframe:
            print("  Switching into iframe...")
            frame = driver.get_frame(iframe)

            clicked = False
            for sel in [
                "css:input[type='checkbox']",
                "css:.cb-i",
                "css:.ctp-checkbox-label",
                "css:label",
                "xpath://input[@type='checkbox']",
            ]:
                try:
                    checkbox = frame.ele(sel, timeout=2)
                    if checkbox:
                        print(f"  [OK] Found checkbox -> {sel}")
                        checkbox.click()
                        print("  [OK] Clicked checkbox!")
                        clicked = True
                        break
                except:
                    continue

            if not clicked:
                print("  [X] No checkbox found inside iframe")
                try:
                    frame.ele("tag:body").click()
                    print("  -> Clicked iframe body as fallback")
                except:
                    pass

            driver.get_frame(None)
        else:
            print("  [X] No suitable iframe found")
    except Exception as e:
        print(f"  [X] Method 2 error: {e}")

    # ========== Method 3: Coordinate click ==========
    print("\n[Method 3] Trying coordinate-based click...")
    try:
        widget = driver.ele("css:.cf-turnstile", timeout=3) or driver.ele("css:[data-sitekey]", timeout=2)
        if widget:
            size = widget.rect.size
            driver.actions.move_to(widget, offset_x=28, offset_y=size[1]//2).click()
            print("  [OK] Coordinate click performed")
    except Exception as e:
        print(f"  [X] Method 3 error: {e}")

    # ========== Check for token ==========
    print("\n[Checking] Waiting for token (up to 12 seconds)...")
    token = None
    for i in range(12):
        try:
            token_el = driver.ele("css:input[name='cf-turnstile-response']", timeout=1)
            val = token_el.value
            if val and len(val) > 30:
                token = val
                print(f"  [OK] SUCCESS! Token received after {i+1}s")
                print(f"  Token: {token[:90]}...")
                break
        except:
            pass
        time.sleep(1)
    else:
        print("  [X] No token generated")

    print(f"\n{'='*60}")
    if token:
        print("RESULT: Turnstile appears solved!")
    else:
        print("RESULT: Could not solve automatically.")
    print(f"{'='*60}\n")

    input("Press Enter to close the browser...")
    driver.quit()


if __name__ == "__main__":
    click_turnstile_checkbox("https://triptiketfly.xyz/register?ref=gacordisinimah")